"""Plan store, edit operations, pinning and staleness on a synthetic prototype set in a temporary root."""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from recipe_mcp.database import JSON, Database
from recipe_mcp.planner import LineSpec, plan
from recipe_mcp.plans import PlanStore
from recipe_mcp.plans.models import BlockInput, BlockResult, EditOp, Fingerprint, TargetRef
from recipe_mcp.plans.store import now


def item(n: str, a: float) -> JSON:
    return {'type': 'item', 'name': n, 'amount': a}


RAW: JSON = {
    'recipe': {
        'ab': {
            'name': 'ab',
            'category': 'crafting',
            'energy_required': 1,
            'allow_productivity': True,
            'ingredients': [item('a', 1)],
            'results': [item('b', 1)],
        },
        'bc': {
            'name': 'bc',
            'category': 'crafting',
            'energy_required': 2,
            'ingredients': [item('b', 2)],
            'results': [item('c', 1)],
        },
    },
    'assembling-machine': {
        'asm': {
            'crafting_speed': 1,
            'crafting_categories': ['crafting'],
            'energy_usage': '100kW',
            'energy_source': {'type': 'electric', 'drain': '0W'},
            'module_slots': 2,
            'allowed_effects': ['speed', 'productivity', 'consumption', 'pollution'],
        }
    },
    'module': {
        'pm1': {'category': 'productivity', 'effect': {'productivity': 0.1}},
        'pm2': {'category': 'productivity', 'effect': {'productivity': 0.2}},
        'spd': {'category': 'speed', 'effect': {'speed': 0.5}},
        'spd2': {'category': 'speed', 'effect': {'speed': 0.7}},
    },
    'beacon': {
        'bcn': {
            'distribution_effectivity': 0.5,
            'module_slots': 2,
            'energy_usage': '100kW',
            'allowed_effects': ['speed', 'consumption'],
        }
    },
    'technology': {},
}
OPS = TypeAdapter(list[EditOp])


def ops(*items: dict[str, Any]) -> list[Any]:
    return OPS.validate_python(list(items))


def block(bid: str, **request: Any) -> BlockInput:
    return BlockInput.model_validate({'id': bid, 'request': {'validate_stage': False, **request}})


@pytest.fixture
def db() -> Database:
    return Database(raw=RAW, progress={'tick': 10, 'provenance': {'source_copy_sha256': 'save-1'}})


@pytest.fixture
def store(tmp_path: Path, db: Database) -> PlanStore:
    return PlanStore(tmp_path / 'plans', db)


def two_blocks() -> list[BlockInput]:
    return [
        block('b-side', targets={'b': 10}, lines=[{'recipe': 'ab', 'modules': ['pm1', 'pm1']}]),
        block('c-side', targets={'c': 1}, lines=['bc', {'recipe': 'ab', 'id': 'ab2', 'modules': ['pm1']}]),
    ]


@pytest.mark.parametrize('name', ['../x', 'a/b', '..', '.', 'x' * 65, '', 'a b', 'a\\b'])
def test_invalid_names(store: PlanStore, name: str) -> None:
    with pytest.raises(ValueError, match='Invalid plan name'):
        store.path(name)


def test_save_get_list(store: PlanStore) -> None:
    view = store.save('p1', two_blocks(), description='demo')
    assert view.plan.revision == 1 and [b.id for b in view.plan.blocks] == ['b-side', 'c-side']
    assert [s.name for s in store.list()] == ['p1'] and store.list()[0].blocks == 2
    with pytest.raises(ValueError, match='overwrite'):
        store.save('p1', two_blocks())
    assert store.save('p1', two_blocks()[:1], overwrite=True).plan.revision == 2
    on_disk = json.loads(store.path('p1').read_text(encoding='utf-8'))
    assert on_disk['schema'] == 1 and on_disk['blocks'][0]['request']['targets'] == {'b': 10}


def test_save_validates_before_writing(store: PlanStore) -> None:
    with pytest.raises(ValueError, match='Unknown recipe'):
        store.save('bad', [block('x', targets={'b': 1}, lines=['nope'])])
    with pytest.raises(ValueError, match='Extra inputs'):
        BlockInput.model_validate({'id': 'x', 'request': {'targetz': {}}})
    with pytest.raises(ValueError, match='lp solver'):
        store.save('bad', [block('x', targets={'b': 1}, solver='matrix', mode='maximize', lines=['ab'])])
    with pytest.raises(ValueError, match='lp solver'):
        store.save('bad', [block('x', targets={'b': 1}, solver='matrix', limits={'machines': 1}, lines=['ab'])])
    assert not store.path('bad').exists()


