"""Linear-programming solver (HiGHS): alternatives, imports, penalised surplus, shadow prices."""
from collections.abc import Mapping, Sequence

import numpy as np
from scipy.optimize import linprog

from ..database import JSON
from .model import Line


def solve_lp(lines: Sequence[Line], targets: Mapping[str, float], imports: set[str], forbid_imports: set[str],
             import_costs: Mapping[str, float], allow_surplus: bool, surplus_items: set[str],
             weights: Mapping[str, float]) -> JSON:
    keys = sorted({k for r in lines for k in r['balance']} | set(targets))
    index = {k: i for i, k in enumerate(keys)}
    produced = {k for r in lines for k, v in r['balance'].items() if v > 0}
    importable = [k for k in keys if (k not in produced or k in imports) and k not in forbid_imports and k not in targets]
    surplus_keys = keys if allow_surplus else [k for k in keys if k in surplus_items or k in targets]
    L, I, S = len(lines), len(importable), len(surplus_keys)
    A = np.zeros((len(keys), L + I + S))
    for j, r in enumerate(lines):
        for k, v in r['balance'].items(): A[index[k], j] = v
    for j, k in enumerate(importable): A[index[k], L + j] = 1
    for j, k in enumerate(surplus_keys): A[index[k], L + I + j] = -1
    b = np.array([targets.get(k, 0.0) for k in keys])
    c: list[float] = []
    for r in lines:
        per_craft_machines = 1 / r['crafts_per_machine']
        mw = (r['active_W'] + r['drain_W'] + r['beacon_W']) / 1e6 if r['energy_type'] == 'electric' else r['beacon_W'] / 1e6
        c.append(r['cost_weight'] * per_craft_machines * (weights['machines'] + weights['power_MW'] * mw))
    c += [weights['imports'] * import_costs.get(k, 1.0) for k in importable]
    c += [weights['surplus']] * S
    bounds: list[tuple[float, float | None]] = []
    for r in lines:
        if r['fixed_machines'] is not None:
            v = r['fixed_machines'] * r['crafts_per_machine']
            bounds.append((v, v))
        else:
            bounds.append((0, None if r['max_machines'] is None else r['max_machines'] * r['crafts_per_machine']))
    bounds += [(0, None)] * (I + S)
    res = linprog(c, A_eq=A, b_eq=b, bounds=bounds, method='highs')
    if not res.success:
        missing = sorted(k for k in targets if k not in produced and k not in importable)
        raise ValueError(f'LP infeasible: {res.message}' + (f'; no producing line for {missing}' if missing else
                         '; check forbid_imports, fixed_machines/max_machines or allow_surplus'))
    x = res.x.tolist()
    prices = dict(zip(keys, res.eqlin.marginals)) if getattr(res, 'eqlin', None) is not None else {}
    return dict(x=x[:L], imports=dict(zip(importable, x[L:L + I])), surplus=dict(zip(surplus_keys, x[L + I:])),
                residual=float(np.max(np.abs(A @ res.x - b))) if len(b) else 0.0, objective=float(res.fun),
                prices={k: float(v) for k, v in prices.items()},
                status=res.message)
