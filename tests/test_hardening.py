"""Evidence protection, missing-evidence statuses, recipients and genealogy rules.

Every test runs inside the fixture's transaction and is rolled back. Rejected statements run in a
savepoint so the test can continue and check that nothing changed.
"""
from datetime import timedelta

import psycopg
import pytest

SHIPPED_LOTS = 'SELECT count(DISTINCT lot_id) FROM trace.v_containment_detail WHERE delivery_no IS NOT NULL'


def rejected(db, role, *statements):
    """Run statements as a group role in a savepoint; return the error they must raise."""
    with pytest.raises(psycopg.Error) as error:
        with db.transaction():
            if role:
                db.execute(f'SET LOCAL ROLE {role}')
            for statement in statements:
                db.execute(statement)
    return error.value


def plating_station(db):
    """A new, empty slot in the plating tank, so test carriers never collide with simulated ones."""
    tank_id, line_id = db.execute("SELECT tank_id, line_id FROM ref.tank WHERE function='plate' ORDER BY tank_id LIMIT 1").fetchone()
    slot = db.execute('SELECT max(slot) + 1 FROM ref.station WHERE tank_id=%s', (tank_id,)).fetchone()[0]
    station = db.execute('INSERT INTO ref.station (tank_id, slot, code) VALUES (%s,%s,%s) RETURNING station_id',
                         (tank_id, slot, f'TEST-S{slot}')).fetchone()[0]
    return tank_id, line_id, station


def plate_lot(db, start, template_lot, qty=10, parent=False, minutes=60):
    """Create a lot from template_lot's order and part (as its rework child if parent=True) and plate it."""
    _, line_id, station = plating_station(db)
    end = start + timedelta(minutes=minutes)
    load = db.execute("INSERT INTO trace.load (line_id, carrier_no, started_at, finished_at) VALUES (%s,'TEST',%s,%s) RETURNING load_id",
                      (line_id, start, end)).fetchone()[0]
    db.execute("INSERT INTO trace.load_step (load_id, station_id, in_station) VALUES (%s,%s,tstzrange(%s,%s,'[)'))", (load, station, start, end))
    lot = db.execute("""INSERT INTO trace.lot (lot_no, order_id, part_id, qty_pcs, parent_lot_id, origin, received_at)
                        SELECT 'TEST-' || lot_no || '-' || %s, order_id, part_id, %s, CASE WHEN %s THEN lot_id END,
                               CASE WHEN %s THEN 'rework' ELSE 'receipt' END, %s
                          FROM trace.lot WHERE lot_id=%s RETURNING lot_id""",
                     (load, qty, parent, parent, start, template_lot)).fetchone()[0]
    db.execute('INSERT INTO trace.load_lot VALUES (%s,%s,%s)', (load, lot, qty))
    return lot


# ---------- F-01: evidence cannot be rewritten by restricted roles ----------

@pytest.mark.parametrize('role,function', [
    ('role_readonly', 'lab.set_analysis_validity()'),
    ('role_readonly', "audit.log_change('id')"),
    ('role_quality', "audit.log_change('id')"),
    ('role_lab', 'lab.set_analysis_validity()'),
])
def test_security_definer_functions_cannot_be_attached_to_own_tables(db, role, function):
    error = rejected(db, role,
                     'CREATE TEMP TABLE t (id int, tank_id int, sampled_at timestamptz, valid_during tstzrange)',
                     f'CREATE TRIGGER x BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION {function}')
    assert isinstance(error, psycopg.errors.InsufficientPrivilege)


