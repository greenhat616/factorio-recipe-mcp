"""Public entry points used by the MCP server, scripts and tests."""

import math
from collections.abc import Iterable, Mapping, Sequence

import numpy as np

from ..database import Database
from .lp import solve_lp
from .matrix import analyze_matrix, recipe_matrix, solve_matrix
from .model import OBJECTIVES, TIME, Planner
from .report import report
from .schema import (
    BeaconSpec,
    Defaults,
    LineSpec,
    MachinePower,
    MachineStats,
    MatrixArgs,
    ModuleSpec,
    Objective,
    Per,
    PlanResult,
    ProductionMatrix,
    RateRequirement,
    SnapshotProvenance,
    SolverName,
    WeightName,
    Weights,
)


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
) -> PlanResult:
    factor = TIME[per]
    p = Planner(db, force, validate_stage, research_productivity, mining_productivity)
    tgt: dict[str, float] = {}
    for name, rate in targets.items():
        if not math.isfinite(rate) or rate < 0:
            raise ValueError('Target rates must be finite and nonnegative')
        tgt[p.key(name)] = rate / factor
    if not tgt:
        raise ValueError('At least one target is required')
    import_keys = {p.key(n) for n in imports}
    forbid = {p.key(n) for n in forbid_imports}
    surplus = {p.key(n) for n in surplus_items}
    costs = {p.key(k): v for k, v in (import_costs or {}).items()}
    auto = (not lines) if auto_discover is None else auto_discover
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
        import_keys,
        set(exclude_recipes),
        max(1, min(max_depth, 30)),
        max(1, min(max_lines, 2000)),
        include_hidden,
        allow_mining,
    )
    if not rows:
        raise ValueError('No usable production lines for the targets')
    if solver == 'lp':
        w = Weights.model_validate({**OBJECTIVES[objective].model_dump(), **(weights or {})})
        sol = solve_lp(rows, tgt, import_keys, forbid, costs, allow_surplus, surplus, w)
    else:
        sol = solve_matrix(rows, tgt, import_keys, surplus, p.warnings)
    out = report(p, rows, sol, tgt, factor)
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
        solver=solver,
        per=per,
        force=p.force,
        validate_stage=validate_stage,
        targets={k: v * factor for k, v in tgt.items()},
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
) -> ProductionMatrix:
    """Stoichiometric matrix (items x lines), rank and Factory Planner style determinacy report."""
    p = Planner(db, force, validate_stage)
    rows = [p.line(s, defaults) for s in lines]
    tgt = {p.key(k): v for k, v in (targets or {}).items()}
    keys, A = recipe_matrix(rows)
    *_, info, _ = analyze_matrix(rows, tgt, {p.key(n) for n in imports}, {p.key(n) for n in surplus_items})
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
