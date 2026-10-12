import psycopg
import pytest
from common import sources

def test_complaint_reaches_bad_bath(db):
    lot = db.execute('SELECT lot_id FROM trace.complaint ORDER BY complaint_id LIMIT 1').fetchone()
    assert lot, 'Run the simulator first'
    rows = db.execute("SELECT parameter_code,status FROM trace.backward_trace(%s) WHERE status IN ('LOW','HIGH')", lot).fetchall()
    assert ('NI', 'LOW') in rows

def test_forward_trace_contains_complaint_and_deliveries(db):
    assert not db.execute('SELECT c.lot_id FROM trace.complaint c LEFT JOIN trace.v_containment_detail d USING(lot_id) WHERE d.lot_id IS NULL').fetchall()
    assert db.execute('SELECT sum(shipped_pieces) FROM trace.v_containment_report').fetchone()[0] > 0

def test_containment_does_not_double_count_shipments(db):
    actual = db.execute('SELECT sum(shipped_pieces) FROM trace.v_containment_report').fetchone()[0]
    expected = db.execute('SELECT sum(dl.qty_pcs) FROM trace.delivery_lot dl WHERE dl.lot_id IN (SELECT lot_id FROM trace.v_containment_detail)').fetchone()[0]
    assert actual == expected

def test_station_overlap_is_rejected(db):
    with pytest.raises(psycopg.errors.ExclusionViolation):
        db.execute('INSERT INTO trace.load_step(load_id,station_id,in_station) SELECT load_id,station_id,in_station FROM trace.load_step LIMIT 1')

def test_unbounded_station_interval_is_rejected(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO trace.load_step(load_id,station_id,in_station) SELECT load_id,station_id,tstzrange(NULL,'2000-01-01') FROM trace.load_step LIMIT 1")

def test_allocations_balance_and_barrels_respect_capacity(db):
    assert not db.execute("SELECT l.lot_id FROM trace.lot l LEFT JOIN trace.load_lot ll USING(lot_id) WHERE l.origin='receipt' GROUP BY l.lot_id HAVING l.qty_pcs <> coalesce(sum(ll.qty_pcs),0)").fetchall()
    capacity = sources()['assumptions']['barrel_load_kg']['value'] * 1000
    assert not db.execute('SELECT ll.load_id FROM trace.load_lot ll JOIN trace.lot l USING(lot_id) JOIN ref.part p USING(part_id) GROUP BY ll.load_id HAVING count(DISTINCT l.part_id)>1 OR sum(ll.qty_pcs*p.mass_g)>%s', (capacity,)).fetchall()

def test_physical_barrels_do_not_overlap(db):
    assert not db.execute("SELECT a.load_id FROM trace.load a JOIN trace.load b ON a.carrier_no=b.carrier_no AND a.load_id<b.load_id AND tstzrange(a.started_at,a.finished_at) && tstzrange(b.started_at,b.finished_at)").fetchall()

def test_every_plating_step_has_complete_analysis_coverage(db):
    assert not db.execute("SELECT st.step_id FROM trace.load_step st JOIN ref.station s USING(station_id) WHERE NOT st.in_station <@ (SELECT range_agg(a.valid_during) FROM lab.analysis a WHERE a.tank_id=s.tank_id)").fetchall()

def test_rework_is_not_released_without_processing(db):
    assert db.execute("SELECT count(*) FROM trace.lot WHERE origin='rework'").fetchone()[0] > 0
    assert not db.execute("SELECT lot_id FROM trace.lot WHERE origin='rework' AND status IN ('released','shipped')").fetchall()

def test_backfilled_analysis_splits_range_and_duplicate_is_safe(db):
    tank, start = db.execute('SELECT tank_id,min(sampled_at) FROM lab.analysis GROUP BY tank_id LIMIT 1').fetchone()
    inserted = db.execute("INSERT INTO lab.analysis(tank_id,sampled_at,source) VALUES(%s,%s::timestamptz + interval '1 hour','test') RETURNING analysis_id,sampled_at", (tank, start)).fetchone()
    before = db.execute('SELECT analysis_id,valid_during FROM lab.analysis WHERE tank_id=%s ORDER BY sampled_at', (tank,)).fetchall()
    db.execute("INSERT INTO lab.analysis(tank_id,sampled_at,source) VALUES(%s,%s,'test') ON CONFLICT(tank_id,sampled_at) DO NOTHING", (tank, inserted[1]))
    assert before == db.execute('SELECT analysis_id,valid_during FROM lab.analysis WHERE tank_id=%s ORDER BY sampled_at', (tank,)).fetchall()
    assert db.execute('SELECT upper(valid_during) FROM lab.analysis WHERE tank_id=%s AND sampled_at=%s', (tank, start)).fetchone()[0] == inserted[1]

def test_lab_can_insert_analysis_but_cannot_release_lot(db):
    tank = db.execute('SELECT tank_id FROM lab.analysis LIMIT 1').fetchone()[0]
    db.execute('SET LOCAL ROLE role_lab')
    db.execute("INSERT INTO lab.analysis(tank_id,sampled_at,source) VALUES(%s,'2030-01-01T06:00:00+01:00','test')", (tank,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("UPDATE trace.lot SET status='released' WHERE lot_id=1")

def test_readonly_cannot_change_measurements(db):
    db.execute('SET LOCAL ROLE role_readonly')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute('UPDATE trace.measurement SET value=value+1 WHERE measurement_id=1')

def test_quality_correction_is_audited(db):
    db.execute("SELECT set_config('app.user','quality_test',true)")
    db.execute('SET LOCAL ROLE role_quality')
    before = db.execute('SELECT value FROM trace.measurement WHERE measurement_id=1').fetchone()[0]
    db.execute('UPDATE trace.measurement SET value=value+1 WHERE measurement_id=1')
    row = db.execute("SELECT changed_by,(old_row->>'value')::numeric,(new_row->>'value')::numeric FROM audit.change_log WHERE table_name='trace.measurement' AND operation='UPDATE' ORDER BY change_id DESC LIMIT 1").fetchone()
    assert row == ('quality_test', before, before + 1)

def test_family_handles_cycle_without_looping(db):
    child, parent = db.execute("SELECT lot_id,parent_lot_id FROM trace.lot WHERE origin='rework' LIMIT 1").fetchone()
    db.execute("UPDATE trace.lot SET parent_lot_id=%s,origin='rework' WHERE lot_id=%s", (child, parent))
    db.execute("SET LOCAL statement_timeout='2s'")
    rows = db.execute('SELECT * FROM trace.lot_family(%s)', (child,)).fetchall()
    assert {row[0] for row in rows} == {parent, child}
