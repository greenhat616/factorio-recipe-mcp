"""Public entry points used by the MCP server, scripts and tests."""

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np

from ..database import Database
from .disposal import disposal_defaults as disposal_defaults_for
from .disposal import dispose
from .limits import infeasibility_report, limit_rows, usage_report
from .lp import ELASTIC_NOTE, solve_lp
from .matrix import analyze_matrix, recipe_matrix, solve_matrix
from .model import OBJECTIVES, TIME, Planner
from .report import add_totals, module_inventory, report
from .schema import (
    BeaconSpec,
    Defaults,
    DisposalMode,
    Limits,
    LineSpec,
    MachinePower,
    MachineStats,
    MatrixArgs,
    Mode,
    ModuleSpec,
    Objective,
    Per,
    PlanLine,
    PlanResult,
    PlanStatus,
    ProductionMatrix,
    RateRequirement,
    SnapshotProvenance,
    SolverName,
    Totals,
    WeightName,
    Weights,
)

# A MILP over more candidate lines than this is slow and rarely needed: route choice is a continuous question.
MAX_INTEGER_LINES = 400


def plan(
    db: Database,
    targets: Mapping[str, float],
    lines: Sequence[str | LineSpec] = (),
    solver: SolverName = 'lp',
    per: Per = 'second',
    defaults: Defaults | None = None,
    auto_discover: bool | None = None,
    imports: Iterable[str] = (),
    forbid_imports: Iterable[str] = (),
    import_costs: Mapping[str, float] | None = None,
    allow_surplus: bool = True,
    surplus_items: Iterable[str] = (),
    objective: Objective = 'balanced',
    weights: Mapping[WeightName, float] | None = None,
    exclude_recipes: Iterable[str] = (),
    max_depth: int = 12,
    max_lines: int = 1000,
    include_hidden: bool = False,
    allow_mining: bool = True,
    mining_productivity: float = 0.0,
    research_productivity: bool = True,
    force: str = '',
    validate_stage: bool = True,
    mode: Mode = 'targets',
    limits: Limits | Mapping[str, Any] | None = None,
    consume: Mapping[str, float] | None = None,
    disposal: DisposalMode = 'report',
    disposal_defaults: Defaults | Mapping[str, Any] | None = None,
    integer_machines: bool = False,
    time_limit: float = 10.0,
) -> PlanResult:
    factor = TIME[per]
    void_defaults = disposal_defaults_for(disposal_defaults)
    lim = limits if isinstance(limits, Limits) else Limits.model_validate(limits or {})
    p = Planner(db, force, validate_stage, research_productivity, mining_productivity)
    tgt: dict[str, float] = {}
    for name, rate in targets.items():
        if mode == 'maximize' and (not math.isfinite(rate) or rate <= 0):
            raise ValueError(f'mode=maximize reads targets as ratios, which must be positive; got {name}={rate}')
        if not math.isfinite(rate) or rate < 0:
            raise ValueError('Target rates must be finite and nonnegative')
        tgt[p.key(name)] = rate / factor
    supply: dict[str, float] = {}
    for name, rate in (consume or {}).items():
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f'consume rates must be finite and positive; got {name}={rate}')
        key = p.key(name)
        if key in tgt:
            raise ValueError(f'{key} is both a target and consumed; an item is either produced or supplied')
        supply[key] = rate / factor
    if not tgt and mode == 'maximize':
        raise ValueError('mode=maximize needs ratio targets to scale')
    constrained = lim != Limits()
    if solver == 'matrix' and (mode == 'maximize' or constrained or integer_machines):
        raise ValueError('mode=maximize, limits and integer_machines need the lp solver; use solver="lp"')
    import_keys = {p.key(n) for n in imports}
    forbid = {p.key(n) for n in forbid_imports}
    surplus = {p.key(n) for n in surplus_items}
    costs = {p.key(k): v for k, v in (import_costs or {}).items()}
    auto = (not lines) if auto_discover is None else auto_discover
    if auto and not tgt:
        raise ValueError(
            'Auto-discovery searches upstream from targets and there are none: pass lines that use the consumed '
            'items, or give ratio targets with mode=maximize.'
        )
    if solver == 'matrix' and auto and not lines:
        raise ValueError(
            'The matrix solver needs one chosen recipe per intermediate: pass lines (e.g. from an lp result) '
            'or set auto_discover=true and remove alternatives with exclude_recipes.'
        )
    rows, excluded, truncated, frontier = p.build_lines(
        lines,
        tgt,
        defaults or Defaults(),
        auto,
        # Consumed items are supplied, so their producers are not searched for.
        import_keys | set(supply),
        set(exclude_recipes),
        max(1, min(max_depth, 30)),
        max(1, min(max_lines, 2000)),
        include_hidden,
        allow_mining,
    )
    if not rows:
        raise ValueError('No usable production lines for the targets')
    if not tgt and not supply and all(r.fixed_machines is None for r in rows):
        raise ValueError('Nothing sets the scale: give targets, consume or a line with fixed_machines')
    if solver == 'lp':
        w = Weights.model_validate({**OBJECTIVES[objective].model_dump(), **(weights or {})})
        if w.beacons or w.modules:
            raise ValueError('weights.beacons and weights.modules are reserved and not implemented yet')
        if integer_machines and len(rows) > MAX_INTEGER_LINES:
            raise ValueError(
                f'integer_machines allows at most {MAX_INTEGER_LINES} lines, got {len(rows)}: solve continuously '
                'first, then pass its lines_for_matrix as lines with auto_discover=false.'
            )
        limit_list = limit_rows(p, rows, lim, factor)
        sol = solve_lp(
            rows,
            tgt,
            import_keys,
            forbid,
            costs,
            allow_surplus,
            surplus,
            w,
            mode,
            limit_list,
            supply,
            integer_machines,
            time_limit,
        )
    else:
        sol = solve_matrix(rows, tgt, import_keys, surplus, p.warnings, supply)
    infeasible = sol.status == 'infeasible'
    status: PlanStatus = 'infeasible' if infeasible else 'time_limit' if sol.status == 'time_limit' else 'optimal'
    short = {f.name: f.shortfall for f in sol.infeasibility if f.kind != 'limit'}
    if sol.scale is not None:
        achieved = {k: v * sol.scale for k, v in tgt.items()}
    else:
        achieved = {k: v - short.get(k, 0.0) for k, v in tgt.items()}
    used_supply = {k: v - short.get(k, 0.0) for k, v in supply.items()}
    out = report(p, rows, sol, achieved, factor, consume=used_supply)
    out.warnings += sol.warnings
    if sol.status == 'time_limit':
        out.warnings.append(f'time_limit={time_limit}s reached: best integer plan found, MIP gap {sol.mip_gap}')
    usage, bottlenecks = usage_report(sol.constraints, mode, factor, per)
    infeasibility, suggestions = infeasibility_report(sol.infeasibility, factor, per)
    void_rows: list[PlanLine] = []
    void_totals: Totals | None = None
    unhandled: list[str] = []
    if disposal == 'report':
        byproducts = {k: v for k, v in sol.surplus.items() if k not in tgt}
        void_rows, void_totals, unhandled = dispose(p, byproducts, void_defaults, factor)
    if frontier:
        out.warnings.append(f'max_depth reached; treated as imports where needed: {frontier[:20]}')
    if truncated:
        out.warnings.append(f'max_lines={max_lines} reached; candidate recipes were dropped')
    chosen = {r.id for r in out.lines}
    provenance = None
    if validate_stage:
        prov = db.progress.get('provenance', {})
        provenance = SnapshotProvenance(
            tick=db.progress.get('tick'),
            source_save=prov.get('source_save'),
            source_copy_sha256=prov.get('source_copy_sha256'),
            exported_at=prov.get('exported_at'),
        )
    return PlanResult(
        **dict(out),
        status=status,
        integer_machines=integer_machines,
        mip_gap=sol.mip_gap,
        bottlenecks_note='Marginal values are not available for integer machine counts' if integer_machines else None,
        disposal=void_rows,
        disposal_totals=void_totals,
        totals_with_disposal=add_totals(out.totals, void_totals) if void_totals else None,
        disposal_unhandled=unhandled,
        module_inventory=module_inventory([*out.lines, *void_rows]),
        infeasibility=infeasibility,
        suggestions=suggestions,
        note=ELASTIC_NOTE if infeasible else None,
        solver=solver,
        mode=mode,
        per=per,
        force=p.force,
        validate_stage=validate_stage,
        targets={k: v * factor for k, v in tgt.items()},
        scale=sol.scale,
        achieved_targets={k: v * factor for k, v in achieved.items()},
        consume={k: v * factor for k, v in supply.items()},
        limits_usage=usage,
        bottlenecks=bottlenecks,
        candidate_lines=len(rows),
        excluded_candidates=excluded[:50],
        lines_for_matrix=[r.pin() for r in rows if r.id in chosen],
        matrix_args=MatrixArgs(
            surplus_items=sorted(k for k in out.surplus if k not in tgt),
            imports=sorted(out.imports),
        )
        if solver == 'lp'
        else None,
        snapshot_provenance=provenance,
    )


