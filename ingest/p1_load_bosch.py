"""Atomic, idempotent loader for the official Bosch CSV files.
Download through Kaggle after accepting its competition rules. Never commit raw data.
"""
import argparse
import json
import re
import warnings
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
from common import ROOT, connect, sources

DATE_COL = re.compile(r'^L(\d+)_S(\d+)_D\d+$')

def load_bosch(cur, directory, max_rows=None, chunk_size=20_000):
    if chunk_size < 1 or (max_rows is not None and max_rows < 1):
        raise ValueError('Row and chunk limits must be positive')
    directory = Path(directory)
    responses = pd.read_csv(directory / 'train_numeric.csv', usecols=['Id', 'Response'], nrows=max_rows,
                            dtype={'Id': 'int64', 'Response': 'int64'})
    if responses.empty or responses.Id.duplicated().any() or not responses.Response.isin([0, 1]).all():
        raise ValueError('Response file needs unique IDs and binary outcomes')
    header = pd.read_csv(directory / 'train_date.csv', nrows=0).columns
    groups = defaultdict(list)
    for col in header:
        match = DATE_COL.fullmatch(col)
        if match:
            groups[f'L{match[1]}_S{match[2]}'].append(col)
    if not groups or 'Id' not in header:
        raise ValueError('Date CSV has no recognised station timestamp columns')
    cur.execute('CREATE TEMP TABLE bosch_import_part (part_id bigint PRIMARY KEY, response smallint) ON COMMIT DROP')
    with cur.copy('COPY bosch_import_part FROM STDIN (FORMAT csv)') as cp:
        cp.write(responses.to_csv(index=False, header=False))
    cur.execute('INSERT INTO bosch.part SELECT * FROM bosch_import_part ON CONFLICT(part_id) DO UPDATE SET response=EXCLUDED.response')
    cur.executemany('INSERT INTO bosch.station VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',
                    [(st, int(st.split('_')[0][1:]), int(st.split('_')[1][1:])) for st in groups])
    cur.execute('DELETE FROM bosch.station_pass s USING bosch_import_part p WHERE s.part_id=p.part_id')
    cur.execute('CREATE TEMP TABLE bosch_seen_id (part_id bigint PRIMARY KEY REFERENCES bosch_import_part) ON COMMIT DROP')
    dtypes = {c: 'float64' for cols in groups.values() for c in cols} | {'Id': 'int64'}
    passes = date_rows = 0
    for chunk in pd.read_csv(directory / 'train_date.csv', dtype=dtypes, chunksize=chunk_size,
                             nrows=max_rows, usecols=['Id', *[c for cols in groups.values() for c in cols]]):
        ids = chunk.Id.to_numpy()
        # Reject mismatched or duplicate IDs with PK/FK constraints.
        with cur.copy('COPY bosch_seen_id FROM STDIN (FORMAT csv)') as cp:
            cp.write(chunk[['Id']].to_csv(index=False, header=False))
        date_rows += len(chunk)
        for station, columns in groups.items():
            block = chunk[columns]
            first, last = block.min(axis=1).to_numpy(), block.max(axis=1).to_numpy()
            seen = ~np.isnan(first)
            if not (np.isfinite(first[seen]).all() and np.isfinite(last[seen]).all()):
                raise ValueError('Non-finite timestamps')
            frame = pd.DataFrame({'id': ids[seen], 'station': station,
                                  'first': first[seen].round(2), 'last': last[seen].round(2)})
            with cur.copy('COPY bosch.station_pass FROM STDIN (FORMAT csv)') as cp:
                cp.write(frame.to_csv(index=False, header=False))
            passes += len(frame)
    if date_rows != len(responses):
        raise ValueError(f'Mismatched row counts: numeric={len(responses)}, date={date_rows}')
    cur.execute('ANALYZE bosch.part; ANALYZE bosch.station_pass')
    return {'input_parts': len(responses), 'input_failures': int(responses.Response.sum()),
            'input_failure_pct': float(responses.Response.mean() * 100), 'station_passes': passes,
            'stations_in_header': len(groups), 'date_columns': sum(map(len, groups.values())),
            'sample_limit': max_rows, 'loaded_at': datetime.now(timezone.utc).isoformat()}

def compare_with_published(result, reference=None):
    """Deviation of a full import from the published dataset size and failure rate.

    Warns instead of failing: a mismatch points at a truncated or wrong download, not at bad rows.
    """
    ref = reference or sources()['bosch_published']
    comparison = {'published_parts_approx': ref['train_parts_approx'], 'published_failure_pct_approx': ref['failure_pct_approx'],
                  'source': ref['src'], 'warnings': []}
    if result['sample_limit'] is not None:
        comparison['note'] = 'Sample import; totals are not comparable with the published dataset.'
        return comparison
    parts_dev = 100 * (result['input_parts'] - ref['train_parts_approx']) / ref['train_parts_approx']
    failure_dev = result['input_failure_pct'] - ref['failure_pct_approx']
    comparison['parts_deviation_pct'] = round(parts_dev, 3)
    comparison['failure_pct_deviation_points'] = round(failure_dev, 4)
    if abs(parts_dev) > ref['parts_tolerance_pct']:
        comparison['warnings'].append(f"{result['input_parts']} parts deviate {parts_dev:+.2f} % from the published ~{ref['train_parts_approx']:,}")
    if abs(failure_dev) > ref['failure_tolerance_points']:
        comparison['warnings'].append(f"failure rate {result['input_failure_pct']:.3f} % deviates {failure_dev:+.3f} points from the published ~{ref['failure_pct_approx']} %")
    for message in comparison['warnings']:
        warnings.warn(message, stacklevel=2)
    return comparison

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', type=Path, default=ROOT / 'data/raw/bosch')
    parser.add_argument('--max-rows', type=int)
    parser.add_argument('--chunk', type=int, default=20_000)
    args = parser.parse_args()
    with connect('load_bosch') as conn:
        with conn.cursor() as cur:
            result = load_bosch(cur, args.dir, args.max_rows, args.chunk)
    result['published_comparison'] = compare_with_published(result)
    out = ROOT / 'docs/results'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'bosch_import.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
