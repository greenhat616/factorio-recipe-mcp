"""Helmod export strings: the serpent reader, plan export and import, and parity with Helmod's own results.

The fixtures are real Helmod 2.2.14 exports (serpent.dump + helpers.encode_string, as Helmod's Export tab
writes them) of Nullius models, with player names removed. Their machine counts are Helmod's own.
"""

import base64
import math
import zlib
from pathlib import Path
from typing import Any

import pytest
from test_plans import RAW, block

from recipe_mcp.database import Database
from recipe_mcp.helmod.codec import LuaError, decode, dump_value, encode, parse_lua
from recipe_mcp.helmod.convert import export_helmod, import_helmod
from recipe_mcp.plans import PlanStore
from recipe_mcp.plans.factory import solve_plan
from recipe_mcp.plans.models import TargetRef

FIXTURES = Path(__file__).parent / 'fixtures/helmod-models'
# Written by Factorio 2.0's serpent.dump: the second reference to a table is a placeholder plus a fixup.
SERPENT = (
    'do local _={root={children={k={x=1}}},blocks={k="SERPENT PLACEHOLDER"},s="q\\"uote\\n",'
    'f=0.3333333333333333,big=1e+300,neg=-2.5,list={1,2,nil,4}};local __={};_.blocks.k=_.root.children.k;'
    'return _;end'
)


@pytest.fixture
def store(tmp_path: Path) -> PlanStore:
    return PlanStore(tmp_path / 'plans', Database(raw=RAW, progress={'tick': 1}))


def machines(store: PlanStore, name: str) -> dict[str, dict[str, float]]:
    return {
        o.id: {line.id: line.machines for line in o.result.lines}
        for o in solve_plan(store, name, detail='full', save_results=False).blocks
        if o.result
    }


def test_reads_serpent_dump_with_shared_tables() -> None:
    t = parse_lua(SERPENT)
    assert t['blocks']['k'] is t['root']['children']['k'] == {'x': 1}
    assert t['s'] == 'q"uote\n' and t['f'] == 1 / 3 and t['big'] == 1e300 and t['neg'] == -2.5
    assert t['list'] == {1: 1, 2: 2, 4: 4}


@pytest.mark.parametrize(
    'text',
    [
        'do local _={};os.execute("x");return _;end',
        '{a=print("x")}',
        'do local _={} return _ end x',
        '{a=1',
        'do local _={};_.a=_.missing.b;return _;end',
    ],
)
def test_rejects_anything_but_tables(text: str) -> None:
    with pytest.raises(LuaError):
        parse_lua(text)


def test_dump_round_trip() -> None:
    value = {
        'name': 'a "b"\n\\c\x01é',
        'n': 3,
        'x': 0.1,
        'inf': math.inf,
        'list': [1, 2.5, 'z'],
        'nested': {'true': True, 'end': False, 5: 'five'},
    }
    back = parse_lua(dump_value(value))
    assert back == {**value, 'list': {1: 1, 2: 2.5, 3: 'z'}}
    assert decode(encode(value)) == back


def test_decode_errors() -> None:
    with pytest.raises(LuaError, match='base64'):
        decode('not a helmod string!')
    plain = base64.b64encode(zlib.compress(b'return 1')).decode()
    with pytest.raises(LuaError):
        decode(plain)


