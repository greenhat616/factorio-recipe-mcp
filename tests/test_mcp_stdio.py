"""End-to-end MCP stdio regression against the real export."""

import asyncio
import json
import sys
import uuid
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from recipe_mcp.database import JSON, Database
from recipe_mcp.paths import PLANS_DIR


async def run(db: Database, force: str, plan_name: str) -> None:
    params = StdioServerParameters(command=sys.executable, args=['-m', 'recipe_mcp'])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            assert len(listing.tools) == 20

            async def call(name: str, args: JSON, error: bool = False) -> Any:
                result = await session.call_tool(name, args)
                assert bool(result.isError) == error, result
                return [json.loads(c.text) for c in result.content if c.type == 'text'] if not error else result

            context = (await call('get_progress_context', {}))[0]
            assert context['compatible']
            recipe = (await call('get_recipe', {'name': 'nullius-pressure-methanol', 'force': force}))[0]
            assert recipe['availability']['usable_at_stage'] is True
            unknown = (await call('get_recipe', {'name': 'nullius-methanol'}))[0]
            assert unknown['availability']['state'] == 'unknown'  # multiplayer requires a force
            related = (
                await call(
                    'related_recipes',
                    {'material': 'nullius-methanol', 'direction': 'producers', 'available_only': True, 'force': force},
                )
            )[0]
            names = {r['name'] for r in related['producers']}
            assert 'nullius-pressure-methanol' in names and 'nullius-fermentation' not in names
            assert not any(db.virtual(n) for n in names)
            await call('search_recipes', {'query': 'methanol', 'available_only': True, 'force': force})
            await call(
                'production_chain', {'material': 'nullius-methanol', 'depth': 2, 'max_recipes': 20, 'force': force}
            )
            machines = await call(
                'compatible_machines', {'recipe': 'nullius-methanol', 'buildable_only': True, 'force': force}
            )
            assert machines
            await call('technology_requirements', {'technology': 'nullius-high-pressure-chemistry', 'force': force})
            tech = (await call('get_technology', {'name': 'nullius-high-pressure-chemistry', 'force': force}))[0]
            assert tech['state'] == 'researched'
            page = (
                await call(
                    'list_technologies', {'force': force, 'state': 'researched', 'include_hidden': True, 'limit': 3}
                )
            )[0]
            expected = sum(t['researched'] for t in db.progress['forces'][force]['technologies'].values())
            assert page['total'] == expected and len(page['technologies']) == 3
            validation = (await call('validate_plan', {'recipe_rates': {'nullius-fermentation': 1}, 'force': force}))[0]
            assert not validation['valid_at_stage']
            await call('net_balance', {'recipe_rates': {'nullius-fermentation': 1}, 'force': force}, error=True)
            await call('production_chain', {'material': 'nullius-methanol'}, error=True)
            balance = (
                await call(
                    'net_balance', {'recipe_rates': {'nullius-methane': 2, 'nullius-methanol': 3}, 'force': force}
                )
            )[0]
            assert balance == {
                'fluid:nullius-carbon-dioxide': -64,
                'fluid:nullius-hydrogen': -220,
                'fluid:nullius-oxygen': -24,
                'fluid:nullius-water': 16,
                'fluid:nullius-methanol': 6,
            }
            await call('net_balance', {'recipe_rates': {'nullius-fermentation': 1}, 'validate_stage': False})
            await call('net_balance', {'recipe_rates': {'nullius-methanol': -1}, 'validate_stage': False}, error=True)
            equipment = (
                await call(
                    'validate_plan',
                    {'recipe_rates': {'nullius-methanol': 1}, 'machines': ['nullius-chemical-plant-3'], 'force': force},
                )
            )[0]
            assert not equipment['valid_at_stage']
            lp = (
                await call('solve_production', {'targets': {'nullius-methanol': 600}, 'per': 'minute', 'force': force})
            )[0]
            assert lp['max_balance_error'] < 1e-6 and lp['solver'] == 'lp' and lp['lines']
            mx = (
                await call(
                    'solve_production',
                    {
                        'targets': {'nullius-methanol': 600},
                        'per': 'minute',
                        'force': force,
                        'solver': 'matrix',
                        'lines': lp['lines_for_matrix'],
                        **lp['matrix_args'],
                    },
                )
            )[0]
            assert abs(mx['totals']['machines'] - lp['totals']['machines']) < 1e-6
            await call(
                'solve_production', {'targets': {'nullius-methanol': 1}, 'solver': 'matrix', 'force': force}, error=True
            )
            mx_scale = (
                await call(
                    'solve_production',
                    {
                        'targets': {'nullius-methanol': 1},
                        'lines': lp['lines_for_matrix'],
                        'mode': 'maximize',
                        'limits': {'imports': {'nullius-box-limestone': 120}},
                        'per': 'minute',
                        'force': force,
                    },
                )
            )[0]
            assert mx_scale['scale'] > 0 and mx_scale['bottlenecks']
            stats = (await call('machine_stats', {'recipe': 'nullius-methanol', 'rate': 10, 'force': force}))[0]
            assert stats['valid_at_stage'] and stats['for_rate']['machines'] > 0
            beacon = {'beacon': 'nullius-beacon-2', 'interference': 3, 'modules': ['nullius-speed-module-1']}
            boosted = (
                await call(
                    'machine_stats', {'recipe': 'nullius-methanol', 'beacons': [beacon], 'validate_stage': False}
                )
            )[0]
            assert boosted['beacons'][0]['entity'] == 'nullius-beacon-2-3'
            assert boosted['beacon_items'] == {'nullius-beacon-2': 1}
            pm = (
                await call(
                    'production_matrix',
                    {'lines': [line['recipe'] for line in lp['lines_for_matrix']], 'targets': {'nullius-methanol': 10}},
                )
            )[0]
            assert pm['square_system']['items'] == len(pm['items'])
            pm_consume = (
                await call(
                    'production_matrix',
                    {
                        'lines': [line['recipe'] for line in lp['lines_for_matrix']],
                        'consume': {'nullius-box-limestone': 1},
                    },
                )
            )[0]
            assert pm_consume['square_system']['roles']['item:nullius-box-limestone'] == 'consumed_input'
            await plan_tools(call, force, plan_name)


