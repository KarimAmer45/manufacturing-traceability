from datetime import date
import pytest
from common import sources
from p1_simulate_plant import part_area_dm2, working_days

def test_working_days_skip_weekends():
    assert list(working_days(date(2026, 3, 6), 1, 5)) == [date(2026, 3, d) for d in (6, 9, 10, 11, 12)]

def test_surface_area_scales_quadratically():
    small = part_area_dm2(6, 20, 10, 4, 1.3)
    assert small > 0
    assert part_area_dm2(12, 40, 20, 8, 1.3) == pytest.approx(4 * small)

def test_configuration_source_keys_resolve():
    config = sources()
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('src', 'mass_src', 'dims_src'):
                    assert item in config['sources']
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(config)
    assert all('why' in assumption for assumption in config['assumptions'].values())