def machine_stats(
    db: Database,
    recipe: str,
    machine: str = '',
    modules: ModuleSpec | None = None,
    beacons: Sequence[BeaconSpec] | None = None,
    defaults: Defaults | None = None,
    rate: float | None = None,
    item: str = '',
    per: Per = 'second',
    force: str = '',
    validate_stage: bool = True,
    mining_productivity: float = 0.0,
    research_productivity: bool = True,
) -> MachineStats:
    """Single line calculator (one recipe in one machine), optional machine count for a rate."""
    factor = TIME[per]
    p = Planner(db, force, validate_stage, research_productivity, mining_productivity)
    spec = LineSpec(
        recipe=recipe,
        machine=machine or None,
        modules=modules,
        beacons=None if beacons is None else list(beacons),
    )
    r = p.line(spec, defaults)
    for_rate = None
    if rate is not None:
        k = p.key(item) if item else max(r.balance, key=lambda x: r.balance[x])
        per_machine = r.balance.get(k, 0) * r.crafts_per_machine
        if per_machine == 0:
            raise ValueError(f'{recipe} does not touch {k}')
        machines = abs(rate / factor / per_machine)
        for_rate = RateRequirement(
            item=k,
            rate=rate,
            machines=machines,
            machines_ceil=math.ceil(machines - 1e-6),
            power_MW=machines * r.power_W / 1e6,
        )
    return MachineStats(
        recipe=r.recipe,
        machine=r.machine,
        machine_type=r.machine_type,
        modules=r.modules,
        beacons=r.beacons,
        speed_multiplier=r.speed_multiplier,
        productivity=r.productivity,
        research_productivity=r.research_productivity,
        consumption_multiplier=r.consumption_multiplier,
        pollution_multiplier=r.pollution_multiplier,
        energy_type=r.energy_type,
        blocked=r.blocked,
        per=per,
        crafts_per_machine=r.crafts_per_machine * factor,
        per_machine={k: v * r.crafts_per_machine * factor for k, v in r.balance.items()},
        power_per_machine_MW=MachinePower(active=r.active_W / 1e6, drain=r.drain_W / 1e6, beacons=r.beacon_W / 1e6),
        pollution_per_minute_per_machine=r.pollution_per_minute,
        beacon_items=r.beacon_items,
        module_counts=r.module_counts,
        valid_at_stage=not r.blocked if validate_stage else None,
        alternatives=[m.name for m in p.candidates(p.recipe_view(recipe))],
        warnings=p.warnings,
        for_rate=for_rate,
    )


