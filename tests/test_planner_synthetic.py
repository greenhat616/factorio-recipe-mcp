"""Planner math on a synthetic prototype set with closed-form answers (no game data needed)."""

import copy
from typing import Any

import pytest

from recipe_mcp.database import JSON, Database
from recipe_mcp.planner import BeaconSpec, Defaults, LineSpec, drain_watts, machine_stats, plan, production_matrix
from recipe_mcp.planner.lp import LPBuilder
from recipe_mcp.planner.schema import PlanResult


def fluid(n: str, a: float) -> JSON:
    return {'type': 'fluid', 'name': n, 'amount': a}


def item(n: str, a: float) -> JSON:
    return {'type': 'item', 'name': n, 'amount': a}


RAW: JSON = {
    'recipe': {
        'aop': {
            'name': 'aop',
            'category': 'oil',
            'energy_required': 5,
            'allow_productivity': True,
            'ingredients': [fluid('crude', 100)],
            'results': [fluid('heavy', 25), fluid('light', 45), fluid('petro', 55)],
        },
        'hc': {
            'name': 'hc',
            'category': 'chem',
            'energy_required': 2,
            'ingredients': [fluid('heavy', 40)],
            'results': [fluid('light', 30)],
        },
        'lc': {
            'name': 'lc',
            'category': 'chem',
            'energy_required': 2,
            'ingredients': [fluid('light', 30)],
            'results': [fluid('petro', 20)],
        },
        'gear-a': {
            'name': 'gear-a',
            'category': 'crafting',
            'energy_required': 1,
            'ingredients': [item('plate', 2)],
            'results': [item('gear', 1)],
        },
        'gear-b': {
            'name': 'gear-b',
            'category': 'crafting',
            'energy_required': 4,
            'ingredients': [item('plate', 1)],
            'results': [item('gear', 1)],
        },
    },
    'fluid': {n: {'name': n} for n in ['crude', 'heavy', 'light', 'petro']},
    'assembling-machine': {
        'refinery': {
            'crafting_speed': 1,
            'crafting_categories': ['oil'],
            'energy_usage': '420kW',
            'module_slots': 2,
            'allowed_effects': ['speed', 'productivity', 'consumption', 'pollution'],
            'energy_source': {'type': 'electric', 'drain': '0W'},
            'fluid_boxes': [{'production_type': 'input'}] + [{'production_type': 'output'}] * 3,
        },
        'plant': {
            'crafting_speed': 1,
            'crafting_categories': ['chem'],
            'energy_usage': '210kW',
            'energy_source': {'type': 'electric'},
            'module_slots': 1,
            'allowed_effects': ['speed', 'productivity', 'consumption'],
            'fluid_boxes': [{'production_type': 'input'}, {'production_type': 'output'}],
        },
        'asm': {
            'crafting_speed': 1,
            'crafting_categories': ['crafting'],
            'energy_usage': '75kW',
            'energy_source': {'type': 'electric'},
        },
    },
    'module': {
        'spd': {'effect': {'speed': 0.5, 'consumption': 0.7}},
        'prod': {'effect': {'productivity': 0.1, 'speed': -0.15}},
    },
    'beacon': {
        'bcn': {
            'distribution_effectivity': 0.5,
            'profile': [1, 0.7071],
            'module_slots': 2,
            'allowed_effects': ['speed', 'consumption'],
            'energy_usage': '480kW',
        }
    },
    'technology': {},
}

A = 100 / 97.5  # crude-oil crafts per second for 100 petro/s with full cracking
OIL = ['aop', 'hc', 'lc']


@pytest.fixture(scope='module')
def db() -> Database:
    return Database(raw=RAW)


def approx(v: float, rel: float = 1e-9) -> Any:
    return pytest.approx(v, rel=rel, abs=rel)


def by_recipe(result: PlanResult, key: str = 'crafts') -> dict[str, Any]:
    return {line.recipe: getattr(line, key) for line in result.lines}


