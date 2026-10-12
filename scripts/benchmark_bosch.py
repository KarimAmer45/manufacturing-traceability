"""Save EXPLAIN ANALYZE plans. Experimental index is rolled back.

Both queries have a total ORDER BY, so the runs before and after the index return the same rows
and time the same work; a hash of each phase's result proves it.
"""
import hashlib
import json
import pathlib
import statistics
import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ingest'))
from common import connect

QUERY = """SELECT f.part_id AS failed_part,n.part_id AS neighbour,n.station_code,n.t_first
FROM bosch.station_pass f JOIN bosch.part fp ON fp.part_id=f.part_id AND fp.response=1
JOIN bosch.station_pass n ON n.station_code=f.station_code AND n.part_id<>f.part_id
AND n.t_first BETWEEN f.t_first-0.05 AND f.t_first+0.05
WHERE f.station_code='L3_S32' ORDER BY f.part_id,n.part_id LIMIT 100"""

QUERIES = {'forward_trace': QUERY, 'station_failure_rates': '''SELECT sp.station_code,count(*) AS parts,
sum(p.response) AS failed,round(100.0*avg(p.response),2) AS fail_pct
FROM bosch.station_pass sp JOIN bosch.part p USING(part_id)
GROUP BY sp.station_code HAVING count(*)>10000 ORDER BY fail_pct DESC,sp.station_code'''}

def main():
    conn = connect('benchmark')
    try:
        count = conn.execute('SELECT count(*) FROM bosch.part').fetchone()[0]
        if not count:
            raise SystemExit('Bosch is empty. Load the official CSVs first; no benchmark was fabricated.')
        if conn.execute("SELECT to_regclass('bosch.station_pass_station_time_idx')").fetchone()[0]:
            raise SystemExit('Experiment index already exists. Use a separate database for a true baseline.')
        conn.execute("SET LOCAL statement_timeout='300s'")
        conn.execute('ANALYZE bosch.station_pass; ANALYZE bosch.part')
        result = {'parts': count, 'queries': QUERIES, 'note': 'Three runs per phase; before precedes after; no forced index use. Deterministic ORDER BY, identical results checked by hash. The station aggregate may not benefit from the time index.'}
        for phase in ('before', 'after'):
            if phase == 'after':
                conn.execute('CREATE INDEX station_pass_station_time_idx ON bosch.station_pass(station_code,t_first)')
                conn.execute('ANALYZE bosch.station_pass')
            result[phase] = {}
            for name, query in QUERIES.items():
                plans = [conn.execute('EXPLAIN(ANALYZE,BUFFERS,FORMAT JSON) ' + query).fetchone()[0][0] for _ in range(3)]
                rows = conn.execute(query).fetchall()
                result[phase][name] = {'median_ms': statistics.median(p['Execution Time'] for p in plans),
                                       'rows': len(rows), 'result_md5': hashlib.md5(repr(rows).encode()).hexdigest(),
                                       'plans': plans}
        result['identical_results'] = {name: result['before'][name]['result_md5'] == result['after'][name]['result_md5'] for name in QUERIES}
        if not all(result['identical_results'].values()):
            raise SystemExit(f"Before and after results differ: {result['identical_results']}")
        out = ROOT / 'docs/results/bosch_benchmark.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(out)
    finally:
        conn.rollback()
        conn.close()

if __name__ == '__main__':
    main()
