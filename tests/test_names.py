"""Names are presentation data: exact IDs and solver arithmetic remain unchanged."""

import json
import re
import zipfile
from pathlib import Path

import pytest
from test_plans import RAW

from recipe_mcp.database import Database
from recipe_mcp.export.locales import parse_cfg, snapshot
from recipe_mcp.names import Names
from recipe_mcp.paths import DATA_DIR
from recipe_mcp.planner import plan
from recipe_mcp.plans import PlanStore
from recipe_mcp.plans.factory import solve_plan
from recipe_mcp.plans.models import BlockInput


@pytest.fixture
def names() -> Names:
    return Names(
        {
            'item': {
                'ore': {},
                'plant': {'place_result': 'plant'},
                'armor': {'place_as_equipment_result': 'shield'},
                'box': {'localised_name': ['pack', ['', ['item-name.ore'], ' 2']]},
                'alias': {'localised_name': ['?', ['missing.key'], ['item-name.ore']]},
                'literal': {'localised_name': 'Literal name'},
                'ref': {'localised_name': ['reference']},
                'cycle': {'localised_name': ['cyclic']},
                'unsupported': {'localised_name': ['control']},
            },
            'fluid': {'ore': {}, 'steam': {}},
            'assembling-machine': {'plant': {'collision_box': []}},
            'energy-shield-equipment': {'shield': {}},
            'recipe': {
                'ore': {'results': [{'name': 'ore', 'type': 'item'}]},
                'steam': {'main_product': 'steam', 'results': [{'name': 'ore'}, {'name': 'steam', 'type': 'fluid'}]},
                'own': {'main_product': '', 'results': [{'name': 'ore'}]},
                'explicit': {'localised_name': ['pack', 'A'], 'results': [{'name': 'ore'}]},
            },
        },
        {
            'en': {
                'item-name.ore': 'Ore',
                'fluid-name.ore': 'Liquid ore',
                'fluid-name.steam': 'Steam',
                'entity-name.plant': 'Chemical plant',
                'equipment-name.shield': 'Shield',
                'recipe-name.own': 'Special recipe',
                'pack': 'Box of __1__',
                'reference': '__ITEM__ore__ / __FLUID__steam__',
                'cyclic': '__ITEM__cycle__',
                'control': '__CONTROL__open-gui__',
            },
            'zh-CN': {'item-name.ore': '矿石', 'pack': '__1__箱'},
        },
    )


@pytest.mark.parametrize(
    ('kind', 'name', 'expected'),
    [
        ('item', 'box', 'Box of Ore 2'),
        ('item', 'alias', 'Ore'),
        ('item', 'plant', 'Chemical plant'),
        ('item', 'armor', 'Shield'),
        ('item', 'ref', 'Ore / Steam'),
        ('item', 'literal', 'Literal name'),
        ('recipe', 'ore', 'Ore'),
        ('recipe', 'steam', 'Steam'),
        ('recipe', 'own', 'Special recipe'),
        ('recipe', 'explicit', 'Box of A'),
        ('fluid', 'ore', 'Liquid ore'),
    ],
)
def test_name_rules(names: Names, kind: str, name: str, expected: str) -> None:
    result = names.get(kind, name)
    assert result.display_name == expected
    assert result.name == name and result.language == 'en' and result.name_status == 'translated'


def test_requested_language_and_fallback_are_observable(names: Names) -> None:
    assert names.get('item', 'box', 'zh-CN').display_name == '矿石 2箱'
    fallback = names.get('fluid', 'steam', 'zh-CN')
    assert (fallback.display_name, fallback.name_status, fallback.resolved_language) == ('Steam', 'fallback', 'en')
    assert names.get('item', 'ore', 'xx').name_status == 'fallback'
    for name in ('missing', 'cycle', 'unsupported'):
        result = names.get('item', name)
        assert result.display_name == name and result.name_status == 'raw' and result.resolved_language is None
    with pytest.raises(ValueError, match='language code'):
        names.get('item', 'ore', '../en')