async def plan_tools(call: Any, force: str, name: str) -> None:
    block = {'id': 'methanol', 'request': {'targets': {'nullius-methanol': 10}}}
    saved = (await call('plan_save', {'name': name, 'blocks': [block], 'force': force}))[0]
    assert saved['plan']['revision'] == 1 and saved['stale'] == 'fresh'
    assert name in {p['name'] for p in await call('plan_list', {})}
    await call('plan_save', {'name': '../escape', 'blocks': [block]}, error=True)
    edit = {'op': 'set_target', 'block_id': 'methanol', 'item': 'nullius-methanol', 'value': 5}
    edited = (await call('plan_edit', {'name': name, 'ops': [edit], 'expected_revision': 1}))[0]
    assert edited['revision'] == 2
    await call('plan_edit', {'name': name, 'ops': [edit], 'expected_revision': 1}, error=True)
    solved = (await call('plan_solve', {'name': name}))[0]
    assert solved['blocks'][0]['status'] == 'optimal' and solved['factory']['net_inputs']
    pinned = (await call('plan_edit', {'name': name, 'ops': [{'op': 'pin', 'block_id': 'methanol'}]}))[0]
    assert pinned['revision'] == 4
    view = (await call('plan_get', {'name': name, 'include_results': False}))[0]
    assert view['plan']['blocks'][0]['result'] is None and view['plan']['blocks'][0]['request']['lines']
    await call('plan_delete', {'name': name, 'confirm': 'wrong'}, error=True)
    deleted = (await call('plan_delete', {'name': name, 'confirm': name}))[0]
    assert deleted['name'] == name


@pytest.mark.realdata
def test_stdio_tools(real_db: Database, force: str) -> None:
    """All 20 tools over real stdio: pagination, force selection, locked-recipe/machine rejection, theory mode,
    and the plan tools against data/plans with a throwaway plan that is removed, trash included."""
    name = f'mcp-test-{uuid.uuid4().hex[:8]}'
    try:
        asyncio.run(run(real_db, force, name))
    finally:
        (PLANS_DIR / f'{name}.json').unlink(missing_ok=True)
        for leftover in (PLANS_DIR / '.trash').glob(f'{name}-*.json'):
            leftover.unlink()
