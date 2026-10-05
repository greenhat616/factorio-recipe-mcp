"""Temperature pools must not feed incompatible consumers or invent heat."""

import pytest

from recipe_mcp.database import JSON, Database
from recipe_mcp.planner import plan
from recipe_mcp.planner.schema import LineSpec


def fluid(name: str, amount: float, **kw: float) -> JSON:
    return {'type': 'fluid', 'name': name, 'amount': amount, **kw}


def steam_db() -> Database:
    return Database(
        raw={
            'fluid': {'steam': {'default_temperature': 15, 'heat_capacity': '0.2kJ'}},
            'recipe': {
                'cold': {'ingredients': [], 'results': [fluid('steam', 1, temperature=165)], 'energy_required': 1},
                'hot': {'ingredients': [], 'results': [fluid('steam', 1, temperature=500)], 'energy_required': 1},
                'use': {
                    'ingredients': [fluid('steam', 1, minimum_temperature=500)],
                    'results': [{'name': 'product', 'amount': 1}],
                    'energy_required': 1,
                },
            },
            'assembling-machine': {
                'asm': {
                    'crafting_categories': ['crafting'],
                    'crafting_speed': 1,
                    'energy_source': {'type': 'void'},
                    'fluid_boxes': [{'production_type': 'input-output'}],
                }
            },
            'generator': {
                'engine': {
                    'fluid_box': {'filter': 'steam'},
                    'fluid_usage_per_tick': 1,
                    'maximum_temperature': 500,
                    'effectivity': 1,
                }
            },
            'technology': {},
        }
    )


@pytest.mark.parametrize('solver', ['lp', 'matrix'])
def test_temperature_matching(solver: str) -> None:
    r = plan(steam_db(), {'product': 1}, lines=['hot', 'use'], solver=solver, validate_stage=False)  # type: ignore[arg-type]
    assert r.max_balance_error < 1e-6
    assert not r.imports
    assert r.totals.machines == pytest.approx(2)
    assert len(r.lines_for_matrix) == 2
    assert any('steam@500' in key for row in r.lines for key in row.outputs)


def test_cold_cannot_feed_hot() -> None:
    with pytest.raises(ValueError, match='infeasible'):
        plan(steam_db(), {'product': 1}, lines=['cold', 'use'], forbid_imports=['steam'], validate_stage=False)
    r = plan(steam_db(), {'product': 1}, lines=['cold', 'hot', 'use'], forbid_imports=['steam'], validate_stage=False)
    assert not any(row.recipe == 'cold' for row in r.lines)


def test_generator_uses_actual_temperature() -> None:
    r = plan(
        steam_db(),
        {'energy:electric': 1.8},
        lines=['cold', LineSpec(recipe='generate:engine', temperature=165)],
        energy_mode='balance',
        validate_stage=False,
    )
    engine = next(row for row in r.lines if row.recipe == 'generate:engine')
    assert engine.machines == pytest.approx(1)
    assert sum(engine.inputs.values()) == pytest.approx(60)
    assert not r.imports
    assert r.max_balance_error < 1e-6


def test_temperature_integer_counts_exclude_matching() -> None:
    r = plan(
        steam_db(),
        {'product': 1},
        lines=['hot', 'use'],
        integer_machines=True,
        limits={'machines': 2},
        validate_stage=False,
    )
    assert r.totals.machines_ceil == 2
    assert r.limits_usage[0].used == 2


def test_auto_steam_generation() -> None:
    r = plan(steam_db(), {'energy:electric': 1.8}, energy_mode='balance', validate_stage=False)
    assert any(row.recipe.startswith('generate:engine@') for row in r.lines)
    assert not r.imports
    assert r.totals.net_electric_MW == pytest.approx(1.8)
