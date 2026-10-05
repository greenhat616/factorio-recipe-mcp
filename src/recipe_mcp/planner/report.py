"""Turn a solution vector into per-line rows, item flows, totals, warnings and shadow prices."""

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from .energy import rate_factor
from .model import EPS, Planner
from .schema import ItemFlow, Line, ModuleCount, PlanLine, Report, Solution, Totals


def plan_line(r: Line, x: float, factor: float, count: int | None = None) -> PlanLine:
    """One line running x crafts per second (internal units), reported per `per`; count overrides the
    rounded-up machine count when whole machines were solved for."""
    machines = x / r.crafts_per_machine
    if count is None:
        count = math.ceil(machines - 1e-6) if machines > 0 else 0
    if r.machine_type == 'temperature-match':
        machines, count = 0.0, 0
    fuel = machines * r.active_W if not r.electric and r.energy_type != 'void' else 0
    return PlanLine(
        id=r.id,
        recipe=r.recipe,
        machine=r.machine,
        machine_type=r.machine_type,
        fuel=r.fuel,
        fuel_per_machine=r.fuel_per_machine * factor,
        modules=r.modules,
        beacons=r.beacons,
        crafts=x * factor,
        machines=machines,
        machines_ceil=count,
        speed_multiplier=r.speed_multiplier,
        productivity=r.productivity,
        productivity_model=r.productivity_model,
        consumption_multiplier=r.consumption_multiplier,
        power_MW=machines * r.power_W / 1e6,
        installed_power_MW=count * r.power_W / 1e6,
        energy_type=r.energy_type,
        fuel_MW=fuel / 1e6,
        pollution_per_minute=machines * r.pollution_per_minute,
        inputs={k: -v * x * rate_factor(k, factor) for k, v in r.balance.items() if v < 0},
        outputs={k: v * x * rate_factor(k, factor) for k, v in r.balance.items() if v > 0},
        beacon_count=machines * r.beacon_entities,
        beacon_count_ceil=math.ceil(count * r.beacon_entities - 1e-6) if count else 0,
        beacon_power_MW=machines * r.beacon_W / 1e6,
    )


def add_line(total: Totals, row: PlanLine) -> None:
    total.electric_generation_MW += row.outputs.get('energy:electric', 0)
    total.heat_generation_MW += row.outputs.get('energy:heat', 0)
    total.heat_consumption_MW += row.fuel_MW if row.energy_type == 'heat' else 0
    if 'energy:electric' in row.inputs or 'energy:electric' in row.outputs:
        total.net_electric_MW += row.outputs.get('energy:electric', 0) - row.power_MW
    total.machines += row.machines
    total.machines_ceil += row.machines_ceil
    total.power_MW += row.power_MW
    total.installed_power_MW += row.installed_power_MW
    total.non_electric_fuel_MW += row.fuel_MW
    total.pollution_per_minute += row.pollution_per_minute
    total.beacon_count += row.beacon_count
    total.beacon_count_ceil += row.beacon_count_ceil
    total.beacon_power_MW += row.beacon_power_MW
    by_type = dict(total.beacon_count_by_type)
    for b in row.beacons:
        by_type[b.item] = by_type.get(b.item, 0.0) + row.machines * b.per_machine
    total.beacon_count_by_type = by_type


def add_totals(total: Totals, other: Totals) -> Totals:
    out = total.model_copy(deep=True)
    for k, v in other.model_dump().items():
        if isinstance(v, dict):
            merged = dict(getattr(out, k))
            for name, n in v.items():
                merged[name] = merged.get(name, 0.0) + n
            setattr(out, k, merged)
        else:
            setattr(out, k, getattr(out, k) + v)
    return out


def module_inventory(rows: Iterable[PlanLine]) -> dict[str, ModuleCount]:
    """Modules in machine slots plus beacon slots; shared beacons hold their modules once."""
    count: defaultdict[str, float] = defaultdict(float)
    ceil: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        for mod in row.modules:
            count[mod] += row.machines
            ceil[mod] += row.machines_ceil
        for b in row.beacons:
            beacons_ceil = math.ceil(row.machines_ceil * b.per_machine - 1e-6) if row.machines_ceil else 0
            for mod in b.modules:
                count[mod] += row.machines * b.per_machine
                ceil[mod] += beacons_ceil
    return {k: ModuleCount(count=count[k], count_ceil=ceil[k]) for k in sorted(count)}


def report(
    planner: Planner,
    lines: Sequence[Line],
    solution: Solution,
    targets: Mapping[str, float],
    factor: float,
    prices_limit: int = 40,
    consume: Mapping[str, float] | None = None,
) -> Report:
    flows: defaultdict[str, ItemFlow] = defaultdict(lambda: ItemFlow(item=''))
    rows: list[PlanLine] = []
    total = Totals()
    counts: Sequence[int | None] = solution.counts or [None] * len(lines)
    for r, x, n in zip(lines, solution.x, counts, strict=True):
        if abs(x) < EPS:
            continue
        for k, v in r.balance.items():
            if v * x > 0:
                flows[k].produced += v * x
            else:
                flows[k].consumed += abs(v * x)
        row = plan_line(r, x, factor, n)
        rows.append(row)
        add_line(total, row)
    for k, v in solution.imports.items():
        flows[k].imported = v
    for k, v in solution.surplus.items():
        flows[k].surplus = v
    for k, v in (consume or {}).items():
        flows[k].supplied = v
    items: list[ItemFlow] = []
    for k in sorted(flows):
        f = flows[k]
        if max(abs(f.produced), abs(f.consumed), abs(f.imported), abs(f.surplus), abs(f.supplied)) < EPS:
            continue
        items.append(
            ItemFlow(
                item=k,
                unit='MW'
                if k.startswith('energy:')
                else 'per ' + {1: 'second', 60: 'minute', 3600: 'hour'}[int(factor)],
                produced=f.produced * rate_factor(k, factor),
                consumed=f.consumed * rate_factor(k, factor),
                imported=f.imported * rate_factor(k, factor),
                supplied=f.supplied * rate_factor(k, factor),
                surplus=f.surplus * rate_factor(k, factor),
                target=targets.get(k, 0) * rate_factor(k, factor),
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
    imports = {k: v * rate_factor(k, factor) for k, v in solution.imports.items() if abs(v) > EPS}
    out = Report(
        lines=rows,
        items=items,
        imports=imports,
        surplus={k: v * rate_factor(k, factor) for k, v in solution.surplus.items() if abs(v) > EPS},
        totals=total,
        max_balance_error=solution.residual,
        warnings=warnings,
        objective=solution.objective,
        matrix=solution.matrix,
    )
    for flow in items:
        if flow.item.startswith('fluid:') and ('@' in flow.item or '[' in flow.item):
            base = flow.item.split('@', 1)[0].split('[', 1)[0]
            out.temperatures.setdefault(base, []).append(flow)
    if solution.prices is not None:
        prices = solution.prices
        keep = list(dict.fromkeys(list(targets) + list(imports)))
        others = sorted((k for k, v in prices.items() if k not in keep and abs(v) > EPS), key=lambda k: -abs(prices[k]))
        keep += others[: max(0, prices_limit - len(keep))]
        out.shadow_prices = {k: prices[k] / rate_factor(k, factor) for k in keep if k in prices}
    return out
