"""Public entry points used by the MCP server, scripts and tests."""
import math
from collections.abc import Iterable, Mapping, Sequence

import numpy as np

from ..database import JSON, Database

from .lp import solve_lp
from .matrix import analyze_matrix, recipe_matrix, solve_matrix
from .model import OBJECTIVES, TIME, Planner
from .report import report


def plan(db: Database, targets: Mapping[str, float], lines: Sequence[str | JSON] = (), solver: str = 'lp',
         per: str = 'second', defaults: JSON | None = None, auto_discover: bool | None = None, imports: Iterable[str] = (),
         forbid_imports: Iterable[str] = (), import_costs: Mapping[str, float] | None = None, allow_surplus: bool = True,
         surplus_items: Iterable[str] = (), objective: str = 'balanced', weights: Mapping[str, float] | None = None,
         exclude_recipes: Iterable[str] = (), max_depth: int = 12, max_lines: int = 1000, include_hidden: bool = False,
         allow_mining: bool = True, mining_productivity: float = 0.0, research_productivity: bool = True,
         force: str = '', validate_stage: bool = True) -> JSON:
    if per not in TIME: raise ValueError('per must be second, minute or hour')
    if solver not in ('lp', 'matrix'): raise ValueError('solver must be lp or matrix')
    factor = TIME[per]
    p = Planner(db, force, validate_stage, research_productivity, mining_productivity)
    tgt: dict[str, float] = {}
    for name, rate in targets.items():
        if not math.isfinite(rate) or rate < 0: raise ValueError('Target rates must be finite and nonnegative')
        tgt[p.key(name)] = rate / factor
    if not tgt: raise ValueError('At least one target is required')
    imports = {p.key(n) for n in imports}
    forbid = {p.key(n) for n in forbid_imports}
    surplus = {p.key(n) for n in surplus_items}
    costs = {p.key(k): v for k, v in (import_costs or {}).items()}
    auto = (not lines) if auto_discover is None else auto_discover
    if solver == 'matrix' and auto and not lines:
        raise ValueError('The matrix solver needs one chosen recipe per intermediate: pass lines (e.g. from an lp result) '
                         'or set auto_discover=true and remove alternatives with exclude_recipes.')
    rows, excluded, truncated, frontier = p.build_lines(list(lines), tgt, defaults or {}, auto, imports, set(exclude_recipes),
                                                        max(1, min(max_depth, 30)), max(1, min(max_lines, 2000)), include_hidden, allow_mining)
    if not rows: raise ValueError('No usable production lines for the targets')
    if solver == 'lp':
        if objective not in OBJECTIVES: raise ValueError('objective must be balanced, machines, power or imports')
        w = {**OBJECTIVES[objective], **(weights or {})}
        sol = solve_lp(rows, tgt, imports, forbid, costs, allow_surplus, surplus, w)
    else:
        sol = solve_matrix(rows, tgt, imports, surplus, p.warnings)
    out = report(p, rows, sol, tgt, factor)
    if frontier: out['warnings'].append(f'max_depth reached; treated as imports where needed: {frontier[:20]}')
    if truncated: out['warnings'].append(f'max_lines={max_lines} reached; candidate recipes were dropped')
    chosen = {r['id'] for r in out['lines']}
    out.update(solver=solver, per=per, force=p.force, validate_stage=validate_stage,
               targets={k: v * factor for k, v in tgt.items()}, candidate_lines=len(rows),
               excluded_candidates=excluded[:50],
               lines_for_matrix=[{'id': r['id'], 'recipe': r['recipe'], 'machine': r['machine'], 'modules': r['modules'],
                                  **({'beacons': [{k: b[k] for k in ('beacon', 'count', 'modules', 'per_machine')} for b in r['beacons']]} if r['beacons'] else {})}
                                 for r in rows if r['id'] in chosen],
               matrix_args={'surplus_items': sorted(k for k in out['surplus'] if k not in tgt),
                            'imports': sorted(k for k in out['imports'])} if solver == 'lp' else None,
               scope='Expected-value rates. Research gates checked when validate_stage; quality, surface effects, '
                     'logistics throughput and fluid-resource depletion not modelled.')
    if validate_stage:
        prov = db.progress.get('provenance', {})
        out['snapshot_provenance'] = {'tick': db.progress.get('tick'), **{k: prov.get(k) for k in ('source_save', 'source_copy_sha256', 'exported_at')}}
    return out