def test_lab_inserts_audited_analyses_but_cannot_rewrite_them(db):
    analysis = db.execute("SELECT analysis_id FROM lab.v_out_of_spec_period ORDER BY valid_during LIMIT 1").fetchone()[0]
    for statement in (f"UPDATE lab.analysis SET valid_during=tstzrange(sampled_at, sampled_at + interval '1 minute') WHERE analysis_id={analysis}",
                      f'DELETE FROM lab.analysis WHERE analysis_id={analysis}'):
        assert isinstance(rejected(db, 'role_lab', statement), psycopg.errors.InsufficientPrivilege)
    tank, last = db.execute('SELECT tank_id, max(sampled_at) FROM lab.analysis GROUP BY tank_id LIMIT 1').fetchone()
    db.execute("SELECT set_config('app.user','lab_test',true)")
    db.execute('SET LOCAL ROLE role_lab')
    new_id = db.execute("INSERT INTO lab.analysis (tank_id, sampled_at, source) VALUES (%s, %s::timestamptz + interval '1 day', 'test') RETURNING analysis_id",
                        (tank, last)).fetchone()[0]
    db.execute('RESET ROLE')
    row = db.execute("""SELECT changed_by, login_user FROM audit.change_log
                         WHERE table_name='lab.analysis' AND operation='INSERT' AND (row_pk->>'analysis_id')::bigint=%s""",
                     (new_id,)).fetchone()
    assert row == ('lab_test', db.execute('SELECT session_user').fetchone()[0])
    # The previous open-ended analysis was closed by the trigger, and that change is audited too.
    assert db.execute("SELECT count(*) FROM audit.change_log WHERE table_name='lab.analysis' AND operation='UPDATE' AND changed_by='lab_test'").fetchone()[0] == 1


def test_containment_survives_every_rejected_rewrite(db):
    before = db.execute(SHIPPED_LOTS).fetchone()[0]
    assert before > 0
    bad = db.execute('SELECT analysis_id FROM lab.v_out_of_spec_period ORDER BY valid_during LIMIT 1').fetchone()[0]
    attempts = [
        ('role_readonly', ['CREATE TEMP TABLE t (tank_id int, sampled_at timestamptz, valid_during tstzrange)',
                           'CREATE TRIGGER x BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION lab.set_analysis_validity()',
                           f"INSERT INTO t SELECT tank_id, sampled_at + interval '1 minute', NULL FROM lab.analysis WHERE analysis_id={bad}"]),
        ('role_lab', [f"UPDATE lab.analysis SET valid_during=tstzrange(sampled_at, sampled_at + interval '1 minute') WHERE analysis_id={bad}"]),
        ('role_ingest', [f'DELETE FROM lab.analysis WHERE analysis_id={bad}']),
        ('role_quality', ["UPDATE lab.parameter_limit SET lsl=0.8 WHERE parameter_code='NI'"]),
        ('role_quality', ["DELETE FROM lab.parameter_limit WHERE parameter_code='NI'"]),
    ]
    for role, statements in attempts:
        rejected(db, role, *statements)
    assert db.execute(SHIPPED_LOTS).fetchone()[0] == before


# ---------- F-05: missing, stale and incomplete evidence is explicit ----------

def test_simulated_line_has_no_unknown_exposure_and_cod_has_no_limit(db):
    assert db.execute('SELECT count(*) FROM trace.v_unknown_exposure').fetchone()[0] == 0
    statuses = dict(db.execute("SELECT DISTINCT parameter_code, status FROM trace.backward_trace((SELECT lot_id FROM trace.complaint ORDER BY complaint_id LIMIT 1)) WHERE status <> 'OK'").fetchall())
    assert statuses == {'NI': 'LOW', 'COD': 'NO_LIMIT'}


def test_dwell_without_analysis_is_reported_not_dropped(db):
    shipped = db.execute(SHIPPED_LOTS).fetchone()[0]
    first, template = db.execute('SELECT min(sampled_at), (SELECT min(lot_id) FROM trace.lot) FROM lab.analysis').fetchone()
    lot = plate_lot(db, first - timedelta(days=1), template)
    rows = db.execute('SELECT analysis_id, parameter_code, status FROM trace.backward_trace(%s)', (lot,)).fetchall()
    assert rows == [(None, None, 'NO_ANALYSIS')]
    assert db.execute('SELECT trace_relation, reasons FROM trace.v_unknown_exposure WHERE lot_id=%s', (lot,)).fetchone() == ('direct', ['NO_ANALYSIS'])
    assert db.execute(SHIPPED_LOTS).fetchone()[0] == shipped