def test_export_writes_helmod_model(store: PlanStore) -> None:
    store.save(
        'p',
        [
            block(
                'main',
                targets={'c': 1},
                lines=[
                    'bc',
                    {
                        'recipe': 'ab',
                        'modules': ['pm1', 'pm2'],
                        'beacons': [{'beacon': 'bcn', 'count': 2, 'per_machine': 0.5, 'modules': ['spd']}],
                    },
                ],
            ),
            block('fixed', lines=[{'recipe': 'ab', 'fixed_machines': 3}]),
            block('eat', consume={'b': 4}, lines=['bc'], solver='matrix'),
        ],
        per='minute',
    )
    out = export_helmod(store, 'p')
    model = decode(out.text or '')
    assert out.blocks == ['main', 'fixed', 'eat'] and model['time'] == out.time == 60
    main, fixed, eat = sorted(model['block_root']['children'].values(), key=lambda b: b['index'])
    assert main['products'] == {'c': {'name': 'c', 'type': 'item', 'input': 1}} and main['solver'] is True
    bc, ab = sorted(main['children'].values(), key=lambda c: c['index'])
    assert (bc['name'], bc['type'], bc['factory']['name']) == ('bc', 'recipe', 'asm')
    assert ab['factory']['modules'] == {
        1: {'name': 'pm1', 'quality': 'normal', 'amount': 1},
        2: {'name': 'pm2', 'quality': 'normal', 'amount': 1},
    }
    assert ab['beacons'][1] | {'modules': None} == {
        'class': 'Beacon',
        'name': 'bcn',
        'type': 'entity',
        'combo': 2,
        'per_factory': 0.5,
        'per_factory_constant': 0,
        'modules': None,
    }
    assert fixed['by_factory'] is True and fixed['children']['R2_1']['factory']['input'] == 3
    assert eat['by_product'] is False and eat['solver'] is False
    assert eat['ingredients'] == {'b': {'name': 'b', 'type': 'item', 'input': 4}}


def test_export_import_round_trip(store: PlanStore) -> None:
    lines: list[Any] = [
        'bc',
        {'recipe': 'ab', 'modules': ['pm1'], 'beacons': [{'beacon': 'bcn', 'count': 1, 'modules': ['spd']}]},
    ]
    store.save('p', [block('main', targets={'c': 2}, lines=lines)], per='minute')
    back = import_helmod(store, export_helmod(store, 'p').text or '', 'q', validate_stage=False)
    assert back.per == 'minute' and back.warnings == []
    req = back.blocks[0].request
    assert req.targets == {'c': 2} and req.auto_discover is False and req.solver == 'lp'
    assert [(s.recipe, s.machine, s.modules, s.beacons) for s in req.lines] == [  # type: ignore[union-attr]
        ('bc', 'asm', [], []),
        ('ab', 'asm', ['pm1'], [req.lines[1].beacons[0]]),  # type: ignore[union-attr,index]
    ]
    assert list(machines(store, 'q').values()) == list(machines(store, 'p').values())


def test_export_freezes_links(store: PlanStore) -> None:
    store.save(
        'p',
        [
            block('c-side', targets={'c': 1}, lines=['bc']),
            block('b-side', targets={'b': {'from': ['c-side'], 'plus': 1}}, lines=['ab']),
        ],
    )
    out = export_helmod(store, 'p')
    assert 'b-side: links to other blocks exported as their current amounts' in out.warnings
    blocks = sorted(decode(out.text or '')['block_root']['children'].values(), key=lambda b: b['index'])
    assert blocks[1]['products']['b']['input'] == pytest.approx(3)


def nested_model() -> dict[str, Any]:
    """A Helmod-shaped model: block c makes c from b, its linked child block makes b."""
    child = {
        'class': 'Block',
        'id': 'block_2',
        'index': 1,
        'name': 'ab',
        'unlinked': False,
        'products': {'b': {'name': 'b', 'type': 'item', 'amount': 1, 'state': 1}},
        'children': {'R2': {'class': 'Recipe', 'index': 0, 'name': 'ab', 'type': 'recipe', 'factory': {'name': 'asm'}}},
    }
    return {
        'time': 1,
        'block_root': {
            'class': 'Block',
            'id': 'block_1',
            'name': 'bc',
            'products': {'c': {'name': 'c', 'type': 'item', 'input': 0.5}},
            'children': {
                'R1': {'class': 'Recipe', 'index': 0, 'name': 'bc', 'type': 'recipe', 'factory': {'name': 'asm'}},
                'block_2': child,
            },
        },
    }


