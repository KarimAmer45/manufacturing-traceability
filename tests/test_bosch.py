import psycopg
import pytest
from p1_load_bosch import compare_with_published, load_bosch

def fixture_files(path):
    # Invented fixtures; never represented as Bosch production records.
    (path / 'train_numeric.csv').write_text('Id,Response\n900000001,0\n900000002,1\n900000003,0\n')
    (path / 'train_date.csv').write_text('Id,L0_S0_D1,L0_S0_D2,L3_S32_D3\n900000001,1.1,1.2,2.0\n900000002,,,2.01\n900000003,3.0,3.4,\n')

def test_bosch_chunked_load_min_max_and_paths(db, tmp_path):
    fixture_files(tmp_path)
    result = load_bosch(db.cursor(), tmp_path, chunk_size=1)
    assert result['input_parts'] == 3 and result['input_failures'] == 1
    assert result['station_passes'] == 4
    first, last = db.execute("SELECT t_first,t_last FROM bosch.station_pass WHERE part_id=900000001 AND station_code='L0_S0'").fetchone()
    assert float(first) == 1.1 and float(last) == 1.2
    assert db.execute('SELECT station_path FROM bosch.v_part_path WHERE part_id=900000001').fetchone()[0] == 'L0_S0 > L3_S32'

def test_bosch_sample_then_full_is_idempotent(db, tmp_path):
    fixture_files(tmp_path)
    cur = db.cursor()
    for limit in (1, None, None):
        result = load_bosch(cur, tmp_path, max_rows=limit, chunk_size=2)
        cur.execute('DROP TABLE bosch_seen_id, bosch_import_part')
    assert result['station_passes'] == 4
    assert db.execute('SELECT count(*) FROM bosch.part WHERE part_id>=900000001').fetchone()[0] == 3

def test_bosch_rejects_date_numeric_id_mismatch(db, tmp_path):
    fixture_files(tmp_path)
    path = tmp_path / 'train_date.csv'
    path.write_text(path.read_text().replace('900000002', '999999999'))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        load_bosch(db.cursor(), tmp_path)

def test_full_import_is_compared_with_published_totals(db, tmp_path):
    fixture_files(tmp_path)
    result = load_bosch(db.cursor(), tmp_path)
    with pytest.warns(UserWarning):
        comparison = compare_with_published(result)
    assert len(comparison['warnings']) == 2 and comparison['parts_deviation_pct'] < -99
    sample = compare_with_published(dict(result, sample_limit=1))
    assert sample['warnings'] == [] and 'Sample import' in sample['note']