def test_matrix_closed_form(db: Database) -> None:
    r = plan(db, {'petro': 100}, OIL, solver='matrix', validate_stage=False)
    assert by_recipe(r) == {'aop': approx(A), 'hc': approx(0.625 * A), 'lc': approx(2.125 * A)}
    assert r.imports['fluid:crude'] == approx(100 * A)
    assert by_recipe(r, 'machines')['aop'] == approx(5 * A)
    # Electric drain defaults to energy_usage / 30 when not declared.
    assert r.totals.power_MW == approx(5 * A * 0.42 + 2 * 0.625 * A * 0.217 + 2 * 2.125 * A * 0.217)
    assert r.max_balance_error < 1e-9 and r.matrix is not None and r.matrix.degrees_of_freedom == 0


def test_lp_agrees_with_matrix_without_alternatives(db: Database) -> None:
    lp = plan(db, {'petro': 100}, OIL, validate_stage=False)
    assert by_recipe(lp)['aop'] == approx(A, 1e-7)


def test_per_minute_units(db: Database) -> None:
    m = plan(db, {'petro': 6000}, OIL, solver='matrix', per='minute', validate_stage=False)
    assert by_recipe(m)['aop'] == approx(A * 60)


def test_matrix_byproduct_becomes_surplus_unknown(db: Database) -> None:
    r = plan(db, {'petro': 55}, ['aop', 'hc'], solver='matrix', validate_stage=False)
    assert r.surplus['fluid:light'] == approx(45 + 25 / 40 * 30)


def test_matrix_diagnostics(db: Database) -> None:
    with pytest.raises(ValueError, match='cannot balance'):
        plan(db, {'petro': 55, 'light': 10}, ['aop'], solver='matrix', validate_stage=False)
    with pytest.raises(ValueError, match='underdetermined'):
        plan(db, {'gear': 1}, ['gear-a', 'gear-b'], solver='matrix', validate_stage=False)
    pm = production_matrix(db, ['gear-a', 'gear-b'], {'gear': 1})
    assert pm.square_system.degrees_of_freedom == 1 and pm.matrix == [[1, 1], [-2, -1]]


def test_lp_route_choice_and_shadow_price(db: Database) -> None:
    g = plan(db, {'gear': 1}, ['gear-a', 'gear-b'], validate_stage=False)
    assert [line.recipe for line in g.lines] == ['gear-a']
    g = plan(db, {'gear': 1}, ['gear-a', 'gear-b'], objective='imports', validate_stage=False)
    assert [line.recipe for line in g.lines] == ['gear-b']
    # 1 plate import + 4 machine-seconds * 1e-3 machine weight.
    assert g.shadow_prices is not None and g.shadow_prices['item:gear'] == approx(1 + 4e-3, 1e-6)


def test_fixed_machines_pin_a_line(db: Database) -> None:
    g = plan(
        db, {'gear': 1}, [LineSpec(recipe='gear-b', fixed_machines=2), 'gear-a'], solver='matrix', validate_stage=False
    )
    assert by_recipe(g)['gear-a'] == approx(0.5)


def test_modules_and_beacons(db: Database) -> None:
    s = machine_stats(
        db,
        'aop',
        modules=['spd', 'prod'],
        beacons=[BeaconSpec(beacon='bcn', count=2, modules=['spd', 'spd'])],
        validate_stage=False,
    )
    # Each beacon: module effect x distribution_effectivity 0.5 x profile[2 beacons] 0.7071.
    assert s.speed_multiplier == approx(1 + 0.5 - 0.15 + 2 * 2 * 0.5 * 0.5 * 0.7071)
    assert s.productivity == approx(0.1)
    assert s.per_machine['fluid:petro'] == approx(55 * 1.1 * s.speed_multiplier / 5)
    assert s.consumption_multiplier == approx(1 + 0.7 + 2 * 2 * 0.7 * 0.5 * 0.7071)
    assert s.power_per_machine_MW.beacons == approx(0.96)


def test_module_rules(db: Database) -> None:
    with pytest.raises(ValueError, match='not allowed by recipe'):
        machine_stats(db, 'hc', machine='plant', modules=['prod'], validate_stage=False)
    with pytest.raises(ValueError, match='not allowed by entity'):
        machine_stats(db, 'aop', beacons=[BeaconSpec(beacon='bcn', modules=['prod'])], validate_stage=False)


