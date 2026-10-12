"""Build a throwaway database with another seed and start date and check that tracing finds the
planted incident there too.

The checks are relative to the incident the simulator plants (computed from config/sources.yaml),
not to the demo's fixed counts: every planted analysis is out of limits, the complaint lot traces
back to one of them, and every lot plated while a planted analysis was in force is contained.
Other out-of-limit periods caused by random process spread are reported, not treated as errors.

Usage: python scripts/verify_second_seed.py --seed 7 --start 2026-01-05 [--name trace_seed_7] [--keep]
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ingest'))
from common import sources
from p1_simulate_plant import working_days

TZ = ZoneInfo('Europe/Berlin')


def planted_window(start, weeks):
    config = sources()
    sim = config['assumptions']['simulation']['value']
    days = list(working_days(start, weeks, config['plant']['working_days_per_week']['value']))
    day = days[len(days) // sim['incident_position_divisor']]
    sample = time(sim['sample_hour'])
    return (datetime.combine(day, sample, TZ),
            datetime.combine(day + timedelta(days=sim['incident_days']), sample, TZ))


def check(conn, window):
    planted = conn.execute("""SELECT a.analysis_id, r.status FROM lab.analysis a JOIN lab.v_result_status r USING (analysis_id)
                               WHERE r.parameter_code='NI' AND a.sampled_at >= %s AND a.sampled_at < %s ORDER BY 1""", window).fetchall()
    flagged = {aid for (aid,) in conn.execute('SELECT analysis_id FROM lab.v_out_of_spec_period')}
    complaint = conn.execute('SELECT c.lot_id, l.lot_no FROM trace.complaint c JOIN trace.lot l USING (lot_id) ORDER BY complaint_id LIMIT 1').fetchone()
    trace_hits = conn.execute("SELECT DISTINCT analysis_id FROM trace.backward_trace(%s) WHERE parameter_code='NI' AND status='LOW'",
                              (complaint[0],)).fetchall() if complaint else []
    exposed = {lot for (lot,) in conn.execute("""SELECT DISTINCT ll.lot_id FROM trace.load_step st JOIN ref.station s USING (station_id)
                                                  JOIN lab.analysis a ON a.tank_id = s.tank_id AND a.valid_during && st.in_station
                                                  JOIN trace.load_lot ll ON ll.load_id = st.load_id
                                                 WHERE a.analysis_id = ANY(%s)""", ([aid for aid, _ in planted],))}
    contained = {lot for (lot,) in conn.execute('SELECT DISTINCT lot_id FROM trace.v_containment_detail')}
    report = conn.execute('SELECT coalesce(sum(shipped_lots),0)::bigint, coalesce(sum(shipped_pieces),0)::bigint, count(*) FILTER (WHERE shipped_lots > 0) FROM trace.v_containment_report').fetchone()
    result = {
        'planted_window': [w.isoformat() for w in window],
        'planted_analyses': [aid for aid, _ in planted],
        'planted_ni_status': sorted({status for _, status in planted}),
        'other_out_of_limit_analyses': sorted(flagged - {aid for aid, _ in planted}),
        'complaint_lot': complaint[1] if complaint else None,
        'complaint_traces_to_planted_analysis': bool({aid for (aid,) in trace_hits} & {aid for aid, _ in planted}),
        'lots_plated_under_planted_analyses': len(exposed),
        'of_which_contained': len(exposed & contained),
        'shipped_lots_in_containment': report[0], 'shipped_pieces_in_containment': report[1],
        'customers_with_shipments': report[2],
        'unknown_exposure_rows': conn.execute('SELECT count(*) FROM trace.v_unknown_exposure').fetchone()[0],
    }
    failures = []
    if not planted or result['planted_ni_status'] != ['LOW'] or not {aid for aid, _ in planted} <= flagged:
        failures.append('planted analyses are not all flagged LOW')
    if not result['complaint_traces_to_planted_analysis']:
        failures.append('complaint lot does not trace back to a planted analysis')
    if not exposed or exposed - contained:
        failures.append('lots plated under planted analyses are missing from containment')
    if not report[0]:
        failures.append('containment lists no shipped lots')
    return result, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--start', type=date.fromisoformat, default=date(2026, 1, 5))
    parser.add_argument('--weeks', type=int, default=26)
    parser.add_argument('--name', help='throwaway database name (default trace_seed_<seed>)')
    parser.add_argument('--keep', action='store_true', help='keep the database for inspection')
    args = parser.parse_args()
    name = args.name or f'trace_seed_{args.seed}'

    base = os.environ.get('DATABASE_URL', '')
    admin = dict(conninfo_to_dict(make_conninfo(base)), dbname='postgres')
    target_url = make_conninfo(base, dbname=name)
    env = dict(os.environ, DATABASE_URL=target_url, PGDATABASE=name)
    with psycopg.connect(**admin, autocommit=True) as conn:
        if conn.execute('SELECT 1 FROM pg_database WHERE datname=%s', (name,)).fetchone():
            raise SystemExit(f'Database {name} already exists; choose another --name.')
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        subprocess.run([sys.executable, str(ROOT / 'scripts/migrate.py')], env=env, check=True, stdout=subprocess.DEVNULL)
        subprocess.run([sys.executable, str(ROOT / 'ingest/p1_simulate_plant.py'), '--seed', str(args.seed),
                        '--start', args.start.isoformat(), '--weeks', str(args.weeks)], env=env, check=True)
        with psycopg.connect(target_url) as conn:
            conn.execute('ANALYZE')
            result, failures = check(conn, planted_window(args.start, args.weeks))
        result = {'seed': args.seed, 'start': args.start.isoformat(), 'weeks': args.weeks, **result,
                  'passed': not failures, 'failures': failures}
        print(json.dumps(result, indent=2))
    finally:
        if not args.keep:
            with psycopg.connect(**admin, autocommit=True) as conn:
                conn.execute(sql.SQL('DROP DATABASE IF EXISTS {}').format(sql.Identifier(name)))
    out = ROOT / 'docs/results/second_seed.json'
    out.write_text(json.dumps(result, indent=2), encoding='utf-8')
    if failures:
        raise SystemExit('Second-seed check failed: ' + '; '.join(failures))


if __name__ == '__main__':
    main()
