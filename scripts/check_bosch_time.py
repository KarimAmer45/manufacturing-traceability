"""Check the published 16.75-tick weekly-cycle hypothesis on observed part arrivals.

This is exploratory autocorrelation, not proof of a calendar conversion.
"""
import json
import pathlib
import sys
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ingest'))
from common import connect

def main():
    bin_ticks = 0.25  # analytical resolution, not a physical process parameter
    with connect('bosch_time_check') as conn:
        rows = conn.execute('''SELECT floor(first_tick / %s)::int AS bin, count(*)
          FROM (SELECT part_id,min(t_first) AS first_tick FROM bosch.station_pass GROUP BY part_id) p
          GROUP BY 1 ORDER BY 1''', (bin_ticks,)).fetchall()
    if not rows:
        raise SystemExit('Bosch is empty; no empirical time conversion is claimed.')
    start, end = rows[0][0], rows[-1][0]
    values = np.zeros(end - start + 1)
    for bucket, count in rows:
        values[bucket - start] = count
    candidates = []
    for lag in range(56, 81):
        if len(values) <= lag + 2:
            continue
        left, right = values[:-lag], values[lag:]
        if left.std() == 0 or right.std() == 0:
            continue
        candidates.append({'lag_ticks': lag * bin_ticks,
                           'correlation': float(np.corrcoef(left, right)[0, 1])})
    report = {'source': 'https://arxiv.org/abs/1701.00705', 'weekly_ticks_hypothesis': 16.75,
              'relative_ticks_per_day': 16.75 / 7, 'bin_ticks': bin_ticks,
              'observed_first_tick_bin': start, 'observed_last_tick_bin': end,
              'candidate_lags': candidates,
              'strongest_in_search_window': max(candidates, key=lambda x:x['correlation']) if candidates else None,
              'interpretation': 'Exploratory part-arrival autocorrelation, with empty bins set to zero. A peak can support periodicity, but does not identify a calendar origin or prove that the cycle is weekly.'}
    out = ROOT / 'docs/results/bosch_time_check.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(out)

if __name__ == '__main__':
    main()