def test_missing_parameter_is_reported(db):
    tank, last, template = db.execute('SELECT tank_id, max(sampled_at), (SELECT min(lot_id) FROM trace.lot) FROM lab.analysis GROUP BY tank_id LIMIT 1').fetchone()
    sample = last + timedelta(days=1)
    analysis = db.execute("INSERT INTO lab.analysis (tank_id, sampled_at, source) VALUES (%s,%s,'test') RETURNING analysis_id", (tank, sample)).fetchone()[0]
    db.execute("INSERT INTO lab.analysis_result VALUES (%s,'ZN',10,'titration')", (analysis,))
    lot = plate_lot(db, sample + timedelta(hours=2), template)
    statuses = dict(db.execute('SELECT parameter_code, status FROM trace.backward_trace(%s)', (lot,)).fetchall())
    required = {code for (code,) in db.execute("SELECT parameter_code FROM lab.parameter_limit WHERE process_code='ZNNI_ALK'")}
    assert statuses == {code: 'OK' if code == 'ZN' else 'MISSING' for code in required}
    assert db.execute('SELECT reasons FROM trace.v_unknown_exposure WHERE lot_id=%s', (lot,)).fetchone()[0] == ['MISSING']


def test_open_ended_analysis_becomes_stale_after_max_age(db):
    last, template = db.execute('SELECT max(sampled_at), (SELECT min(lot_id) FROM trace.lot) FROM lab.analysis').fetchone()
    max_age = db.execute("SELECT lab.max_analysis_age('ZNNI_ALK')").fetchone()[0]
    fresh = plate_lot(db, last + max_age - timedelta(hours=2), template)
    stale = plate_lot(db, last + max_age + timedelta(hours=1), template)
    assert 'STALE' not in {s for (s,) in db.execute('SELECT status FROM trace.backward_trace(%s)', (fresh,))}
    rows = db.execute("SELECT parameter_code, status, analysis_age FROM trace.backward_trace(%s) WHERE parameter_code IN ('NI','ZN')", (stale,)).fetchall()
    assert {status for _, status, _ in rows} == {'STALE'} and all(age > max_age for *_, age in rows)
    assert db.execute('SELECT reasons FROM trace.v_unknown_exposure WHERE lot_id=%s', (stale,)).fetchone()[0] == ['STALE']
    db.execute("INSERT INTO lab.analysis_max_age VALUES ('ZNNI_ALK', interval '30 days', 'test')")
    assert 'STALE' not in {s for (s,) in db.execute('SELECT status FROM trace.backward_trace(%s)', (stale,))}


# ---------- F-06 / F-21: recipients and piece counts ----------

def test_delivery_to_another_customer_is_rejected(db):
    lot, delivery = db.execute('SELECT lot_id, delivery_id FROM trace.delivery_lot ORDER BY delivery_id, lot_id LIMIT 1').fetchone()
    other = db.execute('SELECT d.delivery_id FROM trace.delivery d WHERE d.customer_id <> (SELECT customer_id FROM trace.delivery WHERE delivery_id=%s) LIMIT 1', (delivery,)).fetchone()[0]
    error = rejected(db, None, f'UPDATE trace.delivery_lot SET delivery_id={other} WHERE lot_id={lot} AND delivery_id={delivery}')
    assert isinstance(error, psycopg.errors.IntegrityConstraintViolation)


def test_containment_names_the_delivery_recipient(db):
    # Bypass the recipient rule (owner-only) to reproduce the audit's drop-shipment scenario.
    db.execute('ALTER TABLE trace.delivery_lot DISABLE TRIGGER delivery_lot_customer')
    lot, order_customer, note = db.execute("""SELECT lot_id, order_customer_code, delivery_no FROM trace.v_containment_detail
                                               WHERE delivery_no IS NOT NULL ORDER BY lot_no LIMIT 1""").fetchone()
    other_id, other = db.execute('SELECT customer_id, code FROM ref.customer WHERE code <> %s ORDER BY code LIMIT 1', (order_customer,)).fetchone()
    db.execute("INSERT INTO trace.delivery (customer_id, delivery_no, shipped_at) SELECT %s, 'TEST-X5', shipped_at FROM trace.delivery WHERE delivery_no=%s", (other_id, note))
    db.execute("""UPDATE trace.delivery_lot SET delivery_id=(SELECT delivery_id FROM trace.delivery WHERE delivery_no='TEST-X5')
                   WHERE lot_id=%s AND delivery_id=(SELECT delivery_id FROM trace.delivery WHERE delivery_no=%s)""", (lot, note))
    assert db.execute("SELECT customer_code, order_customer_code FROM trace.v_containment_detail WHERE delivery_no='TEST-X5'").fetchone() == (other, order_customer)
    assert 'TEST-X5' in db.execute('SELECT delivery_notes FROM trace.v_containment_report WHERE customer_code=%s', (other,)).fetchone()[0]
    assert 'TEST-X5' not in db.execute('SELECT delivery_notes FROM trace.v_containment_report WHERE customer_code=%s', (order_customer,)).fetchone()[0]


