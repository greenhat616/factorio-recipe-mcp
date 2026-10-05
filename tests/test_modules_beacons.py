"""Module and beacon accounting on a synthetic prototype set with closed-form answers."""

from typing import Any

import pytest

from recipe_mcp.database import JSON, Database
from recipe_mcp.planner import BeaconSpec, Defaults, LineSpec, machine_stats, plan


def item(n: str, a: float) -> JSON:
    return {'type': 'item', 'name': n, 'amount': a}


def machine(categories: list[str], slots: int, **extra: Any) -> JSON:
    return {
        'crafting_speed': 1,
        'crafting_categories': categories,
        'energy_usage': '100kW',
        'energy_source': {'type': 'electric', 'drain': '0W'},
        'module_slots': slots,
        'allowed_effects': ['speed', 'productivity', 'consumption', 'pollution'],
        **extra,
    }


def beacon(effectivity: float, usage: str, **extra: Any) -> JSON:
    return {
        'distribution_effectivity': effectivity,
        'module_slots': 2,
        'allowed_effects': ['speed', 'consumption'],
        'energy_usage': usage,
        **extra,
    }


RAW: JSON = {
    'recipe': {
        # 1 A -> 1 B in 1 s, so one machine without speed effects runs one craft per second.
        'ab': {
            'name': 'ab',
            'category': 'crafting',
            'energy_required': 1,
            'allow_productivity': True,
            'ingredients': [item('a', 1)],
            'results': [item('b', 1)],
        },
        'void-b': {
            'name': 'void-b',
            'category': 'void',
            'energy_required': 1,
            'ingredients': [item('b', 10)],
            'results': [{'type': 'item', 'name': 'token', 'amount': 1, 'probability': 0}],
        },
    },
    'assembling-machine': {
        'asm': machine(['crafting'], 2),
        'picky': machine(['picky'], 1, allowed_module_categories=['speed']),
        'vent': machine(['void'], 1),
    },
    'module': {
        'spd': {'category': 'speed', 'effect': {'speed': 0.5}},
        'pm': {'category': 'productivity', 'effect': {'productivity': 0.5}},
    },
    'beacon': {
        'bcn': beacon(0.5, '100kW'),
        # A Nullius-style small beacon and its interference variants, placed by the same item.
        'nb': beacon(0.4, '150kW'),
        'nb-1': beacon(0.36, '150kW', placeable_by={'item': 'nb', 'count': 1}),
        'nb-3': beacon(0.24, '120kW', placeable_by={'item': 'nb', 'count': 1}),
    },
    'technology': {},
}
RAW['recipe']['picky'] = {**RAW['recipe']['ab'], 'name': 'picky', 'category': 'picky'}


@pytest.fixture(scope='module')
def db() -> Database:
    return Database(raw=RAW)


def approx(v: float, rel: float = 1e-9) -> Any:
    return pytest.approx(v, rel=rel, abs=rel)


SHARED = BeaconSpec(beacon='bcn', count=1, per_machine=0.5, modules=['spd', 'spd'])


def test_per_machine_counts(db: Database) -> None:
    s = machine_stats(db, 'ab', modules=['spd', 'spd'], beacons=[SHARED], validate_stage=False)
    # Two own modules plus half a beacon holding two: three modules per machine.
    assert s.module_counts == {'spd': approx(3)} and s.beacon_items == {'bcn': approx(0.5)}


def test_beacon_count_and_module_inventory(db: Database) -> None:
    line = LineSpec(recipe='ab', modules=['spd', 'spd'], beacons=[SHARED], fixed_machines=2)
    r = plan(db, {}, [line], validate_stage=False)
    (row,) = r.lines
    assert row.beacon_count == approx(1) and r.totals.beacon_count == approx(1)
    assert r.totals.beacon_count_by_type == {'bcn': approx(1)}
    assert row.beacon_power_MW == approx(0.1) and r.totals.beacon_power_MW == approx(0.1)
    assert r.module_inventory['spd'].count == approx(6) and r.module_inventory['spd'].count_ceil == 6
    # 1.2 machines round up to 2, which share ceil(2 x 0.5) = 1 beacon.
    r = plan(db, {}, [line.model_copy(update={'fixed_machines': 1.2})], validate_stage=False)
    assert r.lines[0].beacon_count == approx(0.6) and r.lines[0].beacon_count_ceil == 1
    assert r.module_inventory['spd'].count == approx(1.2 * 2 + 0.6 * 2) and r.module_inventory['spd'].count_ceil == 6


