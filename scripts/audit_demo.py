"""Record a reversible demonstration correction, leaving the original value intact."""
import pathlib
import sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ingest'))
from common import connect

with connect('quality_demo_correction') as conn:
    if not conn.execute("SELECT 1 FROM audit.change_log WHERE changed_by='quality_demo_correction' AND operation='UPDATE' LIMIT 1").fetchone():
        original = conn.execute('SELECT value FROM trace.measurement WHERE measurement_id=1 FOR UPDATE').fetchone()[0]
        conn.execute('SET LOCAL ROLE role_quality')
        conn.execute('UPDATE trace.measurement SET value=value+1 WHERE measurement_id=1')
        conn.execute('UPDATE trace.measurement SET value=%s WHERE measurement_id=1', (original,))
        print('Two audit entries recorded; measurement restored to its original value.')
    else:
        print('Audit demonstration already exists.')