def test_rework_child_plated_in_bad_window_counts_its_own_pieces(db):
    window = db.execute('SELECT valid_during FROM lab.v_out_of_spec_period ORDER BY valid_during LIMIT 1').fetchone()[0]
    parent, customer, qty = db.execute("""SELECT l.lot_id, c.code, l.qty_pcs FROM trace.lot l JOIN trace.customer_order o USING (order_id)
                                           JOIN ref.customer c USING (customer_id)
                                          WHERE l.origin='receipt' AND l.status='shipped' AND l.qty_pcs >= 30
                                            AND l.lot_id NOT IN (SELECT lot_id FROM trace.v_containment_detail)
                                            AND NOT EXISTS (SELECT 1 FROM trace.lot c WHERE c.parent_lot_id=l.lot_id)
                                          ORDER BY l.lot_id LIMIT 1""").fetchone()
    before = db.execute('SELECT direct_lots, affected_pieces FROM trace.v_containment_report WHERE customer_code=%s', (customer,)).fetchone()
    child = plate_lot(db, window.lower + timedelta(hours=2), parent, qty=30, parent=True)
    db.execute('SET CONSTRAINTS ALL IMMEDIATE')
    assert db.execute('SELECT origin, trace_relation FROM trace.v_containment_detail WHERE lot_id=%s', (child,)).fetchone() == ('rework', 'direct')
    after = db.execute('SELECT direct_lots, affected_pieces FROM trace.v_containment_report WHERE customer_code=%s', (customer,)).fetchone()
    assert after == (before[0] + 1, before[1] + 30)


def test_affected_pieces_count_each_genealogy_once(db):
    expected = db.execute("""SELECT sum(lot_qty_pcs) FROM (SELECT DISTINCT lot_id, parent_lot_id, lot_qty_pcs FROM trace.v_containment_detail) d
                              WHERE parent_lot_id IS NULL OR parent_lot_id NOT IN (SELECT lot_id FROM trace.v_containment_detail)""").fetchone()[0]
    assert db.execute('SELECT sum(affected_pieces) FROM trace.v_containment_report').fetchone()[0] == expected
    shipped, lot_qty = db.execute("""SELECT sum(f.shipped_qty_pcs), sum(f.lot_qty_pcs) FROM lab.v_out_of_spec_period p
                                      CROSS JOIN LATERAL trace.forward_trace(p.tank_id, p.valid_during) f WHERE f.delivery_no IS NOT NULL""").fetchone()
    assert shipped is not None and lot_qty is not None


# ---------- F-22: audit rows show the real login ----------

def test_audit_row_stores_login_next_to_application_label(db):
    db.execute("SELECT set_config('app.user','someone_else',true)")
    db.execute('SET LOCAL ROLE role_quality')
    db.execute('UPDATE trace.measurement SET value=value+1 WHERE measurement_id=1')
    db.execute('RESET ROLE')
    row = db.execute("SELECT changed_by, login_user FROM audit.change_log WHERE table_name='trace.measurement' ORDER BY change_id DESC LIMIT 1").fetchone()
    assert row == ('someone_else', db.execute('SELECT session_user').fetchone()[0])


# ---------- F-23: genealogy and carrier-step rules ----------

def test_genealogy_cycle_is_rejected(db):
    child, parent = db.execute("SELECT lot_id, parent_lot_id FROM trace.lot WHERE origin='rework' ORDER BY lot_id LIMIT 1").fetchone()
    db.execute('SET CONSTRAINTS trace.lot_genealogy IMMEDIATE')
    error = rejected(db, None, f"UPDATE trace.lot SET parent_lot_id={child}, origin='rework' WHERE lot_id={parent}")
    assert 'own ancestor' in str(error)