def test_auto_discovery_revisits_recipe_skipped_as_catalyst_only() -> None:
    # 'cat' is reached before 'b'; 'loop' has no net 'cat' output, but it is the only producer of 'b'.
    raw: JSON = {
        'recipe': {
            'mk': {
                'name': 'mk',
                'category': 'crafting',
                'ingredients': [item('cat', 1), item('b', 1)],
                'results': [item('prod', 1)],
            },
            'loop': {
                'name': 'loop',
                'category': 'crafting',
                'ingredients': [item('cat', 1)],
                'results': [item('cat', 1), item('b', 1)],
            },
        },
        'assembling-machine': {
            'asm': {
                'crafting_speed': 1,
                'crafting_categories': ['crafting'],
                'energy_usage': '75kW',
                'energy_source': {'type': 'electric'},
            }
        },
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
            'implicit': {
                'crafting_speed': 1,
                'crafting_categories': ['crafting'],
                'energy_usage': '100kW',
                'energy_source': {'type': 'electric'},
            },
            'explicit': {
                'crafting_speed': 1,
                'crafting_categories': ['crafting'],
                'energy_usage': '101kW',
                'energy_source': {'type': 'electric', 'drain': '0W'},
            },
        },
        'technology': {},
    }
    s = machine_stats(Database(raw=raw), 'r', defaults=Defaults(machine_preference='efficient'), validate_stage=False)
    assert s.machine == 'explicit'


def test_lp_builder_maps_names_both_ways() -> None:
    lp = LPBuilder()
    x = lp.col('x:0', cost=1)
    y = lp.col('y', cost=2, ub=3)
    lp.add_eq('balance', {x: 1, y: 1}, 4)
    lp.add_ub('cap', {x: 1}, 1)
    res = lp.solve()
    assert lp.cols[lp.index['y']] == 'y' and lp.eq_names == ['balance'] and lp.ub_names == ['cap']
    assert res.x[x] == approx(1) and res.x[y] == approx(3)
    with pytest.raises(ValueError, match='Duplicate'):
        lp.col('y')


def test_plan_reports_optimal_status(db: Database) -> None:
    assert plan(db, {'petro': 100}, OIL, validate_stage=False).status == 'optimal'


def test_maximize_under_import_limit(db: Database) -> None:
    r = plan(db, {'petro': 1}, OIL, mode='maximize', limits={'imports': {'crude': 100}}, validate_stage=False)
    assert r.scale == approx(97.5, 1e-7) and r.achieved_targets['fluid:petro'] == approx(97.5, 1e-7)
    assert r.imports['fluid:crude'] == approx(100, 1e-7)
    (b,) = r.bottlenecks
    assert b.constraint == 'import:fluid:crude' and b.marginal == approx(0.975, 1e-7)
    assert r.limits_usage[0].utilization == approx(1, 1e-7)


def test_maximize_marginal_is_unit_consistent_per_minute(db: Database) -> None:
    r = plan(
        db, {'petro': 1}, OIL, per='minute', mode='maximize', limits={'imports': {'crude': 6000}}, validate_stage=False
    )
    assert r.scale == approx(5850, 1e-7) and r.bottlenecks[0].marginal == approx(0.975, 1e-7)


def test_maximize_keeps_ratios(db: Database) -> None:
    r = plan(
        db, {'petro': 1, 'light': 2}, OIL, mode='maximize', limits={'imports': {'crude': 100}}, validate_stage=False
    )
    out = r.achieved_targets
    assert out['fluid:light'] / out['fluid:petro'] == approx(2, 1e-6)
    flows = {i.item: i for i in r.items}
    assert flows['fluid:light'].target == approx(out['fluid:light'])


def test_maximize_under_power_limit_skips_heavy_cracking(db: Database) -> None:
    # Per MW, heavy cracking yields less petroleum than more distillation; light cracking yields more.
    # Distillation + light cracking: 85 petro per 5 x 0.42 + 1.5 x 2 x 0.217 = 2.751 MW.
    r = plan(db, {'petro': 1}, OIL, mode='maximize', limits={'power_MW': 10}, validate_stage=False)
    assert r.scale == approx(10 * 85 / 2.751, 1e-7)
    assert r.totals.power_MW <= 10 + 1e-6
    assert [b.constraint for b in r.bottlenecks] == ['power_MW']


