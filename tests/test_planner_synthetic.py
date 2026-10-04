"""Planner math on a synthetic prototype set with closed-form answers (no game data needed)."""
from typing import Any

import pytest

from recipe_mcp.database import JSON, Database
from recipe_mcp.planner import drain_watts, machine_stats, plan, production_matrix


def fluid(n: str, a: float) -> JSON: return {'type': 'fluid', 'name': n, 'amount': a}
def item(n: str, a: float) -> JSON: return {'type': 'item', 'name': n, 'amount': a}


RAW: JSON = {
    'recipe': {
        'aop': {'name': 'aop', 'category': 'oil', 'energy_required': 5, 'allow_productivity': True,
                'ingredients': [fluid('crude', 100)], 'results': [fluid('heavy', 25), fluid('light', 45), fluid('petro', 55)]},
        'hc': {'name': 'hc', 'category': 'chem', 'energy_required': 2, 'ingredients': [fluid('heavy', 40)], 'results': [fluid('light', 30)]},
        'lc': {'name': 'lc', 'category': 'chem', 'energy_required': 2, 'ingredients': [fluid('light', 30)], 'results': [fluid('petro', 20)]},
        'gear-a': {'name': 'gear-a', 'category': 'crafting', 'energy_required': 1, 'ingredients': [item('plate', 2)], 'results': [item('gear', 1)]},
        'gear-b': {'name': 'gear-b', 'category': 'crafting', 'energy_required': 4, 'ingredients': [item('plate', 1)], 'results': [item('gear', 1)]},
    },
    'fluid': {n: {'name': n} for n in ['crude', 'heavy', 'light', 'petro']},
    'assembling-machine': {
        'refinery': {'crafting_speed': 1, 'crafting_categories': ['oil'], 'energy_usage': '420kW', 'module_slots': 2,
                     'allowed_effects': ['speed', 'productivity', 'consumption', 'pollution'], 'energy_source': {'type': 'electric', 'drain': '0W'},
                     'fluid_boxes': [{'production_type': 'input'}] + [{'production_type': 'output'}] * 3},
        'plant': {'crafting_speed': 1, 'crafting_categories': ['chem'], 'energy_usage': '210kW', 'energy_source': {'type': 'electric'},
                  'module_slots': 1, 'allowed_effects': ['speed', 'productivity', 'consumption'],
                  'fluid_boxes': [{'production_type': 'input'}, {'production_type': 'output'}]},
        'asm': {'crafting_speed': 1, 'crafting_categories': ['crafting'], 'energy_usage': '75kW', 'energy_source': {'type': 'electric'}},
    },
    'module': {'spd': {'effect': {'speed': 0.5, 'consumption': 0.7}}, 'prod': {'effect': {'productivity': 0.1, 'speed': -0.15}}},
    'beacon': {'bcn': {'distribution_effectivity': 0.5, 'profile': [1, 0.7071], 'module_slots': 2,
                       'allowed_effects': ['speed', 'consumption'], 'energy_usage': '480kW'}},
    'technology': {},
}

A = 100 / 97.5  # crude-oil crafts per second for 100 petro/s with full cracking
OIL = ['aop', 'hc', 'lc']


@pytest.fixture(scope='module')
def db() -> Database:
    return Database(raw=RAW)


def approx(v: float, rel: float = 1e-9) -> Any:
    return pytest.approx(v, rel=rel, abs=rel)


def by_recipe(result: JSON, key: str = 'crafts') -> dict[str, Any]:
    return {l['recipe']: l[key] for l in result['lines']}


def test_matrix_closed_form(db: Database) -> None:
    r = plan(db, {'petro': 100}, OIL, solver='matrix', validate_stage=False)
    assert by_recipe(r) == {'aop': approx(A), 'hc': approx(.625 * A), 'lc': approx(2.125 * A)}
    assert r['imports']['fluid:crude'] == approx(100 * A)
    assert by_recipe(r, 'machines')['aop'] == approx(5 * A)
    # Electric drain defaults to energy_usage / 30 when not declared.
    assert r['totals']['power_MW'] == approx(5 * A * .42 + 2 * .625 * A * .217 + 2 * 2.125 * A * .217)
    assert r['max_balance_error'] < 1e-9 and r['matrix']['degrees_of_freedom'] == 0


def test_lp_agrees_with_matrix_without_alternatives(db: Database) -> None:
    lp = plan(db, {'petro': 100}, OIL, validate_stage=False)
    assert by_recipe(lp)['aop'] == approx(A, 1e-7)


def test_per_minute_units(db: Database) -> None:
    m = plan(db, {'petro': 6000}, OIL, solver='matrix', per='minute', validate_stage=False)
    assert by_recipe(m)['aop'] == approx(A * 60)


def test_matrix_byproduct_becomes_surplus_unknown(db: Database) -> None:
    r = plan(db, {'petro': 55}, ['aop', 'hc'], solver='matrix', validate_stage=False)
    assert r['surplus']['fluid:light'] == approx(45 + 25 / 40 * 30)


