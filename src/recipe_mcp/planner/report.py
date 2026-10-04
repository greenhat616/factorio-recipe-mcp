"""Turn a solution vector into per-line rows, item flows, totals, warnings and shadow prices."""

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence

from .model import EPS, Planner
from .schema import ItemFlow, Line, PlanLine, Report, Solution, Totals


def report(
    planner: Planner,
    lines: Sequence[Line],
    solution: Solution,
    targets: Mapping[str, float],
    factor: float,
    prices_limit: int = 40,
) -> Report:
    flows: defaultdict[str, ItemFlow] = defaultdict(lambda: ItemFlow(item=''))
    rows: list[PlanLine] = []
    total = Totals()
    for r, x in zip(lines, solution.x, strict=True):
        if abs(x) < EPS:
            continue
        machines = x / r.crafts_per_machine
        count = math.ceil(machines - 1e-6) if machines > 0 else 0
        fuel = machines * r.active_W if not r.electric and r.energy_type != 'void' else 0
        for k, v in r.balance.items():
            if v * x > 0:
                flows[k].produced += v * x
            else:
                flows[k].consumed += abs(v * x)
        row = PlanLine(
            id=r.id,
            recipe=r.recipe,
            machine=r.machine,
            modules=r.modules,
            beacons=r.beacons,
            crafts=x * factor,
            machines=machines,
            machines_ceil=count,
            speed_multiplier=r.speed_multiplier,
            productivity=r.productivity,
            consumption_multiplier=r.consumption_multiplier,
            power_MW=machines * r.power_W / 1e6,
            installed_power_MW=count * r.power_W / 1e6,
            energy_type=r.energy_type,
            fuel_MW=fuel / 1e6,
            pollution_per_minute=machines * r.pollution_per_minute,
            inputs={k: -v * x * factor for k, v in r.balance.items() if v < 0},
            outputs={k: v * x * factor for k, v in r.balance.items() if v > 0},
        )
        rows.append(row)
        total.machines += row.machines
        total.machines_ceil += row.machines_ceil
        total.power_MW += row.power_MW
        total.installed_power_MW += row.installed_power_MW
        total.non_electric_fuel_MW += row.fuel_MW
        total.pollution_per_minute += row.pollution_per_minute
    for k, v in solution.imports.items():
        flows[k].imported = v
    for k, v in solution.surplus.items():
        flows[k].surplus = v
    items: list[ItemFlow] = []
    for k in sorted(flows):
        f = flows[k]
        if max(abs(f.produced), abs(f.consumed), abs(f.imported), abs(f.surplus)) < EPS:
            continue
        items.append(
            ItemFlow(
                item=k,
                produced=f.produced * factor,
                consumed=f.consumed * factor,
                imported=f.imported * factor,
                surplus=f.surplus * factor,
                target=targets.get(k, 0) * factor,
            )
        )
    warnings = list(planner.warnings)
    active = [(r, x) for r, x in zip(lines, solution.x, strict=True) if x > EPS]
    for r, _ in active:
        for k, need in r.ingredient_temperatures.items():
            supply = [t for p, _ in active if p.balance.get(k, 0) > 0 for t in p.temperatures.get(k, [])]
            lo = next((t for t in (need.minimum_temperature, need.temperature) if t is not None), -math.inf)
            hi = next((t for t in (need.maximum_temperature, need.temperature) if t is not None), math.inf)
            if supply and not any(lo <= t <= hi for t in supply):
                wanted = need.model_dump(exclude_none=True)
                warnings.append(f'{r.id}: needs {k} at {wanted}, producers in plan supply {sorted(set(supply))}')
    imports = {k: v * factor for k, v in solution.imports.items() if abs(v) > EPS}
    out = Report(
        lines=rows,
        items=items,
        imports=imports,
        surplus={k: v * factor for k, v in solution.surplus.items() if abs(v) > EPS},
        totals=total,
        max_balance_error=solution.residual,
        warnings=warnings,
        objective=solution.objective,
        matrix=solution.matrix,
    )
    if solution.prices is not None:
        prices = solution.prices
        keep = list(dict.fromkeys(list(targets) + list(imports)))
        others = sorted((k for k, v in prices.items() if k not in keep and abs(v) > EPS), key=lambda k: -abs(prices[k]))
        keep += others[: max(0, prices_limit - len(keep))]
        out.shadow_prices = {k: prices[k] / factor for k in keep if k in prices}
    return out
