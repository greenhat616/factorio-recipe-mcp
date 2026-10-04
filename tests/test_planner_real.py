"""Planner against the real export and save snapshot (skipped when data/ is absent)."""

import pytest

from recipe_mcp.database import Database
from recipe_mcp.planner import LineSpec, Planner, machine_stats, plan
from recipe_mcp.planner.disposal import void_recipes
from recipe_mcp.planner.schema import PlanResult

pytestmark = pytest.mark.realdata


@pytest.fixture(scope='module')
def methanol(real_db: Database, force: str) -> PlanResult:
    return plan(real_db, {'nullius-methanol': 10}, force=force)


def test_auto_discovered_plan_balances(methanol: PlanResult) -> None:
    assert methanol.max_balance_error < 1e-6 and methanol.lines
    for i in methanol.items:
        net = i.produced + i.imported - i.consumed - i.surplus - i.target
        assert abs(net) < 1e-6, i


def test_pinned_lines_replay_in_lp_and_matrix(real_db: Database, force: str, methanol: PlanResult) -> None:
    pinned = plan(real_db, {'nullius-methanol': 10}, methanol.lines_for_matrix, force=force)
    assert pinned.totals.machines == pytest.approx(methanol.totals.machines, rel=1e-6)
    assert methanol.matrix_args is not None
    mx = plan(
        real_db,
        {'nullius-methanol': 10},
        methanol.lines_for_matrix,
        solver='matrix',
        force=force,
        **methanol.matrix_args.model_dump(),
    )
    assert mx.totals.machines == pytest.approx(methanol.totals.machines, rel=1e-6)


def test_stage_locked_machine_rejected(real_db: Database, force: str) -> None:
    with pytest.raises(ValueError, match='Stage-locked'):
        plan(
            real_db,
            {'nullius-methanol': 1},
            [LineSpec(recipe='nullius-methanol', machine='nullius-chemical-plant-3')],
            force=force,
        )


def test_machine_stats_for_rate(real_db: Database, force: str) -> None:
    st = machine_stats(real_db, 'nullius-methanol', rate=10, force=force)
    assert st.valid_at_stage and st.for_rate is not None and st.for_rate.machines > 0


def test_maximize_methanol_under_limestone_limit(real_db: Database, force: str, methanol: PlanResult) -> None:
    # On the auto-discovered candidate set other raw routes stay uncapped, so pin the chosen route.
    r = plan(
        real_db,
        {'nullius-methanol': 1},
        methanol.lines_for_matrix,
        mode='maximize',
        limits={'imports': {'nullius-box-limestone': 2}},
        force=force,
    )
    assert r.scale is not None and r.scale > 0
    assert 'import:item:nullius-box-limestone' in {b.constraint for b in r.bottlenecks}
    assert r.imports['item:nullius-box-limestone'] <= 2 + 1e-6


def test_consume_limestone_scales_pinned_route(real_db: Database, force: str, methanol: PlanResult) -> None:
    r = plan(
        real_db,
        {'nullius-methanol': 1},
        methanol.lines_for_matrix,
        mode='maximize',
        consume={'nullius-box-limestone': 2},
        force=force,
    )
    limestone = methanol.imports['item:nullius-box-limestone']
    assert r.scale == pytest.approx(10 * 2 / limestone, rel=1e-6)
    flow = next(i for i in r.items if i.item == 'item:nullius-box-limestone')
    assert flow.supplied + flow.produced - flow.consumed == pytest.approx(0, abs=1e-6)
    assert flow.imported == 0 and flow.surplus == 0


def test_void_recipes_found(real_db: Database, force: str) -> None:
    assert sum(len(v) for v in void_recipes(Planner(real_db, force)).values()) == 42


def test_surplus_oxygen_goes_to_an_unpowered_chimney(methanol: PlanResult) -> None:
    row = next(d for d in methanol.disposal if d.id == 'dispose:fluid:nullius-compressed-oxygen')
    assert row.machine == 'nullius-chimney-2' and row.power_MW == 0
