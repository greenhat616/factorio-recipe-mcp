"""Golden values come from executing pinned Helmod Lua, not a second planner invocation."""

import json
from pathlib import Path

import pytest
from test_energy import RAW

from recipe_mcp.database import Database
from recipe_mcp.planner import plan
from recipe_mcp.planner.schema import BeaconSpec, Defaults, LineSpec, RecipeEntry

REFERENCE = json.loads((Path(__file__).parent / 'fixtures/helmod-2.2.14.json').read_text())


@pytest.mark.parametrize('case', REFERENCE['factories'])
def test_helmod_factory_effects(case: dict[str, float]) -> None:
    db = Database(
        raw={
            'recipe': {
                'recipe': {'energy_required': 2, 'ingredients': [], 'results': [{'name': 'product', 'amount': 1}]}
            },
            'assembling-machine': {
                'asm': {
                    'crafting_speed': 1,
                    'crafting_categories': ['crafting'],
                    'energy_usage': '100kW',
                    'energy_source': {'type': 'electric', 'drain': '0W'},
                    'module_slots': 1,
                    'allowed_effects': ['speed', 'consumption'],
                }
            },
            'module': {
                'module': {'category': 'speed', 'effect': {'speed': case['speed'], 'consumption': case['consumption']}}
            },
            'beacon': {
                'beacon': {
                    'module_slots': 1,
                    'energy_usage': '20kW',
                    'distribution_effectivity': 0,
                    'allowed_effects': ['speed', 'consumption'],
                }
            },
            'technology': {},
        }
    )
    beacons = [BeaconSpec(beacon='beacon', count=int(case['beacon']), modules=['module'])] if case['beacon'] else []
    result = plan(
        db, {'product': 1}, lines=[LineSpec(recipe='recipe', modules=['module'], beacons=beacons)], validate_stage=False
    )
    assert result.lines[0].machines == pytest.approx(case['machines'])
    assert result.totals.power_MW * 1e6 == pytest.approx(case['power_W'], abs=1)


@pytest.mark.parametrize('case', REFERENCE['products'])
def test_helmod_expected_product(case: dict) -> None:
    entry = RecipeEntry(name='product', **case['element'])
    assert entry.output(case['bonus'], 'helmod') == pytest.approx(case['output'])


def test_helmod_known_probability_catalyst_difference() -> None:
    case = REFERENCE['products'][4]
    entry = RecipeEntry(name='product', **case['element'])
    assert case['output'] == 2.5
    assert entry.output(case['bonus']) == 2.75
    # Helmod subtracts ignored output after probability; the existing planner discounts it before probability.
    assert entry.output(case['bonus'], 'helmod') == case['output']


def test_helmod_energy_values() -> None:
    db = Database(raw=RAW)
    result = plan(db, {'energy:electric': 9}, lines=['generate:gen'], energy_mode='balance', validate_stage=False)
    assert result.lines[0].outputs['energy:electric'] / result.lines[0].machines * 1e6 == pytest.approx(
        REFERENCE['generator_W']
    )
    result = plan(db, {'energy:heat': 50}, lines=[LineSpec(recipe='heat:nuke', fuel='rod')], validate_stage=False)
    assert result.imports['item:rod'] == pytest.approx(REFERENCE['fuel_per_second'])
    assert result.lines[0].outputs['energy:heat'] * 1e6 == REFERENCE['reactor_W']
    result = plan(db, {'energy:electric': 0.07}, lines=['solar:panel'], validate_stage=False)
    assert result.lines[0].outputs['energy:electric'] * 1e6 == pytest.approx(REFERENCE['solar_peak_W'] * 0.7)
    assert REFERENCE['neighbours'][:3] == [0, 1, 2]


def test_helmod_productivity_mode_survives_pin() -> None:
    db = Database(
        raw={
            'recipe': {
                'r': {
                    'energy_required': 1,
                    'allow_productivity': True,
                    'ingredients': [],
                    'results': [{'name': 'product', 'amount': 4, 'probability': 0.5, 'ignored_by_productivity': 1}],
                }
            },
            'assembling-machine': {
                'asm': {
                    'crafting_speed': 1,
                    'crafting_categories': ['crafting'],
                    'energy_source': {'type': 'void'},
                    'module_slots': 1,
                    'allowed_effects': ['productivity'],
                }
            },
            'module': {'prod': {'category': 'productivity', 'effect': {'productivity': 0.5}}},
            'technology': {},
        }
    )
    r = plan(
        db,
        {'product': 2.5},
        lines=['r'],
        defaults=Defaults(modules=['prod'], productivity_model='helmod'),
        validate_stage=False,
    )
    assert r.lines[0].machines == pytest.approx(1)
    pinned = plan(db, {'product': 2.5}, lines=r.lines_for_matrix, validate_stage=False)
    assert pinned.lines[0].machines == pytest.approx(1)
    assert pinned.lines[0].productivity_model == 'helmod'