def test_machine_type_limit(db: Database) -> None:
    r = plan(
        db,
        {'petro': 1},
        OIL,
        mode='maximize',
        limits={'machines_by_type': {'refinery': 5}},
        validate_stage=False,
    )
    assert r.scale == approx(97.5, 1e-7)
    with pytest.raises(ValueError, match='Unknown machine'):
        plan(db, {'petro': 1}, OIL, mode='maximize', limits={'machines_by_type': {'nope': 1}}, validate_stage=False)


def test_total_machine_limit(db: Database) -> None:
    # Full cracking needs 5 + 2 x 0.625 + 2 x 2.125 = 10.5 machines per crude craft/s.
    r = plan(db, {'petro': 1}, OIL, mode='maximize', limits={'machines': 21}, validate_stage=False)
    assert r.totals.machines == approx(21, 1e-7) and r.bottlenecks[0].constraint == 'machines'


def test_maximize_rejects_bad_input(db: Database) -> None:
    with pytest.raises(ValueError, match='unbounded'):
        plan(db, {'petro': 1}, OIL, mode='maximize', validate_stage=False)
    with pytest.raises(ValueError, match='ratios'):
        plan(db, {'petro': 0}, OIL, mode='maximize', limits={'power_MW': 1}, validate_stage=False)
    with pytest.raises(ValueError, match='lp solver'):
        plan(db, {'petro': 1}, OIL, solver='matrix', mode='maximize', validate_stage=False)
    with pytest.raises(ValueError, match='power_mw'):
        plan(db, {'petro': 1}, OIL, mode='maximize', limits={'power_mw': 1}, validate_stage=False)
    with pytest.raises(ValueError, match='is a target'):
        plan(db, {'petro': 1}, OIL, limits={'imports': {'petro': 1}}, validate_stage=False)


def test_slack_limit_leaves_targets_solution_unchanged(db: Database) -> None:
    free = plan(db, {'petro': 100}, OIL, validate_stage=False)
    capped = plan(db, {'petro': 100}, OIL, limits={'power_MW': 1000, 'machines': 1000}, validate_stage=False)
    assert capped.totals.machines == approx(free.totals.machines)
    assert all(u.utilization is not None and u.utilization < 1 for u in capped.limits_usage)
    assert capped.bottlenecks == []


def test_tight_limit_in_targets_mode_reports_marginal_cost(db: Database) -> None:
    # gear-b uses fewer plates but more machines; capping plates forces it and frees objective per plate.
    free = plan(db, {'gear': 1}, ['gear-a', 'gear-b'], validate_stage=False)
    assert [line.recipe for line in free.lines] == ['gear-a']
    r = plan(db, {'gear': 1}, ['gear-a', 'gear-b'], limits={'imports': {'plate': 1.5}}, validate_stage=False)
    (b,) = r.bottlenecks
    # One more plate moves one gear from gear-b (4 machines + 0.01) to gear-a (1 machine + 0.02).
    assert b.constraint == 'import:item:plate' and b.used == approx(1.5, 1e-7) and b.marginal == approx(2.99, 1e-7)


def test_consume_with_maximize(db: Database) -> None:
    r = plan(db, {'petro': 1}, OIL, mode='maximize', consume={'crude': 100}, validate_stage=False)
    assert r.scale == approx(97.5, 1e-7) and 'fluid:crude' not in r.imports
    crude = next(i for i in r.items if i.item == 'fluid:crude')
    assert crude.supplied == approx(100) and crude.consumed == approx(100, 1e-7) and crude.surplus == 0
    assert r.consume == {'fluid:crude': 100}


def test_consume_without_targets_in_lp(db: Database) -> None:
    r = plan(db, {}, OIL, consume={'crude': 100}, validate_stage=False)
    assert by_recipe(r)['aop'] == approx(1, 1e-7) and r.max_balance_error < 1e-9


