"""Solve a plan's blocks in dependency order, resolve linked targets and sum them into one factory ledger."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, Field

from ..planner import plan as solve_block
from ..planner.disposal import disposal_defaults, dispose
from ..planner.model import TIME, Planner
from ..planner.report import add_totals, module_inventory
from ..planner.schema import ModuleCount, PlanLine, PlanResult, Totals
from .models import Block, BlockResult, PlanFile, Staleness, TargetRef
from .store import PlanStore, now

Detail = Literal['summary', 'full']
ASSUMPTIONS = [
    'Blocks share one bus: any block output can feed any block input; logistics and buffers are not modelled.',
    'Each block chooses its own route; blocks are not optimised jointly.',
    'Factory disposal covers only surplus left after other blocks take what they import.',
]


class BlockOutcome(BaseModel):
    id: str
    status: str
    error: str | None = None
    resolved_targets: dict[str, float] = {}
    achieved_targets: dict[str, float] = {}
    totals: Totals | None = None
    imports: dict[str, float] = {}
    surplus: dict[str, float] = {}
    consume: dict[str, float] = {}
    warnings: list[str] = []
    result: PlanResult | None = Field(None, description='Full solve_production result when detail="full"')


class FactoryLedger(BaseModel):
    """Per item, in units per the plan's `per`."""

    net_inputs: dict[str, float] = Field(description='Imports and consume amounts not covered by other blocks')
    net_outputs: dict[str, float] = Field(description='Target output and surplus not taken by other blocks')
    internal_transfers: dict[str, float] = Field(description='Amount passed between blocks: min(out, in)')
    disposal: list[PlanLine] = Field(description='Void machines for surplus left after internal transfers')
    disposal_unhandled: list[str] = []
    totals: Totals = Field(description='Sum of block totals (production machines, no disposal)')
    totals_with_disposal: Totals
    module_inventory: dict[str, ModuleCount]
    complete: bool = Field(description='False when a block failed; its share is missing from the ledger')
    assumptions: list[str] = ASSUMPTIONS


class FactoryResult(BaseModel):
    plan: str
    revision: int
    stale: Staleness
    order: list[str]
    blocks: list[BlockOutcome]
    factory: FactoryLedger
    warnings: list[str] = []


def dependencies(plan: PlanFile, block: Block) -> set[str]:
    enabled = {b.id for b in plan.blocks if b.enabled}
    out: set[str] = set()
    for v in block.request.targets.values():
        if isinstance(v, TargetRef):
            out |= (enabled - {block.id}) if v.from_ == '*' else set(v.from_)
    return out


def order_blocks(plan: PlanFile, selected: Sequence[str]) -> list[str]:
    """Dependency closure of the selection in topological order (Kahn); a cycle is an error with its path."""
    deps: dict[str, set[str]] = {}
    todo = list(selected)
    while todo:
        bid = todo.pop()
        if bid in deps:
            continue
        deps[bid] = dependencies(plan, plan.block(bid))
        todo += deps[bid]
    remaining = {k: set(v) for k, v in deps.items()}
    order: list[str] = []
    ready = sorted(k for k, v in remaining.items() if not v)
    while ready:
        bid = ready.pop(0)
        order.append(bid)
        del remaining[bid]
        for k, v in remaining.items():
            if bid in v:
                v.discard(bid)
                if not v and k not in ready:
                    ready.append(k)
        ready.sort()
    if remaining:
        raise ValueError(f'Block targets form a cycle: {" -> ".join(cycle_path(remaining))}')
    return order


def cycle_path(graph: Mapping[str, set[str]]) -> list[str]:
    start = min(graph)
    path = [start]
    seen = {start: 0}
    while True:
        nxt = min(n for n in graph[path[-1]] if n in graph)
        if nxt in seen:
            return [*path[seen[nxt] :], nxt]
        seen[nxt] = len(path)
        path.append(nxt)