def test_snapshot_hash_and_absence(tmp_path: Path, names: Names) -> None:
    path = tmp_path / 'locales.json'
    assert Names.from_snapshot(names.raw, path, 'correct').warnings
    path.write_text(json.dumps({'raw_sha256': 'correct', 'catalogs': names.catalogs}), encoding='utf-8')
    assert Names.from_snapshot(names.raw, path, 'wrong').get('item', 'ore').name_status == 'raw'
    assert Names.from_snapshot(names.raw, path, 'correct').get('item', 'ore').display_name == 'Ore'


def test_cfg_and_mod_override_order(tmp_path: Path) -> None:
    assert parse_cfg('\ufeff# comment\n[item-name]\nx= a=b\nx=final\n;ignored') == {'item-name.x': 'final'}
    mods, game = tmp_path / 'mods', tmp_path / 'game'
    mods.mkdir()
    for name, version, text in [('base', '2.0.77', 'Ore'), ('translation', '1.2.3', '矿石')]:
        lang = 'en' if name == 'base' else 'zh-CN'
        with zipfile.ZipFile(mods / f'{name}_{version}.zip', 'w') as archive:
            archive.writestr(f'{name}/info.json', json.dumps({'name': name, 'version': version}))
            archive.writestr(f'{name}/locale/{lang}/names.cfg', '[item-name]\nore=' + text)
            if name == 'translation':
                archive.writestr(f'{name}/locale/en/names.cfg', '[item-name]\nore=Overridden ore')
    log = '  translation = "1.2.3",\nChecksum of base: 1\nChecksum of translation: 0\nChecksum of unknown: 0'
    result = snapshot({'raw_sha256': 'sha', 'loaded_mod_versions': {'base': '2.0.77'}}, game, mods, log)
    assert result['catalogs']['en']['item-name.ore'] == 'Overridden ore'
    assert result['catalogs']['zh-CN']['item-name.ore'] == '矿石'
    assert any('unknown' in warning for warning in result['warnings'])
    assert result['sources'][0]['mod'] == 'base'
    with pytest.raises(ValueError, match='Missing exported mod version'):
        snapshot({'raw_sha256': 'sha', 'loaded_mod_versions': {'base': '9.9.9'}}, game, mods)


def test_catalog_keeps_types_temperatures_and_channels(names: Names) -> None:
    result = names.catalog(
        {
            'items': [{'item': 'fluid:steam~open-2@165'}],
            'lines': [{'recipe': 'steam', 'machine': 'plant', 'modules': ['ore']}],
            'targets': {'energy:electric': 1, 'item:ore': 2},
        },
        ['zh-CN'],
    )
    assert set(result.translations) == {'zh-CN', 'en'}
    en = result.translations['en']
    assert en['fluid:steam~open-2@165'].display_name == 'Steam~open-2@165'
    assert en['recipe:steam'].display_name == 'Steam'
    assert en['entity:plant'].display_name == 'Chemical plant'
    assert en['item:ore'].display_name == 'Ore'
    assert en['energy:electric'].display_name == 'Electricity'


def test_language_does_not_change_solver_or_saved_requests(tmp_path: Path) -> None:
    raw = {**RAW, 'item': {**RAW.get('item', {}), 'b': {}}}
    db = Database(raw=raw)
    db.names = Names(raw, {'en': {'item-name.b': 'Output'}, 'zh-CN': {'item-name.b': '产物'}})
    a = plan(db, {'b': 1}, lines=['ab'], graph='bipartite', validate_stage=False)
    b = plan(db, {'b': 1}, lines=['ab'], graph='bipartite', validate_stage=False, language='zh-CN')
    assert a.model_dump(exclude={'names'}) == b.model_dump(exclude={'names'})
    assert b.names and b.names.translations['zh-CN']['item:b'].display_name == '产物'
    store = PlanStore(tmp_path, db)
    store.save(
        'names',
        [
            BlockInput.model_validate(
                {'id': 'block', 'request': {'targets': {'b': 1}, 'lines': ['ab'], 'validate_stage': False}}
            )
        ],
    )
    before = store.read('names').model_dump()
    result = solve_plan(store, 'names', detail='full', graph='full', save_results=False, language='zh-CN')
    assert result.names and result.names.language == 'zh-CN'
    assert result.blocks[0].result and result.blocks[0].result.names
    assert result.blocks[0].result.names.language == 'zh-CN'
    assert store.read('names').model_dump() == before


