"""Graph endpoints must account for exactly the solved material flows."""

from pathlib import Path

import pytest
from test_plans import RAW, block

from recipe_mcp.database import Database
from recipe_mcp.planner import plan
from recipe_mcp.planner.schema import GraphOptions, LineSpec, ProductionGraph
from recipe_mcp.plans import PlanStore
from recipe_mcp.plans.factory import solve_plan


def conserved(graph: ProductionGraph) -> None:
    ids = {n.id for n in graph.nodes}
    assert len(ids) == len(graph.nodes)
    assert all(e.source in ids and e.target in ids for e in graph.edges)
    for node in graph.nodes:
        if node.kind == 'item':
            ins = sum(e.rate for e in graph.edges if e.kind == 'flow' and e.target == node.id)
            outs = sum(e.rate for e in graph.edges if e.kind == 'flow' and e.source == node.id)
            assert ins == pytest.approx(outs, abs=1e-6), node.id
    assert not any('residual' in note for note in graph.notes)


@pytest.mark.parametrize('integer', [False, True])
def test_graph_stable_and_conserved(integer: bool) -> None:
    db = Database(raw=RAW)
    r = plan(db, {'e': 40}, lines=['ox'], graph='bipartite', integer_machines=integer, validate_stage=False)
    assert r.graph is not None
    conserved(r.graph)
    assert any(n.kind == 'disposal' for n in r.graph.nodes)
    depths = {n.id: n.depth for n in r.graph.nodes}
    assert depths['target:item:e'] == 0 and depths['item:item:e'] == 1 and depths['line:ox'] == 2
    again = plan(db, {'e': 40}, lines=['ox'], graph='bipartite', integer_machines=integer, validate_stage=False)
    assert r.graph == again.graph
    assert 'graph' not in plan(db, {'e': 40}, lines=['ox'], validate_stage=False).model_dump()


def test_graph_matrix_relaxed_and_allocated() -> None:
    db = Database(raw=RAW)
    r = plan(
        db,
        {'b': 4},
        lines=[LineSpec(recipe='ab', id='a', fixed_machines=3), LineSpec(recipe='ab', id='b', fixed_machines=1)],
        graph='bipartite',
        graph_options=GraphOptions(allocate=True),
        validate_stage=False,
    )
    assert r.graph is not None
    conserved(r.graph)
    edges = [e for e in r.graph.edges if e.allocated and e.target == 'target:item:b']
    assert sorted(e.rate for e in edges) == [1, 3]
    for solver in ('matrix', 'lp'):
        r = plan(db, {'b': 4}, lines=['ab'], solver=solver, graph='bipartite', validate_stage=False)
        assert r.graph is not None
        conserved(r.graph)
    r = plan(db, {'b': 4}, lines=['ab'], limits={'machines': 1}, graph='bipartite', validate_stage=False)
    assert r.status == 'infeasible' and r.graph is not None
    conserved(r.graph)
    with pytest.raises(ValueError, match='not implemented'):
        plan(db, {'b': 1}, graph_options={'format': 'mermaid'}, validate_stage=False)


@pytest.mark.parametrize('kind', ['blocks', 'full'])
def test_factory_graph(tmp_path: Path, kind: str) -> None:
    store = PlanStore(tmp_path, Database(raw=RAW))
    store.save(
        'p',
        [
            block('source', targets={'e': 40}, lines=['ox']),
            block('sink', lines=['use-o'], consume={'o': {'from': ['source'], 'factor': 0.5}}),
        ],
    )
    r = solve_plan(store, 'p', graph=kind)  # type: ignore[arg-type]
    assert r.graph is not None
    assert r.plan == 'p'
    conserved(r.graph)
    links = [e for e in r.graph.edges if e.kind == 'link']
    assert links[0].data['of'] == 'surplus' and links[0].data['resolved'] == 20
    if kind == 'full':
        node = next(n for n in r.graph.nodes if n.id == 'block:source')
        assert all(id.startswith('block:source/') for id in node.data['children'])


@pytest.mark.realdata
def test_real_energy_graph(real_db: Database, force: str) -> None:
    r = plan(real_db, {'nullius-methanol': 10}, energy_mode='balance', graph='bipartite', force=force)
    assert r.graph is not None
    conserved(r.graph)
    assert len(r.graph.model_dump_json().encode()) < 200_000
    assert all(e.unit == 'MW' for e in r.graph.edges if e.item.startswith('energy:'))


def test_hundred_active_lines_graph_budget() -> None:
    raw = {
        'recipe': {
            f'r-{i}': {
                'ingredients': [{'name': 'ore', 'amount': 1}],
                'results': [{'name': f'p-{i}', 'amount': 1}],
                'energy_required': 1,
            }
            for i in range(100)
        },
        'assembling-machine': RAW['assembling-machine'],
        'technology': {},
    }
    r = plan(Database(raw=raw), {f'p-{i}': 1 for i in range(100)}, graph='bipartite', validate_stage=False)
    assert r.graph is not None and len(r.lines) == 100
    conserved(r.graph)
    assert len(r.graph.model_dump_json().encode()) < 200_000