def resolve_targets(
    plan: PlanFile, block: Block, results: Mapping[str, PlanResult], key: Planner, warnings: list[str]
) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, v in block.request.targets.items():
        if not isinstance(v, TargetRef):
            out[name] = float(v)
            continue
        k = key.key(name)
        refs = sorted(dependencies(plan, block)) if v.from_ == '*' else v.from_
        total = v.plus
        for ref in refs:
            r = results.get(ref)
            if r is None:
                warnings.append(f'{block.id}: {name} refers to block {ref}, which has no result; counted as 0')
            elif k not in r.imports:
                warnings.append(f'{block.id}: block {ref} does not import {k}; counted as 0')
            else:
                total += r.imports[k]
        out[name] = total
    return out


def solve_plan(
    store: PlanStore, name: str, blocks: Sequence[str] = (), detail: Detail = 'summary', save_results: bool = True
) -> FactoryResult:
    plan = store.read(name)
    stale = store.staleness(plan.fingerprint)
    warnings: list[str] = []
    if stale in ('stage_changed', 'prototypes_changed'):
        warnings.append(f'Plan data is {stale}; solved with the current prototypes and save snapshot')
    selected = list(blocks) or [b.id for b in plan.blocks if b.enabled]
    order = order_blocks(plan, selected)
    for bid in order:
        if not plan.block(bid).enabled:
            warnings.append(f'Block {bid} is disabled but solved because another block refers to it')
    key = Planner(store.db, plan.force, validate_stage=False)
    results: dict[str, PlanResult] = {}
    outcomes: list[BlockOutcome] = []
    resolved_by_block: dict[str, dict[str, float]] = {}
    for bid in order:
        block = plan.block(bid)
        try:
            resolved = resolve_targets(plan, block, results, key, warnings)
            resolved_by_block[bid] = resolved
            args = {f: getattr(block.request, f) for f in block.request.model_fields_set if f != 'targets'}
            r = solve_block(store.db, resolved, force=plan.force, per=plan.per, **args)
        except ValueError as e:
            outcomes.append(BlockOutcome(id=bid, status='error', error=str(e)))
            continue
        results[bid] = r
        outcomes.append(
            BlockOutcome(
                id=bid,
                status=r.status,
                resolved_targets=resolved,
                achieved_targets=r.achieved_targets,
                totals=r.totals,
                imports=r.imports,
                surplus=r.surplus,
                consume=r.consume,
                warnings=r.warnings,
                result=r if detail == 'full' else None,
            )
        )
    factory = ledger(store, plan, list(results.values()), complete=len(results) == len(order))
    revision = plan.revision
    if save_results:
        fp = store.fingerprint()
        stamp = now()
        for o in outcomes:
            b = plan.block(o.id)
            if o.id in results:
                b.result = BlockResult.from_plan(results[o.id], resolved_by_block[o.id], fp, stamp)
            else:
                b.result = BlockResult(solved_at=stamp, fingerprint=fp, status='error', error=o.error)
        plan.fingerprint = fp
        revision = store.commit(plan, plan.revision).revision
    return FactoryResult(
        plan=name, revision=revision, stale=stale, order=order, blocks=outcomes, factory=factory, warnings=warnings
    )