def test_module_limit_allocates_between_configurations(db: Database) -> None:
    lines: list[str | LineSpec] = [
        LineSpec(recipe='ab', id='plain', modules=[]),
        LineSpec(recipe='ab', id='prod', modules=['pm']),
    ]
    free = plan(db, {'b': 10}, lines, objective='imports', validate_stage=False)
    assert free.imports['item:a'] == approx(10 / 1.5, 1e-7)
    r = plan(db, {'b': 10}, lines, objective='imports', limits={'modules': {'pm': 4}}, validate_stage=False)
    assert r.imports['item:a'] == approx(8, 1e-7)
    assert r.module_inventory['pm'].count == approx(4, 1e-7)
    assert [b.constraint for b in r.bottlenecks] == ['modules:pm']


def test_beacon_limits(db: Database) -> None:
    # One beacon per machine (count 1, effectivity 0.5, one speed module): 1.25 crafts per machine.
    line = LineSpec(recipe='ab', modules=[], beacons=[BeaconSpec(beacon='bcn', modules=['spd'])])
    r = plan(db, {'b': 1}, [line], mode='maximize', limits={'beacons': 2}, validate_stage=False)
    assert r.scale == approx(2.5, 1e-7) and [b.constraint for b in r.bottlenecks] == ['beacons']
    r = plan(db, {'b': 1}, [line], mode='maximize', limits={'beacons_by_type': {'bcn': 3}}, validate_stage=False)
    assert r.scale == approx(3.75, 1e-7) and r.bottlenecks[0].constraint == 'beacons_by_type:bcn'
    with pytest.raises(ValueError, match='Unknown beacon'):
        plan(db, {'b': 1}, [line], limits={'beacons_by_type': {'nope': 1}}, validate_stage=False)
    with pytest.raises(ValueError, match='Unknown module'):
        plan(db, {'b': 1}, [line], limits={'modules': {'nope': 1}}, validate_stage=False)


def test_interference_variants(db: Database) -> None:
    def stats(spec: BeaconSpec) -> Any:
        return machine_stats(db, 'ab', modules=[], beacons=[spec], validate_stage=False)

    s = stats(BeaconSpec(beacon='nb', interference=3, modules=['spd']))
    (row,) = s.beacons
    assert (row.entity, row.item, row.interference) == ('nb-3', 'nb', 3) and row.effect_factor == approx(0.24)
    assert s.power_per_machine_MW.beacons == approx(0.12) and s.beacon_items == {'nb': 1}
    direct = stats(BeaconSpec(beacon='nb-3', modules=['spd'])).beacons[0]
    assert (direct.entity, direct.interference, direct.effect_factor) == ('nb-3', 3, approx(0.24))
    with pytest.raises(ValueError, match='no interference variant 2'):
        stats(BeaconSpec(beacon='nb', interference=2))
    with pytest.raises(ValueError, match='not subject to interference'):
        stats(BeaconSpec(beacon='bcn', interference=1))
    with pytest.raises(ValueError, match='is interference variant 3'):
        stats(BeaconSpec(beacon='nb-3', interference=1))


def test_pin_keeps_interference(db: Database) -> None:
    line = LineSpec(recipe='ab', modules=[], beacons=[BeaconSpec(beacon='nb', interference=1, modules=['spd'])])
    r = plan(db, {'b': 1}, [line], validate_stage=False)
    (pinned,) = r.lines_for_matrix
    assert pinned.beacons is not None and pinned.beacons[0].interference == 1
    again = plan(db, {'b': 1}, r.lines_for_matrix, validate_stage=False)
    assert again.lines[0].speed_multiplier == approx(r.lines[0].speed_multiplier)


def test_allowed_module_categories(db: Database) -> None:
    with pytest.raises(ValueError, match='category productivity not allowed by entity'):
        machine_stats(db, 'picky', modules=['pm'], validate_stage=False)
    assert machine_stats(db, 'picky', modules=['spd'], validate_stage=False).speed_multiplier == approx(1.5)
    line = LineSpec(recipe='picky', modules=['pm'], ignore_module_rules=True)
    assert plan(db, {'b': 1}, [line], validate_stage=False).lines[0].productivity == approx(0.5)


def test_disposal_machines_take_modules(db: Database) -> None:
    lines: list[str | LineSpec] = [LineSpec(recipe='ab', modules=[], fixed_machines=45)]
    r = plan(db, {}, lines, disposal_defaults={'modules': ['spd']}, validate_stage=False)
    # 45 b/s at 10 per craft and speed 1.5: 3 vents, each with one speed module.
    (row,) = r.disposal
    assert row.machine == 'vent' and row.machines == approx(3) and r.module_inventory['spd'].count == approx(3)


def test_reserved_fields_are_rejected(db: Database) -> None:
    with pytest.raises(ValueError, match='module_options is reserved'):
        plan(db, {'b': 1}, ['ab'], defaults=Defaults(module_options=[]), validate_stage=False)
    with pytest.raises(ValueError, match='reserved'):
        plan(db, {'b': 1}, ['ab'], weights={'beacons': 1}, validate_stage=False)
    assert plan(db, {'b': 1}, ['ab'], weights={'beacons': 0}, validate_stage=False).status == 'optimal'
