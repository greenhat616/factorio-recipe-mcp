"""Turn a solution vector into per-line rows, item flows, totals, warnings and shadow prices."""

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence

from ..database import JSON
from .model import EPS, Line, Planner


def report(
    planner: Planner,
    lines: Sequence[Line],
    solution: JSON,
    targets: Mapping[str, float],
    factor: float,
    prices_limit: int = 40,
) -> JSON:
    flows: defaultdict[str, dict[str, float]] = defaultdict(lambda: {'produced': 0.0, 'consumed': 0.0})
    rows: list[JSON] = []
    total: defaultdict[str, float] = defaultdict(float)
    for r, x in zip(lines, solution['x'], strict=True):
        if abs(x) < EPS:
            continue
        machines = x / r['crafts_per_machine']
        count = math.ceil(machines - 1e-6) if machines > 0 else 0
        elec = r['energy_type'] == 'electric'
        power = machines * ((r['active_W'] + r['drain_W']) if elec else 0) + machines * r['beacon_W']
        installed = count * ((r['active_W'] + r['drain_W']) if elec else 0) + count * r['beacon_W']
        fuel = machines * r['active_W'] if not elec and r['energy_type'] != 'void' else 0
        for k, v in r['balance'].items():
            flows[k]['produced' if v * x > 0 else 'consumed'] += abs(v * x)
        machines, x = float(machines), float(x)
        rows.append(
            {
                'id': r['id'],
                'recipe': r['recipe'],
                'machine': r['machine'],
                'modules': r['modules'],
                'beacons': r['beacons'],
                'crafts': x * factor,
                'machines': machines,
                'machines_ceil': count,
                'speed_multiplier': r['speed_multiplier'],
                'productivity': r['productivity'],
                'consumption_multiplier': r['consumption_multiplier'],
                'power_MW': power / 1e6,
                'installed_power_MW': installed / 1e6,
                (r['energy_type'] + '_fuel_MW'): fuel / 1e6,
                'pollution_per_minute': machines * r['pollution_per_minute'],
                'inputs': {k: -v * x * factor for k, v in r['balance'].items() if v < 0},
                'outputs': {k: v * x * factor for k, v in r['balance'].items() if v > 0},
            }
        )
        total['machines'] += machines
        total['machines_ceil'] += count
        total['power_MW'] += power / 1e6
        total['installed_power_MW'] += installed / 1e6
        total['non_electric_fuel_MW'] += fuel / 1e6
        total['pollution_per_minute'] += machines * r['pollution_per_minute']
    for k, v in solution['imports'].items():
        flows[k]['import'] = v
    for k, v in solution['surplus'].items():
        flows[k]['surplus'] = v
    items: list[JSON] = []
    for k in sorted(flows):
        f = flows[k]
        if max(abs(v) for v in f.values()) < EPS:
            continue
        items.append({'item': k, **{n: v * factor for n, v in f.items()}, 'target': targets.get(k, 0) * factor})
    warnings = list(planner.warnings)
    active = [(r, x) for r, x in zip(lines, solution['x'], strict=True) if x > EPS]
    for r, _ in active:
        for k, need in r['ingredient_temperatures'].items():
            supply = [t for p, y in active if p['balance'].get(k, 0) > 0 for t in p['temperatures'].get(k, [])]
            lo, hi = (
                need.get('minimum_temperature', need.get('temperature', -math.inf)),
                need.get('maximum_temperature', need.get('temperature', math.inf)),
            )
            if supply and not any(lo <= t <= hi for t in supply):
                warnings.append(f'{r["id"]}: needs {k} at {need}, producers in plan supply {sorted(set(supply))}')
    out = {
        'lines': rows,
        'items': items,
        'imports': {k: v * factor for k, v in solution['imports'].items() if abs(v) > EPS},
        'surplus': {k: v * factor for k, v in solution['surplus'].items() if abs(v) > EPS},
        'totals': dict(total),
        'max_balance_error': solution['residual'],
        'warnings': warnings,
    }
    if 'prices' in solution:
        # Marginal objective cost of one more unit/second of each item (targets and imports first).
        keep = list(dict.fromkeys(list(targets) + list(out['imports'])))
        others = sorted(
            (k for k, v in solution['prices'].items() if k not in keep and abs(v) > EPS),
            key=lambda k: -abs(solution['prices'][k]),
        )
        keep += others[: max(0, prices_limit - len(keep))]
        out['shadow_prices'] = {k: float(solution['prices'][k]) / factor for k in keep if k in solution['prices']}
        out['objective'] = solution['objective']
    if 'matrix' in solution:
        out['matrix'] = solution['matrix']
    return out