def test_failed_write_keeps_the_old_file(store: PlanStore, monkeypatch: pytest.MonkeyPatch) -> None:
    store.save('p1', two_blocks())
    before = store.path('p1').read_bytes()

    def boom(*_: Any) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(os, 'replace', boom)
    with pytest.raises(OSError):
        store.edit('p1', ops({'op': 'set_meta', 'description': 'changed'}))
    assert store.path('p1').read_bytes() == before
    assert [p.name for p in store.root.iterdir()] == ['p1.json']


def test_edit_is_atomic_and_versioned(store: PlanStore) -> None:
    store.save('p1', two_blocks())
    before = store.path('p1').read_bytes()
    bad = ops(
        {'op': 'set_target', 'block_id': 'b-side', 'item': 'b', 'value': 20},
        {'op': 'remove_line', 'block_id': 'b-side', 'line_id': 'missing'},
    )
    with pytest.raises(ValueError, match=r'ops\[1\] remove_line: No line missing'):
        store.edit('p1', bad)
    assert store.path('p1').read_bytes() == before
    r = store.edit('p1', ops({'op': 'set_target', 'block_id': 'b-side', 'item': 'b', 'value': 20}))
    assert r.revision == 2 and store.read('p1').block('b-side').request.targets == {'b': 20}
    with pytest.raises(ValueError, match='Revision conflict: expected 1, current 2'):
        store.edit('p1', ops({'op': 'set_meta', 'description': 'x'}), expected_revision=1)


def test_edit_operations(store: PlanStore) -> None:
    store.save('p1', two_blocks())
    r = store.edit(
        'p1',
        ops(
            {'op': 'add_line', 'block_id': 'b-side', 'line': {'recipe': 'ab', 'id': 'plain', 'modules': []}},
            {'op': 'update_line', 'block_id': 'b-side', 'line_id': 'plain', 'fields': {'max_machines': 3}},
            {'op': 'set_limit', 'block_id': 'b-side', 'key': 'power_MW', 'value': 5},
            {'op': 'set_limit', 'block_id': 'b-side', 'key': 'modules.pm1', 'value': 4},
            {'op': 'remove_limit', 'block_id': 'b-side', 'key': 'power_MW'},
            {'op': 'set_consume', 'block_id': 'c-side', 'item': 'a', 'value': 3},
            {'op': 'remove_consume', 'block_id': 'c-side', 'item': 'a'},
            {'op': 'update_request', 'block_id': 'c-side', 'fields': {'objective': 'imports', 'solver': None}},
            {'op': 'set_target', 'block_id': 'b-side', 'item': 'b', 'value': {'from': ['c-side'], 'plus': 1}},
            {'op': 'set_meta', 'per': 'minute'},
        ),
    )
    plan_ = store.read('p1')
    b = plan_.block('b-side').request
    assert [s if isinstance(s, str) else s.id or s.recipe for s in b.lines] == ['ab', 'plain']
    assert b.limits is not None and b.limits.power_MW is None and b.limits.modules == {'pm1': 4}
    assert plan_.block('c-side').request.consume == {} and plan_.block('c-side').request.objective == 'imports'
    assert r.warnings == ['per changed second -> minute; stored rates were not converted']
    with pytest.raises(ValueError, match='referenced by'):
        store.edit('p1', ops({'op': 'remove_block', 'block_id': 'c-side'}))
    store.edit('p1', ops({'op': 'rename_block', 'block_id': 'c-side', 'new_id': 'c2'}))
    ref = store.read('p1').block('b-side').request.targets['b']
    assert isinstance(ref, TargetRef) and ref.from_ == ['c2']
    with pytest.raises(ValueError, match='Unknown limit key'):
        store.edit('p1', ops({'op': 'set_limit', 'block_id': 'b-side', 'key': 'imports', 'value': 1}))
    with pytest.raises(ValueError, match='unknown or own block'):
        store.edit('p1', ops({'op': 'set_target', 'block_id': 'b-side', 'item': 'b', 'value': {'from': ['zz']}}))


def test_staleness(store: PlanStore, db: Database) -> None:
    store.save('p1', two_blocks())
    assert store.view('p1').stale == 'unknown'  # synthetic prototypes have no dump hash
    db.raw_sha256 = 'proto-1'
    store.save('p1', two_blocks(), overwrite=True)
    assert store.view('p1').stale == 'fresh'
    db.progress['tick'] = 11
    assert store.view('p1').stale == 'stage_changed' and store.list()[0].stale == 'stage_changed'
    db.raw_sha256 = 'proto-2'
    assert store.view('p1').stale == 'prototypes_changed'


def test_delete_moves_to_trash(store: PlanStore) -> None:
    store.save('p1', two_blocks())
    with pytest.raises(ValueError, match='confirm'):
        store.delete('p1', 'p2')
    trashed = Path(store.delete('p1', 'p1'))
    assert trashed.exists() and trashed.parent.name == '.trash'
    assert not store.exists('p1') and store.list() == []


