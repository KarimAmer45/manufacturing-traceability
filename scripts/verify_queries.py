"""Execute every showcase query with a timeout and write an execution manifest."""
import json
import pathlib
import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ingest'))
from common import connect

results = {}
with connect('verify_queries') as conn:
    conn.execute("SET LOCAL statement_timeout='120s'")
    for path in sorted((ROOT / 'sql/queries').glob('*.sql')):
        cur = conn.execute(path.read_text(encoding='utf-8'))
        counts = []
        while True:
            if cur.description:
                counts.append(len(cur.fetchall()))
            if not cur.nextset():
                break
        results[path.name] = counts
out = ROOT / 'docs/results/query_validation.json'
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(results, indent=2), encoding='utf-8')
print(json.dumps(results, indent=2))