def test_consume_rejections(db: Database) -> None:
    with pytest.raises(ValueError, match='both a target and consumed'):
        plan(db, {'petro': 1}, OIL, consume={'petro': 1}, validate_stage=False)
    with pytest.raises(ValueError, match='Auto-discovery'):
        plan(db, {}, consume={'crude': 100}, validate_stage=False)
    with pytest.raises(ValueError, match='import-capped'):
        plan(db, {'petro': 1}, OIL, consume={'crude': 1}, limits={'imports': {'crude': 1}}, validate_stage=False)
    with pytest.raises(ValueError, match='Nothing sets the scale'):
        plan(db, {}, OIL, solver='matrix', validate_stage=False)
    with pytest.raises(ValueError, match='positive'):
        plan(db, {'petro': 1}, OIL, consume={'crude': 0}, validate_stage=False)


def test_matrix_consume_is_helmod_input_mode(db: Database) -> None:
    r = plan(db, {}, OIL, solver='matrix', consume={'crude': 100}, validate_stage=False)
    assert r.surplus['fluid:petro'] == approx(97.5) and r.max_balance_error < 1e-9
    assert r.matrix is not None and r.matrix.roles['fluid:crude'] == 'consumed_input'
    pm = production_matrix(db, OIL, consume={'crude': 100})
    assert pm.square_system.roles['fluid:crude'] == 'consumed_input' and pm.square_system.degrees_of_freedom == 0


def test_elastic_diagnosis_lowers_the_target(db: Database) -> None:
    # Relaxing the crude cap costs (100 / 97.5) / 50 per unit of petro, cutting the target 1 / 100: cut the target.
    r = plan(db, {'petro': 100}, OIL, limits={'imports': {'crude': 50}}, validate_stage=False)
    assert r.status == 'infeasible' and r.note
    (f,) = r.infeasibility
    assert (f.kind, f.name) == ('target', 'fluid:petro') and f.achievable == approx(48.75, 1e-6)
    assert r.achieved_targets['fluid:petro'] == approx(48.75, 1e-6)
    assert r.suggestions[0].startswith('Or lower target fluid:petro to <= 48.75')


def test_elastic_diagnosis_raises_a_limit_when_lines_are_fixed(db: Database) -> None:
    # Ten refineries draw 4.2 MW; no target can give way, so the power limit must.
    lines: list[str | LineSpec] = [LineSpec(recipe='aop', fixed_machines=10), 'hc', 'lc']
    r = plan(db, {}, lines, limits={'power_MW': 1}, validate_stage=False)
    (f,) = r.infeasibility
    assert (f.kind, f.name) == ('limit', 'power_MW') and f.achievable == approx(4.2, 1e-6)
    assert r.suggestions == ['Raise limits.power_MW to >= 4.2 MW']


def test_elastic_diagnosis_in_maximize_mode(db: Database) -> None:
    # Using up 100 crude needs at least 2.1 MW of refineries; with 1 MW only 100 / 2.1 crude can be consumed.
    r = plan(
        db, {'petro': 1}, OIL, mode='maximize', consume={'crude': 100}, limits={'power_MW': 1}, validate_stage=False
    )
    (f,) = r.infeasibility
    assert (f.kind, f.name) == ('consume', 'fluid:crude') and f.achievable == approx(100 / 2.1, 1e-6)
    assert r.consume == {'fluid:crude': 100}


def test_elastic_diagnosis_keeps_line_bounds_hard(db: Database) -> None:
    lines = [LineSpec(recipe='aop', fixed_machines=1)]
    with pytest.raises(ValueError, match=r"fixed_machines/max_machines on \['aop'\]"):
        plan(db, {'petro': 1}, lines, allow_surplus=False, limits={'power_MW': 100}, validate_stage=False)


def test_infeasible_without_limits_keeps_the_plain_error(db: Database) -> None:
    with pytest.raises(ValueError, match='LP infeasible'):
        plan(db, {'petro': 1}, [LineSpec(recipe='aop', fixed_machines=1)], allow_surplus=False, validate_stage=False)


def with_void(raw: JSON) -> JSON:
    """RAW plus a light-oil void (10 per craft, 1 s) in a speed-2 stack and a speed-1 vent."""
    out = copy.deepcopy(raw)
    out['recipe']['void-light'] = {
        'name': 'void-light',
        'category': 'void',
        'energy_required': 1,
        'ingredients': [fluid('light', 10)],
        'results': [{'type': 'item', 'name': 'void-token', 'amount': 1, 'probability': 0}],
    }
    for name, speed, usage in (('stack', 2, '100kW'), ('vent', 1, '1kW')):
        out['assembling-machine'][name] = {
            'crafting_speed': speed,
            'crafting_categories': ['void'],
            'energy_usage': usage,
            'energy_source': {'type': 'electric', 'drain': '0W'},
            'fluid_boxes': [{'production_type': 'input'}],
        }
    return out