def put_result(store: PlanStore, db: Database, name: str, bid: str) -> None:
    p = store.read(name)
    b = p.block(bid)
    targets = {k: float(v) for k, v in b.request.targets.items() if isinstance(v, float | int)}
    r = plan(db, targets, b.request.lines, validate_stage=False)
    fp = Fingerprint(prototype_raw_sha256=None, progress_tick=None, source_copy_sha256=None)
    b.result = BlockResult.from_plan(r, targets, fp, now())
    store.commit(p, p.revision)


def test_pin_replays_in_the_matrix_solver(store: PlanStore, db: Database) -> None:
    store.save('p1', [block('c', targets={'c': 1})])
    with pytest.raises(ValueError, match='run plan_solve first'):
        store.edit('p1', ops({'op': 'pin', 'block_id': 'c'}))
    put_result(store, db, 'p1', 'c')
    before = store.read('p1').block('c').result
    assert before is not None and before.totals is not None
    store.edit('p1', ops({'op': 'pin', 'block_id': 'c', 'solver': 'matrix'}))
    req = store.read('p1').block('c').request
    assert req.solver == 'matrix' and req.auto_discover is False and len(req.lines) == 2
    r = plan(db, {'c': 1}, req.lines, solver='matrix', imports=req.imports, validate_stage=False)
    assert r.totals.machines == pytest.approx(before.totals.machines, rel=1e-6)


def line_spec(store: PlanStore, bid: str, j: int) -> LineSpec:
    spec = store.read('p1').block(bid).request.lines[j]
    assert isinstance(spec, LineSpec)
    return spec


def test_module_operations(store: PlanStore, db: Database) -> None:
    store.save('p1', two_blocks())
    r = store.edit('p1', ops({'op': 'replace_module', 'from': 'pm1', 'to': 'pm2'}))
    assert r.notes == [
        'replace_module pm1 -> pm2: block b-side: 1 lines',
        'replace_module pm1 -> pm2: block c-side: 1 lines',
    ]
    assert line_spec(store, 'b-side', 0).modules == ['pm2', 'pm2']
    assert store.edit('p1', ops({'op': 'replace_module', 'from': 'pm1', 'to': 'pm2'})).warnings == [
        'replace_module: no pm1 found'
    ]
    # bc does not allow productivity: putting pm2 in it fails validation and nothing is saved.
    before = store.path('p1').read_bytes()
    with pytest.raises(ValueError, match='not allowed by recipe'):
        store.edit('p1', ops({'op': 'set_modules', 'block_id': 'c-side', 'line_id': 'bc', 'modules': ['pm2']}))
    assert store.path('p1').read_bytes() == before
    store.edit(
        'p1',
        ops(
            {'op': 'set_modules', 'block_id': 'c-side', 'line_id': 'defaults', 'modules': ['spd']},
            {'op': 'set_modules', 'block_id': 'c-side', 'line_id': 'ab2', 'modules': None},
            {
                'op': 'set_beacons',
                'block_id': 'b-side',
                'line_id': 'ab',
                'beacons': [{'beacon': 'bcn', 'modules': ['spd'], 'per_machine': 0.5}],
            },
        ),
    )
    c = store.read('p1').block('c-side').request
    assert c.defaults is not None and c.defaults.modules == ['spd'] and line_spec(store, 'c-side', 1).modules is None
    with pytest.raises(ValueError, match='productivity not allowed by entity'):
        store.edit('p1', ops({'op': 'replace_module', 'from': 'spd', 'to': 'pm1', 'block_id': 'b-side'}))
    r = store.edit('p1', ops({'op': 'replace_module', 'from': 'spd', 'to': 'spd2', 'block_id': 'b-side'}))
    assert r.notes == ['replace_module spd -> spd2: block b-side: 1 lines']
    beacons = line_spec(store, 'b-side', 0).beacons
    assert beacons is not None and beacons[0].modules == ['spd2']


@pytest.mark.realdata
def test_real_methanol_pin(tmp_path: Path, real_db: Database, force: str) -> None:
    store = PlanStore(tmp_path, real_db)
    store.save(
        'methanol',
        [BlockInput.model_validate({'id': 'm', 'request': {'targets': {'nullius-methanol': 10}}})],
        force=force,
    )
    assert store.view('methanol').stale == 'fresh'
    p = store.read('methanol')
    r = plan(real_db, {'nullius-methanol': 10}, force=force)
    p.block('m').result = BlockResult.from_plan(r, {'nullius-methanol': 10}, store.fingerprint(), now())
    store.commit(p, p.revision)
    store.edit('methanol', ops({'op': 'pin', 'block_id': 'm', 'solver': 'matrix'}))
    req = store.read('methanol').block('m').request
    mx = plan(
        real_db,
        {'nullius-methanol': 10},
        req.lines,
        solver='matrix',
        imports=req.imports,
        surplus_items=req.surplus_items,
        force=force,
    )
    assert mx.totals.machines == pytest.approx(r.totals.machines, rel=1e-6)