def test_import_links_child_blocks(store: PlanStore) -> None:
    r = import_helmod(store, encode(nested_model()), 'n', validate_stage=False)
    parent, child = r.blocks
    assert parent.request.targets == {'c': 0.5} and child.request.targets == {
        'b': TargetRef.model_validate({'from': ['bc']})
    }
    assert machines(store, 'n') == {'bc': {'bc': 1.0}, 'ab': {'ab': 1.0}}


def test_import_rejects_unknown_and_empty(store: PlanStore) -> None:
    model = nested_model()
    model['block_root']['children']['R1']['name'] = 'nope'
    with pytest.raises(ValueError, match='nope'):
        import_helmod(store, encode(model), 'x', validate_stage=False)
    with pytest.raises(ValueError, match='no recipes'):
        import_helmod(store, encode({'time': 1, 'block_root': {'children': {}}}), 'x')
    with pytest.raises(ValueError, match='block_root'):
        import_helmod(store, encode({'blocks': {}}), 'x')
    assert not store.exists('x')


def test_dry_run_writes_nothing(store: PlanStore) -> None:
    r = import_helmod(store, encode(nested_model()), 'n', validate_stage=False, dry_run=True)
    assert not r.saved and not store.exists('n')


@pytest.mark.parametrize('path', sorted(FIXTURES.glob('*.txt')), ids=lambda p: p.stem)
def test_real_models_match_helmod(real_db: Database, tmp_path: Path, path: Path) -> None:
    store = PlanStore(tmp_path, real_db)
    r = import_helmod(store, path.read_text(), path.stem, validate_stage=False)
    got = machines(store, path.stem)
    for bid, lines in r.helmod_machines.items():
        for lid, count in lines.items():
            assert got[bid].get(lid, 0.0) == pytest.approx(count, rel=1e-6, abs=1e-9), (bid, lid)
    exported = decode(export_helmod(store, path.stem).text or '')
    assert exported['time'] == {'second': 1, 'minute': 60, 'hour': 3600}[r.per]


def test_real_model_details(real_db: Database, tmp_path: Path) -> None:
    store = PlanStore(tmp_path, real_db)
    text = (FIXTURES / 'model_30.txt').read_text()
    lime, steel = import_helmod(store, text, 'm30', validate_stage=False).blocks
    # The root has no recipes; its 15 steel/s go to the linked child that makes steel.
    assert lime.request.consume == {'nullius-crushed-limestone': 2.5} and steel.request.targets == {
        'nullius-steel-ingot': 15
    }
    r = import_helmod(store, (FIXTURES / 'model_66.txt').read_text(), 'm66', validate_stage=False)
    assert any('imported for the LP' in w for w in r.warnings) and r.blocks[0].request.solver == 'lp'
    r = import_helmod(store, (FIXTURES / 'model_4.txt').read_text(), 'm4', validate_stage=False)
    assert r.blocks[0].request.targets == {'energy:electric': pytest.approx(1)}
    assert r.blocks[0].request.energy_mode == 'balance'


def test_exchange_through_files(store: PlanStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('recipe_mcp.helmod.convert.EXCHANGE_DIR', tmp_path / 'helmod')
    store.save('p', [block('main', targets={'c': 1}, lines=['bc', 'ab'])])
    out = export_helmod(store, 'p', path='out/p.txt')
    assert out.text is None and out.path == str((tmp_path / 'helmod/out/p.txt').resolve())
    with pytest.raises(ValueError, match='overwrite'):
        export_helmod(store, 'p', path='out/p.txt')
    assert export_helmod(store, 'p', path='out/p.txt', overwrite=True).path == out.path
    back = import_helmod(store, '', 'q', validate_stage=False, path='out/p.txt')
    assert back.blocks[0].request.targets == {'c': 1}
    with pytest.raises(ValueError, match='either text or path'):
        import_helmod(store, 'x', 'r', path='out/p.txt')
    with pytest.raises(ValueError, match='No file'):
        import_helmod(store, '', 'r', path='missing.txt')