def test_matrix_diagnostics(db: Database) -> None:
    with pytest.raises(ValueError, match='cannot balance'):
        plan(db, {'petro': 55, 'light': 10}, ['aop'], solver='matrix', validate_stage=False)
    with pytest.raises(ValueError, match='underdetermined'):
        plan(db, {'gear': 1}, ['gear-a', 'gear-b'], solver='matrix', validate_stage=False)
    pm = production_matrix(db, ['gear-a', 'gear-b'], {'gear': 1})
    assert pm['square_system']['degrees_of_freedom'] == 1 and pm['matrix'] == [[1, 1], [-2, -1]]


def test_lp_route_choice_and_shadow_price(db: Database) -> None:
    g = plan(db, {'gear': 1}, ['gear-a', 'gear-b'], validate_stage=False)
    assert [l['recipe'] for l in g['lines']] == ['gear-a']
    g = plan(db, {'gear': 1}, ['gear-a', 'gear-b'], objective='imports', validate_stage=False)
    assert [l['recipe'] for l in g['lines']] == ['gear-b']
    # 1 plate import + 4 machine-seconds * 1e-3 machine weight.
    assert g['shadow_prices']['item:gear'] == approx(1 + 4e-3, 1e-6)


def test_fixed_machines_pin_a_line(db: Database) -> None:
    g = plan(db, {'gear': 1}, [{'recipe': 'gear-b', 'fixed_machines': 2}, 'gear-a'], solver='matrix', validate_stage=False)
    assert by_recipe(g)['gear-a'] == approx(.5)


def test_modules_and_beacons(db: Database) -> None:
    s = machine_stats(db, 'aop', modules=['spd', 'prod'], beacons=[{'beacon': 'bcn', 'count': 2, 'modules': ['spd', 'spd']}],
                      validate_stage=False)
    # Each beacon: module effect x distribution_effectivity 0.5 x profile[2 beacons] 0.7071.
    assert s['speed_multiplier'] == approx(1 + .5 - .15 + 2 * 2 * .5 * .5 * .7071)
    assert s['productivity'] == approx(.1)
    assert s['per_machine']['fluid:petro'] == approx(55 * 1.1 * s['speed_multiplier'] / 5)
    assert s['consumption_multiplier'] == approx(1 + .7 + 2 * 2 * .7 * .5 * .7071)
    assert s['power_per_machine_MW']['beacons'] == approx(.96)


def test_module_rules(db: Database) -> None:
    with pytest.raises(ValueError, match='not allowed by recipe'):
        machine_stats(db, 'hc', machine='plant', modules=['prod'], validate_stage=False)
    with pytest.raises(ValueError, match='not allowed by entity'):
        machine_stats(db, 'aop', beacons=[{'beacon': 'bcn', 'modules': ['prod']}], validate_stage=False)


def test_auto_discovery_revisits_recipe_skipped_as_catalyst_only() -> None:
    # 'cat' is reached before 'b'; 'loop' has no net 'cat' output, but it is the only producer of 'b'.
    raw: JSON = {
        'recipe': {
            'mk': {'name': 'mk', 'category': 'crafting', 'ingredients': [item('cat', 1), item('b', 1)], 'results': [item('prod', 1)]},
            'loop': {'name': 'loop', 'category': 'crafting', 'ingredients': [item('cat', 1)], 'results': [item('cat', 1), item('b', 1)]},
        },
        'assembling-machine': {'asm': {'crafting_speed': 1, 'crafting_categories': ['crafting'], 'energy_usage': '75kW',
                                       'energy_source': {'type': 'electric'}}},
        'technology': {},
    }
    r = plan(Database(raw=raw), {'prod': 1}, forbid_imports=['b'], validate_stage=False)
    assert by_recipe(r) == {'mk': approx(1), 'loop': approx(1)}


def test_drain_defaults_to_a_thirtieth_of_usage_for_crafting_machines() -> None:
    electric = {'energy_usage': '300kW', 'energy_source': {'type': 'electric'}}
    assert drain_watts(electric) == approx(10e3)
    assert drain_watts({**electric, 'energy_source': {'type': 'electric', 'drain': '1kW'}}) == approx(1e3)
    assert drain_watts(electric, 'mining-drill') == 0
    assert drain_watts({**electric, 'energy_source': {'type': 'burner'}}) == 0


def test_efficient_preference_counts_the_default_drain() -> None:
    # 'implicit' declares no drain, so it idles at 100/30 kW and costs more than 'explicit'.
    raw: JSON = {
        'recipe': {'r': {'name': 'r', 'category': 'crafting', 'ingredients': [], 'results': [item('x', 1)]}},
        'assembling-machine': {
            'implicit': {'crafting_speed': 1, 'crafting_categories': ['crafting'], 'energy_usage': '100kW', 'energy_source': {'type': 'electric'}},
            'explicit': {'crafting_speed': 1, 'crafting_categories': ['crafting'], 'energy_usage': '101kW',
                         'energy_source': {'type': 'electric', 'drain': '0W'}},
        },
        'technology': {},
    }
    s = machine_stats(Database(raw=raw), 'r', defaults={'machine_preference': 'efficient'}, validate_stage=False)
    assert s['machine'] == 'explicit'