def ledger(store: PlanStore, plan: PlanFile, results: Sequence[PlanResult], complete: bool) -> FactoryLedger:
    out: defaultdict[str, float] = defaultdict(float)
    inp: defaultdict[str, float] = defaultdict(float)
    surplus: defaultdict[str, float] = defaultdict(float)
    totals = Totals()
    for r in results:
        for k, v in r.achieved_targets.items():
            out[k] += v
        for k, v in r.surplus.items():
            out[k] += v
            surplus[k] += v
        for k, v in [*r.imports.items(), *r.consume.items()]:
            inp[k] += v
        totals = add_totals(totals, r.totals)
    items = sorted(set(out) | set(inp))
    eps = 1e-9
    net_in = {k: inp[k] - out[k] for k in items if inp[k] - out[k] > eps}
    net_out = {k: out[k] - inp[k] for k in items if out[k] - inp[k] > eps}
    internal = {k: min(out[k], inp[k]) for k in items if min(out[k], inp[k]) > eps}
    # Other blocks' inputs take surplus first; only what is left needs venting.
    leftover = {k: surplus[k] - inp[k] for k in surplus if surplus[k] - inp[k] > eps}
    factor = TIME[plan.per]
    validate = all(b.request.validate_stage for b in plan.blocks if b.enabled)
    p = Planner(store.db, plan.force, validate_stage=validate)
    rows, void_totals, unhandled = dispose(
        p, {k: v / factor for k, v in leftover.items()}, disposal_defaults(None), factor
    )
    lines = [line for r in results for line in r.lines]
    return FactoryLedger(
        net_inputs=net_in,
        net_outputs=net_out,
        internal_transfers=internal,
        disposal=rows,
        disposal_unhandled=unhandled,
        totals=totals,
        totals_with_disposal=add_totals(totals, void_totals),
        module_inventory=module_inventory([*lines, *rows]),
        complete=complete,
    )


class Delta(BaseModel):
    a: float
    b: float
    diff: float = Field(description='b - a')


class Comparison(BaseModel):
    a: str
    b: str
    per: str = Field(description='Rates of both sides are in units per this time unit (that of a)')
    totals: dict[str, Delta]
    imports: dict[str, Delta]
    surplus: dict[str, Delta]
    warnings: list[str] = []


def saved_side(store: PlanStore, ref: str) -> tuple[PlanFile, Totals, dict[str, float], dict[str, float]]:
    """Summed saved results of "plan" (enabled blocks) or "plan/block"."""
    name, _, bid = ref.partition('/')
    plan = store.read(name)
    blocks = [plan.block(bid)] if bid else [b for b in plan.blocks if b.enabled]
    totals = Totals()
    imports: defaultdict[str, float] = defaultdict(float)
    surplus: defaultdict[str, float] = defaultdict(float)
    for b in blocks:
        r = b.result
        if r is None or r.totals is None:
            raise ValueError(f'{ref}: block {b.id} has no saved result; run plan_solve on {name} first')
        stale = store.staleness(r.fingerprint)
        if stale in ('stage_changed', 'prototypes_changed'):
            raise ValueError(f'{ref}: block {b.id} result is {stale}; run plan_solve on {name} first')
        totals = add_totals(totals, r.totals)
        for k, v in r.imports.items():
            imports[k] += v
        for k, v in r.surplus.items():
            surplus[k] += v
    return plan, totals, dict(imports), dict(surplus)


def flat_totals(t: Totals) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in t.model_dump().items():
        if isinstance(v, dict):
            out.update({f'{k}.{name}': n for name, n in v.items()})
        else:
            out[k] = float(v)
    return out


def deltas(a: Mapping[str, float], b: Mapping[str, float], scale_b: float = 1.0) -> dict[str, Delta]:
    out: dict[str, Delta] = {}
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k, 0.0), b.get(k, 0.0) * scale_b
        out[k] = Delta(a=va, b=vb, diff=vb - va)
    return out


def compare_plans(store: PlanStore, a: str, b: str) -> Comparison:
    plan_a, totals_a, imports_a, surplus_a = saved_side(store, a)
    plan_b, totals_b, imports_b, surplus_b = saved_side(store, b)
    warnings: list[str] = []
    scale = TIME[plan_a.per] / TIME[plan_b.per]
    if plan_a.per != plan_b.per:
        warnings.append(f'{b} rates converted from per {plan_b.per} to per {plan_a.per}')
    return Comparison(
        a=a,
        b=b,
        per=plan_a.per,
        totals=deltas(flat_totals(totals_a), flat_totals(totals_b)),
        imports=deltas(imports_a, imports_b, scale),
        surplus=deltas(surplus_a, surplus_b, scale),
        warnings=warnings,
    )