@pytest.mark.parametrize('change,message', [
    ('qty_pcs = (SELECT qty_pcs + 1 FROM trace.lot p WHERE p.lot_id = lot.parent_lot_id)', 'more pieces'),
    ('part_id = (SELECT min(part_id) FROM ref.part WHERE part_id <> lot.part_id)', 'part and customer'),
])
def test_child_lot_must_fit_its_parent(db, change, message):
    child = db.execute("SELECT lot_id FROM trace.lot WHERE origin='rework' ORDER BY lot_id LIMIT 1").fetchone()[0]
    db.execute('SET CONSTRAINTS trace.lot_genealogy IMMEDIATE')
    error = rejected(db, None, f'UPDATE trace.lot SET {change} WHERE lot_id={child}')
    assert message in str(error)


def test_step_outside_its_carrier_run_is_rejected(db):
    error = rejected(db, 'role_operator',
                     "UPDATE trace.load_step SET in_station = tstzrange(lower(in_station) - interval '3000 days', upper(in_station) - interval '3000 days') WHERE step_id = (SELECT min(step_id) FROM trace.load_step)")
    assert isinstance(error, psycopg.errors.IntegrityConstraintViolation)


def test_deferred_recipient_check_ignores_a_row_deleted_before_commit(db):
    lot, delivery = db.execute('SELECT lot_id, delivery_id FROM trace.delivery_lot ORDER BY delivery_id, lot_id LIMIT 1').fetchone()
    other = db.execute('SELECT delivery_id FROM trace.delivery WHERE customer_id <> (SELECT customer_id FROM trace.delivery WHERE delivery_id=%s) ORDER BY delivery_id LIMIT 1', (delivery,)).fetchone()[0]
    db.execute('SET CONSTRAINTS ALL DEFERRED')
    db.execute('INSERT INTO trace.delivery_lot VALUES (%s,%s,1)', (other, lot))
    db.execute('DELETE FROM trace.delivery_lot WHERE delivery_id=%s AND lot_id=%s', (other, lot))
    db.execute('SET CONSTRAINTS ALL IMMEDIATE')   # runs the queued checks, as COMMIT would
    db.execute('SET CONSTRAINTS ALL DEFERRED')
    db.execute('INSERT INTO trace.delivery_lot VALUES (%s,%s,1)', (other, lot))
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        with db.transaction():
            db.execute('SET CONSTRAINTS ALL IMMEDIATE')


# ---------- limit versions: no retroactive close by quality ----------

def test_quality_cannot_close_a_limit_retroactively(db):
    limit_id = db.execute("SELECT limit_id FROM lab.parameter_limit WHERE parameter_code='NI' AND upper_inf(valid_during)").fetchone()[0]
    shipped = db.execute(SHIPPED_LOTS).fetchone()[0]
    error = rejected(db, 'role_quality',
                     f"UPDATE lab.parameter_limit SET valid_during = daterange(lower(valid_during), '2026-04-01') WHERE limit_id={limit_id}")
    assert 'before today' in str(error)
    assert db.execute(SHIPPED_LOTS).fetchone()[0] == shipped


@pytest.mark.parametrize('days_ahead', [0, 30])
def test_quality_can_close_a_limit_today_or_later(db, days_ahead):
    limit_id = db.execute("SELECT limit_id FROM lab.parameter_limit WHERE parameter_code='NI' AND upper_inf(valid_during)").fetchone()[0]
    db.execute('SET LOCAL ROLE role_quality')
    db.execute("""UPDATE lab.parameter_limit
                     SET valid_during = daterange(lower(valid_during), (now() AT TIME ZONE 'Europe/Berlin')::date + %s)
                   WHERE limit_id=%s""", (days_ahead, limit_id))
    db.execute('RESET ROLE')
    assert not db.execute('SELECT upper_inf(valid_during) FROM lab.parameter_limit WHERE limit_id=%s', (limit_id,)).fetchone()[0]
