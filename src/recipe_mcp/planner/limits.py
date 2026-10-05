"""Turn user limits into LP rows, and LP constraint states into usage and bottleneck reports."""

import math
from collections.abc import Sequence

from .energy import PREFIXES, rate_factor
from .model import CRAFTING_KINDS, Planner
from .schema import Bottleneck, ConstraintState, Infeasibility, LimitRow, Limits, LimitUsage, Line, Mode, Per

TIGHT = 1e-6
MACHINE_KINDS = (*CRAFTING_KINDS, 'mining-drill', *(kind for kinds in PREFIXES.values() for kind in kinds))


def check_cap(name: str, value: float) -> float:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f'Limit {name} must be finite and nonnegative, got {value}')
    return value


def limit_rows(p: Planner, lines: Sequence[Line], limits: Limits, factor: float) -> list[LimitRow]:
    rows: list[LimitRow] = []
    for name, cap in limits.imports.items():
        key = p.key(name)
        rows.append(
            LimitRow(
                name=f'import:{key}',
                limit=check_cap(name, cap) / rate_factor(key, factor),
                item=key,
                rate=not key.startswith('energy:'),
                unit='MW' if key.startswith('energy:') else 'unit',
            )
        )
    if limits.power_MW is not None:
        coef = [r.power_W / 1e6 / r.crafts_per_machine for r in lines]
        rows.append(LimitRow(name='power_MW', limit=limits.power_MW, coef=coef, unit='MW'))
    if limits.machines is not None:
        coef = [0 if r.machine_type == 'temperature-match' else 1 / r.crafts_per_machine for r in lines]
        rows.append(LimitRow(name='machines', limit=limits.machines, coef=coef, unit='machine'))
    known = {n for kind in MACHINE_KINDS for n in p.raw.get(kind, {})}
    for machine, cap in limits.machines_by_type.items():
        if machine not in known:
            raise ValueError(f'Unknown machine in limits.machines_by_type: {machine}')
        coef = [1 / r.crafts_per_machine if r.machine == machine else 0.0 for r in lines]
        rows.append(
            LimitRow(name=f'machines_by_type:{machine}', limit=check_cap(machine, cap), coef=coef, unit='machine')
        )
    if limits.beacons is not None:
        coef = [r.beacon_entities / r.crafts_per_machine for r in lines]
        rows.append(LimitRow(name='beacons', limit=limits.beacons, coef=coef, unit='beacon'))
    for name, cap in limits.beacons_by_type.items():
        item = beacon_item_named(p, name)
        coef = [r.beacon_items.get(item, 0.0) / r.crafts_per_machine for r in lines]
        rows.append(LimitRow(name=f'beacons_by_type:{item}', limit=check_cap(name, cap), coef=coef, unit='beacon'))
    for module, cap in limits.modules.items():
        if module not in p.raw.get('module', {}):
            raise ValueError(f'Unknown module in limits.modules: {module}')
        coef = [r.module_counts.get(module, 0.0) / r.crafts_per_machine for r in lines]
        rows.append(LimitRow(name=f'modules:{module}', limit=check_cap(module, cap), coef=coef, unit='module'))
    if limits.pollution_per_minute is not None:
        coef = [r.pollution_per_minute / r.crafts_per_machine for r in lines]
        rows.append(
            LimitRow(name='pollution_per_minute', limit=limits.pollution_per_minute, coef=coef, unit='pollution/minute')
        )
    return rows


def beacon_item_named(p: Planner, name: str) -> str:
    """A beacon entity (incl. interference variants) or the item that places one -> the beacon item."""
    if name in p.raw.get('beacon', {}):
        return p.beacon_item(name)
    if any(p.beacon_item(b) == name for b in p.raw.get('beacon', {})):
        return name
    raise ValueError(f'Unknown beacon in limits.beacons_by_type: {name}')


def usage_report(
    states: Sequence[ConstraintState], mode: Mode, factor: float, per: Per
) -> tuple[list[LimitUsage], list[Bottleneck]]:
    usage: list[LimitUsage] = []
    bottlenecks: list[Bottleneck] = []
    for st in states:
        scale = factor if st.rate else 1.0
        limit, used = st.limit * scale, st.used * scale
        unit = f'{st.unit}/{per}' if st.rate else st.unit
        usage.append(
            LimitUsage(
                constraint=st.name, limit=limit, used=used, utilization=used / limit if limit > 0 else None, unit=unit
            )
        )
        tight = abs(st.limit - st.used) <= TIGHT * max(1.0, abs(st.limit))
        if st.marginal is None or not tight or abs(st.marginal) <= 1e-9:
            continue
        gain = 'scale increase' if mode == 'maximize' else 'objective decrease'
        bottlenecks.append(
            Bottleneck(
                constraint=st.name,
                limit=limit,
                used=used,
                marginal=st.marginal / scale,
                meaning=f'{gain} per +1 {unit} of limit',
            )
        )
    return usage, bottlenecks


def limit_key(name: str) -> str:
    """'import:item:x' -> 'imports.item:x', 'machines_by_type:m' -> 'machines_by_type.m'."""
    kind, _, rest = name.partition(':')
    return {'import': 'imports'}.get(kind, kind) + (f'.{rest}' if rest else '')


def infeasibility_report(
    found: Sequence[Infeasibility], factor: float, per: Per
) -> tuple[list[Infeasibility], list[str]]:
    out: list[Infeasibility] = []
    suggestions: list[str] = []
    for f in found:
        scale = factor if f.rate else 1.0
        unit = f'{f.unit}/{per}' if f.rate else f.unit
        g = f.model_copy(
            update=dict(
                requested=f.requested * scale, achievable=f.achievable * scale, shortfall=f.shortfall * scale, unit=unit
            )
        )
        out.append(g)
        if g.kind == 'limit':
            suggestions.append(f'Raise limits.{limit_key(g.name)} to >= {g.achievable:.6g} {unit}')
        elif g.kind == 'target':
            suggestions.append(
                f'Or lower target {g.name} to <= {g.achievable:.6g} {unit} (mode=maximize finds the largest rate)'
            )
        else:
            suggestions.append(f'Or lower consume {g.name} to <= {g.achievable:.6g} {unit}')
    return out, suggestions
