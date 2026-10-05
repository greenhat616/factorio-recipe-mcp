"""Closed-form energy checks independent of the LP implementation."""

from copy import deepcopy

import pytest

from recipe_mcp.database import JSON, Database
from recipe_mcp.planner import plan
from recipe_mcp.planner.schema import Defaults, LineSpec, Per

RAW: JSON = {
    'recipe': {
        'make': {
            'ingredients': [{'name': 'ore', 'amount': 1}],
            'results': [{'name': 'plate', 'amount': 1}],
            'energy_required': 1,
        }
    },
    'assembling-machine': {
        'asm': {
            'crafting_categories': ['crafting'],
            'crafting_speed': 1,
            'energy_usage': '2MW',
            'energy_source': {'type': 'electric', 'drain': '0W'},
        }
    },
    'fluid': {'gas': {'fuel_value': '10kJ'}},
    'item': {
        'rod': {'fuel_category': 'nuclear', 'fuel_value': '4GJ', 'burnt_result': 'spent'},
        'coal': {'fuel_category': 'chemical', 'fuel_value': '4MJ'},
    },
    'generator': {
        'gen': {'max_power_output': '1MW', 'effectivity': 0.9, 'burns_fluid': True, 'fluid_box': {'filter': 'gas'}}
    },
    'reactor': {
        'nuke': {
            'consumption': '50MW',
            'neighbour_bonus': 1,
            'energy_source': {'type': 'burner', 'fuel_categories': ['nuclear']},
        },
        'geo': {'consumption': '10MW', 'energy_source': {'type': 'void'}},
    },
    'solar-panel': {'panel': {'production': '100kW'}},
    'electric-energy-interface': {'windmill': {'energy_production': '4MW'}},
    'technology': {},
}


@pytest.fixture
def energy_db() -> Database:
    return Database(raw=deepcopy(RAW))


@pytest.mark.parametrize('per,factor', [('second', 1), ('minute', 60), ('hour', 3600)])
def test_generator_units(energy_db: Database, per: Per, factor: int) -> None:
    r = plan(
        energy_db, {'energy:electric': 9}, lines=['generate:gen'], energy_mode='balance', validate_stage=False, per=per
    )
    assert r.lines[0].machines == pytest.approx(9)
    assert r.imports['fluid:gas'] == pytest.approx(1000 * factor)
    assert r.achieved_targets['energy:electric'] == 9
    assert r.totals.electric_generation_MW == 9
    assert next(f for f in r.items if f.item == 'energy:electric').unit == 'MW'
    assert r.max_balance_error < 1e-6


@pytest.mark.parametrize('neighbours,heat', [(0, 50), (2, 150)])
def test_nuclear_fuel_and_ash(energy_db: Database, neighbours: int, heat: float) -> None:
    r = plan(
        energy_db,
        {'energy:heat': heat},
        lines=[LineSpec(recipe='heat:nuke', fuel='rod', neighbours=neighbours)],
        energy_mode='balance',
        validate_stage=False,
    )
    assert r.lines[0].machines == pytest.approx(1)
    assert r.imports['item:rod'] == pytest.approx(0.0125)
    assert r.surplus['item:spent'] == pytest.approx(0.0125)
    assert r.lines_for_matrix[0].neighbours == neighbours
    assert r.lines_for_matrix[0].fuel == 'item:rod'


def test_fuel_validation_and_priority(energy_db: Database) -> None:
    with pytest.raises(ValueError, match='compatible fuel'):
        plan(energy_db, {'energy:heat': 50}, lines=[LineSpec(recipe='heat:nuke', fuel='coal')], validate_stage=False)
    r = plan(
        energy_db,
        {'energy:heat': 50},
        lines=['heat:nuke'],
        defaults=Defaults(fuel=['coal', 'rod']),
        validate_stage=False,
        energy_mode='balance',
    )
    assert r.imports['item:rod'] == pytest.approx(0.0125)


def test_electric_import_caps_and_surplus(energy_db: Database) -> None:
    with pytest.raises(ValueError, match='infeasible'):
        plan(energy_db, {'plate': 1}, lines=['make'], energy_mode='balance', validate_stage=False)
    r = plan(
        energy_db,
        {'plate': 60},
        lines=['make'],
        energy_mode='balance',
        per='minute',
        limits={'imports': {'energy:electric': 2}},
        validate_stage=False,
    )
    assert r.imports['energy:electric'] == 2
    assert r.limits_usage[0].unit == 'MW' and r.limits_usage[0].used == 2
    r = plan(
        energy_db,
        {'energy:heat': 1},
        lines=[LineSpec(recipe='heat:geo', fixed_machines=1)],
        energy_mode='balance',
        allow_surplus=False,
        validate_stage=False,
    )
    assert r.surplus['energy:heat'] == 9
    assert not r.disposal_unhandled


