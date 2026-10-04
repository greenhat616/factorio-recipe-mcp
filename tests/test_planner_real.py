"""Planner against the real export and save snapshot (skipped when data/ is absent)."""
import pytest

from recipe_mcp.planner import machine_stats, plan

pytestmark = pytest.mark.realdata


@pytest.fixture(scope='module')
def methanol(real_db, force):
    return plan(real_db, {'nullius-methanol': 10}, force=force)


def test_auto_discovered_plan_balances(methanol):
    assert methanol['max_balance_error'] < 1e-6 and methanol['lines']
    for i in methanol['items']:
        net = i['produced'] + i.get('import', 0) - i['consumed'] - i.get('surplus', 0) - i['target']
        assert abs(net) < 1e-6, i


def test_pinned_lines_replay_in_lp_and_matrix(real_db, force, methanol):
    pinned = plan(real_db, {'nullius-methanol': 10}, methanol['lines_for_matrix'], force=force)
    assert pinned['totals']['machines'] == pytest.approx(methanol['totals']['machines'], rel=1e-6)
    mx = plan(real_db, {'nullius-methanol': 10}, methanol['lines_for_matrix'], solver='matrix', force=force,
              **methanol['matrix_args'])
    assert mx['totals']['machines'] == pytest.approx(methanol['totals']['machines'], rel=1e-6)


def test_stage_locked_machine_rejected(real_db, force):
    with pytest.raises(ValueError, match='Stage-locked'):
        plan(real_db, {'nullius-methanol': 1}, [{'recipe': 'nullius-methanol', 'machine': 'nullius-chemical-plant-3'}], force=force)


def test_machine_stats_for_rate(real_db, force):
    st = machine_stats(real_db, 'nullius-methanol', rate=10, force=force)
    assert st['valid_at_stage'] and st['for_rate']['machines'] > 0
