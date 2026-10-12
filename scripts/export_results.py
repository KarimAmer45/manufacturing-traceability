"""Export actual query output and a self-contained HTML evidence page."""
import csv
import html
import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ingest'))
from common import connect, sources

QUERIES = {
    'counts': "SELECT 'lots' AS item, count(*) AS total FROM trace.lot UNION ALL SELECT 'carriers',count(*) FROM trace.load UNION ALL SELECT 'measurements',count(*) FROM trace.measurement UNION ALL SELECT 'analyses',count(*) FROM lab.analysis UNION ALL SELECT 'complaints',count(*) FROM trace.complaint UNION ALL SELECT 'bosch_parts',count(*) FROM bosch.part",
    'bad_bath_periods': 'SELECT * FROM lab.v_out_of_spec_period ORDER BY valid_during',
    'complaint_backward_trace': "SELECT * FROM trace.backward_trace((SELECT lot_id FROM trace.complaint ORDER BY complaint_id LIMIT 1)) WHERE status <> 'OK'",
    'containment': 'SELECT customer_code, affected_lots, direct_lots, descendant_lots, affected_pieces, shipped_lots, shipped_pieces, delivery_notes FROM trace.v_containment_report ORDER BY customer_code',
    'unknown_exposure': 'SELECT lot_no, lot_status, trace_relation, reasons, customer_code, order_customer_code, delivery_no, shipped_qty_pcs, lot_qty_pcs FROM trace.v_unknown_exposure ORDER BY customer_code, lot_no',
    'genealogy': "SELECT * FROM trace.lot_family((SELECT lot_id FROM trace.lot WHERE origin='rework' ORDER BY lot_id LIMIT 1))",
    'audit_corrections': "SELECT changed_at,login_user,changed_by,row_pk,old_row->>'value' AS old_value,new_row->>'value' AS new_value FROM audit.change_log WHERE table_name='trace.measurement' AND operation='UPDATE' ORDER BY changed_at",
    'bosch_paths': 'SELECT * FROM bosch.v_top_paths',
}


def main():
    out = ROOT / 'docs/results'
    out.mkdir(parents=True, exist_ok=True)
    result = {'generated_at': datetime.now(timezone.utc).isoformat(), 'tables': {}}
    sections = []
    with connect('export_results') as conn:
        result['postgres_version'] = conn.execute('SELECT version()').fetchone()[0]
        for name, sql in QUERIES.items():
            cur = conn.execute(sql)
            columns = [c.name for c in cur.description]
            rows = cur.fetchall()
            result['tables'][name] = [dict(zip(columns, row)) for row in rows]
            with (out / f'{name}.csv').open('w', encoding='utf-8', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(columns)
                writer.writerows(rows)
            head = ''.join(f'<th>{html.escape(c)}</th>' for c in columns)
            body = ''.join('<tr>' + ''.join(f'<td>{html.escape(str(v))}</td>' for v in row) + '</tr>' for row in rows)
            sections.append(f'<section><h2>{name.replace("_", " ").title()}</h2><div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></section>')
    (out / 'summary.json').write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')
    assumptions = sources()['assumptions']
    (out / 'assumptions.json').write_text(json.dumps(assumptions, indent=2), encoding='utf-8')
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Manufacturing traceability — demo results</title><style>body{font:15px system-ui;background:#f1f5f8;color:#172b3a;margin:0}main{max-width:1200px;margin:auto;padding:40px 24px}h1{font-size:38px;margin:10px 0}h2{font-size:20px}section{background:white;padding:22px;margin:20px 0;border:1px solid #d6e1e8;border-radius:12px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;padding:10px;border-bottom:1px solid #e6ebef;vertical-align:top}th{color:#425a69}small{color:#56717e}.badge{color:#075f54;font-weight:700;letter-spacing:2px}p{line-height:1.6}</style><main><div class="badge">PROJECT 01 / DATABASE ENGINEERING</div><h1>From complaint to containment</h1><p>Simulated zinc–nickel production, sourced operating parameters, and explicit assumptions. No private Partec production data.</p><p>A shipped lot with untested deposit nickel is traced to an out-of-window bath analysis. The containment report lists affected customers and delivery notes. A separate unknown-exposure list shows lots plated without current, complete bath evidence.</p>'''
    page += '<small>' + html.escape(result['generated_at'] + ' | ' + result['postgres_version']) + '</small>'
    bosch_parts = next(row['total'] for row in result['tables']['counts'] if row['item'] == 'bosch_parts')
    if bosch_parts:
        bosch_note = f'The Bosch tables hold {bosch_parts:,} parts loaded from the official Kaggle training data.'
        if (out / 'bosch_benchmark.json').exists():
            bosch_note += ' The query timings measured on them are in bosch_benchmark.json.'
    else:
        bosch_note = 'Empty Bosch tables mean the authenticated Kaggle dataset has not been loaded. These results do not claim a real-data benchmark.'
    page += ''.join(sections) + '<p>' + html.escape(bosch_note) + '</p></main></html>'
    (out / 'index.html').write_text(page, encoding='utf-8')
    print(out / 'index.html')


if __name__ == '__main__':
    main()
