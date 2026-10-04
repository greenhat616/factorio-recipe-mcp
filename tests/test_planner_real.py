"""Planner against the real export and save snapshot (skipped when data/ is absent)."""

import pytest

from recipe_mcp.database import Database
from recipe_mcp.planner import LineSpec, machine_stats, plan
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