def test_solar_wind_and_auto_discovery(energy_db: Database) -> None:
    r = plan(energy_db, {'energy:electric': 0.7}, lines=['solar:panel'], validate_stage=False, energy_mode='balance')
    assert r.lines[0].machines == pytest.approx(10)
    with pytest.raises(ValueError, match='wind_factor'):
        plan(energy_db, {'energy:electric': 2}, lines=['wind:windmill'], validate_stage=False)
    r = plan(energy_db, {'energy:electric': 2}, lines=['wind:windmill'], wind_factor=0.5, validate_stage=False)
    assert r.lines[0].machines == 1
    r = plan(energy_db, {'plate': 1}, energy_mode='balance', validate_stage=False)
    assert any(line.recipe.startswith('generate:') for line in r.lines)
    assert r.totals.net_electric_MW == pytest.approx(0)


def test_burner_report_and_balance() -> None:
    raw = deepcopy(RAW)
    raw['assembling-machine']['asm']['energy_source'] = {
        'type': 'burner',
        'fuel_categories': ['nuclear'],
        'effectivity': 0.5,
    }
    db = Database(raw=raw)
    spec = LineSpec(recipe='make', fuel='rod')
    r = plan(db, {'plate': 1}, lines=[spec], validate_stage=False)
    assert 'item:rod' not in r.imports
    assert r.lines[0].fuel_per_machine == pytest.approx(0.001)
    r = plan(db, {'plate': 1}, lines=[spec], energy_mode='balance', validate_stage=False)
    assert r.imports['item:rod'] == pytest.approx(0.001)
    assert r.surplus['item:spent'] == pytest.approx(0.001)


@pytest.mark.realdata
@pytest.mark.parametrize('target', ['nullius-methanol', 'energy:electric'])
def test_nullius_self_power(real_db: Database, force: str, target: str) -> None:
    r = plan(real_db, {target: 10}, force=force, energy_mode='balance')
    assert r.status == 'optimal' and r.max_balance_error < 1e-6
    assert any(line.recipe.startswith('heat:') for line in r.lines)
    assert any(line.recipe.startswith('generate:nullius-turbine') for line in r.lines)
    assert any('burn-' in line.recipe and 'steam' in line.recipe for line in r.lines)
    assert any(
        real_db.recipes.get(line.recipe, {}).get('category') in ('boiling', 'pressure-boiling') for line in r.lines
    )
    assert r.totals.net_electric_MW == pytest.approx(10 if target == 'energy:electric' else 0, abs=1e-6)


def test_electric_reactor_cannot_supply_free_heat() -> None:
    raw = deepcopy(RAW)
    raw['reactor'] = {'heater': {'consumption': '1MW', 'energy_source': {'type': 'electric'}}}
    db = Database(raw=raw)
    with pytest.raises(ValueError, match='infeasible'):
        plan(db, {'energy:heat': 1}, lines=['heat:heater'], energy_mode='balance', validate_stage=False)
    r = plan(
        db,
        {'energy:heat': 1},
        lines=['heat:heater'],
        energy_mode='balance',
        imports=['energy:electric'],
        validate_stage=False,
    )
    assert r.totals.power_MW == 1 and r.imports['energy:electric'] == 1


@pytest.mark.realdata
def test_nullius_generator_channels(real_db: Database, force: str) -> None:
    r = plan(real_db, {'energy:electric': 10}, force=force, energy_mode='balance')
    for row in r.lines:
        if not row.recipe.startswith('generate:nullius-turbine-generator-'):
            continue
        channel = next(k for k in row.inputs if k.startswith('fluid:nullius-energy~'))
        kind, tier = channel.split('~')[1].split('-')
        assert f'-{kind}-' in row.machine and row.machine.endswith('-' + tier)
        turbines = [
            line
            for line in r.lines
            if line.machine_type == 'furnace' and any(k.split('@')[0] == channel for k in line.outputs)
        ]
        assert turbines and all(f'-{kind}-' in line.machine and line.machine.endswith('-' + tier) for line in turbines)
