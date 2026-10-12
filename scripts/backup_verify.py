"""Dump and restore to a new, uniquely named database; compare every project table.

Each table is compared by row count and a content hash (an order-independent sum of SHA-256
hashes of the rows' text form), plus the complaint trace.

Same server (default): the restore keeps object owners, because they already exist there.
Fresh server (--target-url): the five NOLOGIN group roles are created first (the GRANTs in the
dump refer to them) and pg_restore runs with --no-owner, so the restoring login owns the objects.

Requires pg_dump/pg_restore on PATH, PG_BIN, or the optional local portable runtime.
Leaves both the backup and restored database available for inspection.
"""
import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ingest'))
from common import connect

GROUP_ROLES = ('role_readonly', 'role_operator', 'role_lab', 'role_quality', 'role_ingest')
HASH_MODULUS = 2 ** 256


def executable(name):
    suffix = '.exe' if os.name == 'nt' else ''
    candidates = [pathlib.Path(os.environ.get('PG_BIN', '.')) / (name + suffix),
                  ROOT / '.local/postgres/pgsql/bin' / (name + suffix)]
    for path in candidates:
        if path.is_file():
            return str(path.resolve())
    found = shutil.which(name)
    if not found:
        raise SystemExit(f'{name} not found. Install PostgreSQL client tools or set PG_BIN.')
    return found


def table_digest(conn, schema, table):
    """Row count and order-independent content hash of one table, streamed with COPY."""
    total, rows, rest = 0, 0, b''
    with conn.cursor().copy(sql.SQL('COPY {} TO STDOUT').format(sql.Identifier(schema, table))) as copy:
        for block in copy:
            *lines, rest = (rest + bytes(block)).split(b'\n')
            for line in lines:
                total = (total + int.from_bytes(hashlib.sha256(line).digest(), 'big')) % HASH_MODULUS
            rows += len(lines)
    return {'rows': rows, 'sha256_sum': f'{total:064x}'}


def table_snapshot(conn):
    # Fixed output settings, so equal values always produce equal text on both sides.
    for setting in ("SET TimeZone='UTC'", "SET DateStyle='ISO, YMD'", "SET IntervalStyle='postgres'", 'SET extra_float_digits=3'):
        conn.execute(setting)
    tables = conn.execute("SELECT schemaname,tablename FROM pg_tables WHERE schemaname IN ('ref','lab','trace','bosch','audit','public') ORDER BY 1,2").fetchall()
    return {f'{schema}.{table}': table_digest(conn, schema, table) for schema, table in tables}


def libpq_env(base, params, password):
    env = dict(base)
    for key, variable in [('host', 'PGHOST'), ('port', 'PGPORT'), ('user', 'PGUSER'), ('dbname', 'PGDATABASE')]:
        if params.get(key):
            env[variable] = str(params[key])
    if password:
        env['PGPASSWORD'] = password  # child environment only, never arguments or reports
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target-url', help='maintenance database URL of another (fresh) server, e.g. postgresql://user@host:5434/postgres')
    parser.add_argument('--target-name', help='name of the database to create for the restore (default: trace_restore_<random>)')
    args = parser.parse_args()

    started = time.perf_counter()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = args.target_name or 'trace_restore_' + uuid.uuid4().hex[:10]
    backup = ROOT / 'backups' / f'traceability_{stamp}.dump'
    backup.parent.mkdir(exist_ok=True)
    with connect('backup_verify') as conn:
        params = conn.info.get_parameters()
        password = conninfo_to_dict(os.environ.get('DATABASE_URL', '')).get('password', os.environ.get('PGPASSWORD', ''))
        source_env = libpq_env(os.environ, params, password)
        original = table_snapshot(conn)
    subprocess.run([executable('pg_dump'), '-Fc', '-f', str(backup)], env=source_env, check=True)

    if args.target_url:
        target_params = conninfo_to_dict(args.target_url)
        target_password = target_params.pop('password', None) or os.environ.get('PGPASSWORD', '')
        target_server = f"{target_params.get('host', 'localhost')}:{target_params.get('port', 5432)}"
    else:
        target_params = {k: v for k, v in params.items() if k in ('host', 'port', 'user')}
        target_password = password
        target_server = 'same server as the source'
    admin = dict(target_params, dbname=target_params.get('dbname') or 'postgres')
    if target_password:
        admin['password'] = target_password
    with psycopg.connect(**admin, autocommit=True) as conn:
        if args.target_url:
            for role in GROUP_ROLES:
                if not conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,)).fetchone():
                    conn.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(target)))
    restore_cmd = [executable('pg_restore'), '--exit-on-error', '-d', target, str(backup)]
    if args.target_url:
        restore_cmd.insert(1, '--no-owner')
    restore_start = time.perf_counter()
    subprocess.run(restore_cmd, env=libpq_env(os.environ, dict(admin, dbname=target), target_password), check=True)
    restore_seconds = time.perf_counter() - restore_start
    with psycopg.connect(**dict(admin, dbname=target)) as conn:
        restored = table_snapshot(conn)
        complaint_hits = conn.execute("SELECT count(*) FROM trace.backward_trace((SELECT lot_id FROM trace.complaint LIMIT 1)) WHERE status='LOW'").fetchone()[0]
    counts_match = {k: v['rows'] for k, v in original.items()} == {k: v['rows'] for k, v in restored.items()}
    hashes_match = original == restored
    report = {'run_at': datetime.now().astimezone().isoformat(timespec='seconds'),
              'source_database': params['dbname'], 'target_server': target_server,
              'restored_database': target, 'restore_options': restore_cmd[1:-3],
              'restore_seconds': round(restore_seconds, 3), 'total_seconds': round(time.perf_counter() - started, 3),
              'backup_bytes': backup.stat().st_size, 'table_counts_match': counts_match,
              'table_hashes_match': hashes_match, 'complaint_low_results': complaint_hits,
              'tables': original,
              'note': 'Run while no other writes are occurring. Every table is compared by row count and content hash; the complaint trace is re-run on the restored database.'}
    if not hashes_match or complaint_hits == 0:
        mismatched = sorted(k for k in set(original) | set(restored) if original.get(k) != restored.get(k))
        raise RuntimeError(f'Restore validation failed: {mismatched or "complaint trace returned no LOW rows"}')
    out = ROOT / 'docs/results/backup_restore.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