def test_real_nullius_names(real_db: Database) -> None:
    if not real_db.names.catalogs:
        pytest.skip('Locale snapshot is not installed')
    assert real_db.names.get('recipe', 'nullius-pressure-methanol').display_name == 'Methanol (pressurized)'
    for lang in ('en', 'zh-CN', 'de'):
        value = real_db.names.get('recipe', 'nullius-pressure-methanol', lang)
        assert value.display_name == real_db.names.catalogs[lang]['recipe-name.nullius-pressure-methanol']
    assert real_db.names.get('fluid', 'nullius-methanol', 'zh-CN').display_name == '甲醇'
    assert real_db.names.get('entity', 'nullius-chemical-plant-2').display_name == 'Chemical plant 2'
    assert real_db.names.get('item', 'nullius-speed-module-1').display_name == 'Speed module 1'


def test_native_default_rules_and_embedded_references(names: Names) -> None:
    names.raw['technology'] = {'test-1': {}, 'test-2': {'max_level': 'infinite'}}
    names.catalogs['en']['technology-name.test'] = 'Testing'
    assert names.get('technology', 'test-1').display_name == 'Testing 1'
    assert names.get('technology', 'test-2').display_name == 'Testing'
    names.raw['recipe']['different'] = {'results': [{'name': 'ore'}]}
    names.catalogs['en']['recipe-name.different'] = 'Refining ore'
    assert names.get('recipe', 'different').display_name == 'Refining ore'
    names.groups['item']['embedded'] = {'localised_name': ['pack', '__ENTITY__plant__']}
    names.groups['entity']['plant']['localised_name'] = 'Overridden entity'
    assert names.get('item', 'embedded').display_name == 'Box of Chemical plant'
    names.groups['item']['plural'] = {'localised_name': ['plural', 2]}
    names.catalogs['en']['plural'] = '__plural_for_parameter__1__{1=item|rest=items}__'
    assert names.get('item', 'plural').name_status == 'raw'
    assert parse_cfg('[recipe-name]\nx= repair ') == {'recipe-name.x': ' repair '}


@pytest.mark.parametrize('language', ['en', 'zh-CN'])
@pytest.mark.parametrize('kind', ['recipe', 'item', 'fluid', 'entity', 'technology'])
def test_against_native_locale_dump(real_db: Database, language: str, kind: str) -> None:
    work = DATA_DIR / 'reports/helmod-gaps/native-locale' / language
    path = work / f'script-output/{kind}-locale.json'
    if not path.exists() or not real_db.names.catalogs:
        pytest.skip('Run scripts/validation/locale_reference.py to generate native references')
    native_log = (work / 'console.log').read_text(encoding='utf-8')
    export_log = (DATA_DIR / 'export-console.log').read_text(encoding='utf-8')
    if dict(re.findall(r'Checksum of (.+?): (\d+)', native_log)) != dict(
        re.findall(r'Checksum of (.+?): (\d+)', export_log)
    ):
        pytest.skip('Native locale reference uses different prototypes')
    names = json.loads(path.read_text(encoding='utf-8'))['names']
    mismatches = {
        n: (expected, real_db.names.get(kind, n, language).display_name)
        for n, expected in names.items()
        if expected != real_db.names.get(kind, n, language).display_name
    }
    assert not mismatches
