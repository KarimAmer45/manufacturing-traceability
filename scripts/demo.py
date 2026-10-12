"""Run the demo on an empty migrated database, then export reproducible evidence."""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ingest'))
from common import connect


def main():
    with connect('demo') as conn:
        populated = conn.execute('SELECT EXISTS(SELECT 1 FROM ref.process)').fetchone()[0]
    if not populated:
        subprocess.run([sys.executable, str(ROOT / 'ingest/p1_simulate_plant.py')], check=True)
    else:
        print('Using existing data; simulator skipped.')
    subprocess.run([sys.executable, '-m', 'pytest', '-q', str(ROOT / 'tests')], check=True)
    subprocess.run([sys.executable, str(ROOT / 'scripts/audit_demo.py')], check=True)
    subprocess.run([sys.executable, str(ROOT / 'scripts/export_results.py')], check=True)


if __name__ == '__main__':
    main()