def machine_stats(db: Database, recipe: str, machine: str = '', modules: Mapping[str, int] | Sequence[str] | None = None,
                  beacons: Iterable[JSON] = (), defaults: JSON | None = None, rate: float | None = None, item: str = '',
                  per: str = 'second', force: str = '', validate_stage: bool = True, mining_productivity: float = 0.0,
                  research_productivity: bool = True) -> JSON:
    """Single line calculator (one recipe in one machine), optional machine count for a rate."""
    if per not in TIME: raise ValueError('per must be second, minute or hour')
    factor = TIME[per]
    p = Planner(db, force, validate_stage, research_productivity, mining_productivity)
    spec: JSON = {'recipe': recipe, 'beacons': list(beacons)}
    if machine: spec['machine'] = machine
    if modules is not None: spec['modules'] = modules
    r = p.line(spec, defaults or {})
    out = {k: r[k] for k in ('recipe', 'machine', 'machine_type', 'modules', 'beacons', 'speed_multiplier', 'productivity',
                             'research_productivity', 'consumption_multiplier', 'pollution_multiplier', 'energy_type', 'blocked')}
    out.update(per=per, crafts_per_machine=r['crafts_per_machine'] * factor,
               per_machine={k: v * r['crafts_per_machine'] * factor for k, v in r['balance'].items()},
               power_per_machine_MW=dict(active=r['active_W'] / 1e6, drain=r['drain_W'] / 1e6, beacons=r['beacon_W'] / 1e6),
               pollution_per_minute_per_machine=r['pollution_per_minute'], valid_at_stage=not r['blocked'] if validate_stage else None,
               alternatives=[n for _, n, m in p.candidates(p.recipe_view(recipe))],
               warnings=p.warnings)
    if rate is not None:
        k = p.key(item) if item else max(r['balance'], key=lambda x: r['balance'][x])
        per_machine = r['balance'].get(k, 0) * r['crafts_per_machine']
        if per_machine == 0: raise ValueError(f'{recipe} does not touch {k}')
        machines = abs(rate / factor / per_machine)
        out['for_rate'] = {'item': k, 'rate': rate, 'machines': machines, 'machines_ceil': math.ceil(machines - 1e-6),
                           'power_MW': machines * (r['active_W'] + r['drain_W'] + r['beacon_W']) / 1e6 if r['energy_type'] == 'electric' else machines * r['beacon_W'] / 1e6}
    return out


def production_matrix(db: Database, lines: Sequence[str | JSON], targets: Mapping[str, float] | None = None,
                      imports: Iterable[str] = (), surplus_items: Iterable[str] = (), defaults: JSON | None = None,
                      force: str = '', validate_stage: bool = False, dense_limit: int = 60) -> JSON:
    """Stoichiometric matrix (items x lines), rank and Factory Planner style determinacy report."""
    p = Planner(db, force, validate_stage)
    rows = [p.line(s, defaults or {}) for s in lines]
    tgt = {p.key(k): v for k, v in (targets or {}).items()}
    keys, A = recipe_matrix(rows)
    _, _, _, _, info, _ = analyze_matrix(rows, tgt, {p.key(n) for n in imports}, {p.key(n) for n in surplus_items})
    out = {'lines': [r['id'] for r in rows], 'items': keys, 'rank_recipe_matrix': int(np.linalg.matrix_rank(A)) if A.size else 0,
           'square_system': info, 'blocked': {r['id']: r['blocked'] for r in rows if r['blocked']}}
    if info['degrees_of_freedom'] > 0:
        out['hint'] = 'Underdetermined: remove alternative recipes, fix a line with fixed_machines, or drop imports/surplus_items.'
    elif info['unknowns'] < info['items']:
        out['hint'] = 'More items than unknowns: the system only solves if the extra equations are consistent; otherwise mark items as surplus_items or imports.'
    if len(keys) * len(rows) <= dense_limit * dense_limit:
        out['matrix'] = [[float(v) for v in row] for row in A]
    else:
        out['nonzeros'] = [[keys[i], rows[j]['id'], float(A[i, j])] for i, j in zip(*np.nonzero(A))]
    return out