def test_disposal_counts_void_machines() -> None:
    db = Database(raw=with_void(RAW))
    r = plan(db, {'petro': 55}, ['aop'], validate_stage=False)
    # 45 light/s: vent is the efficient default (1 kW per speed vs 50 kW): 45 / (10 x 1) = 4.5 vents.
    (row,) = r.disposal
    assert (row.recipe, row.machine) == ('void-light', 'vent') and row.machines == approx(4.5, 1e-7)
    assert r.disposal_unhandled == ['fluid:heavy']
    assert r.disposal_totals is not None and r.disposal_totals.power_MW == approx(4.5 * 0.001, 1e-7)
    assert r.totals_with_disposal is not None
    assert r.totals_with_disposal.machines == approx(r.totals.machines + 4.5, 1e-7)
    fast = plan(db, {'petro': 55}, ['aop'], disposal_defaults={'machine_preference': 'fastest'}, validate_stage=False)
    assert fast.disposal[0].machine == 'stack' and fast.disposal[0].machines == approx(2.25, 1e-7)
    none = plan(db, {'petro': 55}, ['aop'], disposal='none', validate_stage=False)
    assert none.disposal == [] and none.disposal_totals is None


def test_disposal_does_not_change_the_route() -> None:
    db = Database(raw=with_void(RAW))
    a = plan(db, {'petro': 55}, ['aop', 'hc'], validate_stage=False)
    b = plan(db, {'petro': 55}, ['aop', 'hc'], disposal='none', validate_stage=False)
    assert a.totals == b.totals


def test_integer_machines_maximize(db: Database) -> None:
    # 2.5 machines allow scale 2.5 continuously but only 2 whole gear-a machines.
    kw: dict[str, Any] = dict(mode='maximize', limits={'machines': 2.5}, validate_stage=False)
    cont = plan(db, {'gear': 1}, ['gear-a', 'gear-b'], **kw)
    whole = plan(db, {'gear': 1}, ['gear-a', 'gear-b'], integer_machines=True, **kw)
    assert cont.scale == approx(2.5, 1e-7) and whole.scale == approx(2, 1e-7)
    assert whole.integer_machines and whole.mip_gap is not None and whole.bottlenecks == []
    assert whole.bottlenecks_note and whole.limits_usage[0].used == approx(2)
    assert [(line.recipe, line.machines_ceil) for line in whole.lines] == [('gear-a', 2)]


def test_integer_machines_targets(db: Database) -> None:
    # 1.25 gear/s: gear-a alone needs 2 whole machines; 1 gear-a + 1 gear-b also makes 1.25 with 2 machines and
    # fewer plates, which the imports tie-breaker prefers.
    r = plan(db, {'gear': 1.25}, ['gear-a', 'gear-b'], integer_machines=True, validate_stage=False)
    assert r.status == 'optimal' and r.totals.machines_ceil == 2
    assert {line.recipe: line.machines_ceil for line in r.lines} == {'gear-a': 1, 'gear-b': 1}
    assert r.max_balance_error < 1e-6


def test_integer_machines_rejections(db: Database) -> None:
    with pytest.raises(ValueError, match='lp solver'):
        plan(db, {'petro': 100}, OIL, solver='matrix', integer_machines=True, validate_stage=False)
    with pytest.raises(ValueError, match='elastic diagnosis'):
        plan(db, {'gear': 3}, ['gear-a'], limits={'machines': 2.5}, integer_machines=True, validate_stage=False)
    with pytest.raises(ValueError, match='unbounded'):
        plan(db, {'gear': 1}, ['gear-a'], mode='maximize', integer_machines=True, validate_stage=False)
    many: list[str | LineSpec] = [LineSpec(recipe='gear-a', id=f'a{i}') for i in range(401)]
    with pytest.raises(ValueError, match='at most 400 lines'):
        plan(db, {'gear': 1}, many, integer_machines=True, validate_stage=False)