def production_matrix(
    db: Database,
    lines: Sequence[str | LineSpec],
    targets: Mapping[str, float] | None = None,
    imports: Iterable[str] = (),
    surplus_items: Iterable[str] = (),
    defaults: Defaults | None = None,
    force: str = '',
    validate_stage: bool = False,
    dense_limit: int = 60,
    consume: Mapping[str, float] | None = None,
) -> ProductionMatrix:
    """Stoichiometric matrix (items x lines), rank and Factory Planner style determinacy report."""
    p = Planner(db, force, validate_stage)
    rows = [p.line(s, defaults) for s in lines]
    tgt = {p.key(k): v for k, v in (targets or {}).items()}
    keys, A = recipe_matrix(rows)
    supply = {p.key(k): v for k, v in (consume or {}).items()}
    *_, info, _ = analyze_matrix(rows, tgt, {p.key(n) for n in imports}, {p.key(n) for n in surplus_items}, supply)
    out = ProductionMatrix(
        lines=[r.id for r in rows],
        items=keys,
        rank_recipe_matrix=int(np.linalg.matrix_rank(A)) if A.size else 0,
        square_system=info,
        blocked={r.id: r.blocked for r in rows if r.blocked},
    )
    if info.degrees_of_freedom > 0:
        out.hint = 'Underdetermined: remove alternative recipes, fix a line with fixed_machines, or drop imports/surplus_items.'
    elif info.unknowns < info.items:
        out.hint = (
            'More items than unknowns: the system only solves if the extra equations are consistent; '
            'otherwise mark items as surplus_items or imports.'
        )
    if len(keys) * len(rows) <= dense_limit * dense_limit:
        out.matrix = [[float(v) for v in row] for row in A]
    else:
        out.nonzeros = [(keys[i], rows[j].id, float(A[i, j])) for i, j in zip(*np.nonzero(A), strict=True)]
    return out
