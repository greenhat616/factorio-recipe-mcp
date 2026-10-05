"""Build local Nullius inspection reports and measure the spec's performance budgets."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from recipe_mcp.database import Database
from recipe_mcp.paths import DATA_DIR
from recipe_mcp.planner import plan
from recipe_mcp.planner.schema import LineSpec
from recipe_mcp.plans import PlanStore
from recipe_mcp.plans.factory import solve_plan
from recipe_mcp.plans.models import BlockInput
from recipe_mcp.viewer import render


def main() -> None:
    db = Database()
    force = 'faction-a632079'
    output = DATA_DIR / 'reports/helmod-gaps'
    output.mkdir(parents=True, exist_ok=True)
    blocks = [
        BlockInput.model_validate(
            {
                'id': 'methanol',
                'description': '10 甲醇/秒，能源由公用工程块供应',
                'request': {
                    'targets': {'nullius-methanol': 10},
                    'energy_mode': 'balance',
                    'imports': ['energy:electric', 'energy:heat'],
                },
            }
        ),
        BlockInput.model_validate(
            {
                'id': 'utilities',
                'description': '供电与供热，并覆盖自身能耗',
                'request': {
                    'targets': {'energy:electric': {'from': ['methanol']}, 'energy:heat': {'from': ['methanol']}},
                    'energy_mode': 'balance',
                },
            }
        ),
    ]
    with TemporaryDirectory() as temp:
        store = PlanStore(Path(temp), db)
        store.save('methanol-self-powered', blocks, force=force)
        started = perf_counter()
        factory = solve_plan(store, 'methanol-self-powered', detail='full', graph='full', save_results=False)
        seconds = perf_counter() - started
        assert factory.factory.complete and factory.graph
        result = factory.model_dump(mode='json')
        (output / 'factory.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        (output / 'factory.html').write_text(render(result, '甲醇工厂 · 规划观察器'), encoding='utf-8')
        ten = [
            BlockInput.model_validate(
                {'id': f'block-{i}', 'request': {'targets': {'nullius-methanol': 10}, 'energy_mode': 'balance'}}
            )
            for i in range(10)
        ]
        store.save('ten-blocks', ten, force=force)
        started = perf_counter()
        ten_result = solve_plan(store, 'ten-blocks', save_results=False)
        ten_seconds = perf_counter() - started
        assert ten_result.factory.complete
    single = plan(db, {'energy:electric': 10}, energy_mode='balance', graph='bipartite', force=force)
    payload = single.model_dump(mode='json')
    (output / 'power.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'power.html').write_text(render(payload, '10 MW 发电站 · 规划观察器'), encoding='utf-8')
    tiny = Database(raw={'solar-panel': {'panel': {'production': '1MW'}}, 'recipe': {}, 'technology': {}})
    started = perf_counter()
    thousand = plan(
        tiny,
        {'energy:electric': 100},
        lines=[LineSpec(recipe='solar:panel', id=f'panel-{i}') for i in range(1000)],
        energy_mode='balance',
        validate_stage=False,
    )
    lp_seconds = perf_counter() - started
    metrics = {
        'factory_seconds': seconds,
        'ten_blocks_seconds': ten_seconds,
        '1000_candidates_seconds': lp_seconds,
        '1000_candidate_count': thousand.candidate_lines,
        'factory_graph_bytes': len(factory.graph.model_dump_json().encode()),
        'power_graph_bytes': len(single.graph.model_dump_json().encode()) if single.graph else 0,
        'max_energy_balance_error': max(b.result.max_balance_error for b in factory.blocks if b.result),
    }
    (output / 'metrics.json').write_text(json.dumps(metrics, indent=2), encoding='utf-8')
    print(json.dumps(metrics, indent=2))
    print(output / 'factory.html')


if __name__ == '__main__':
    main()
