"""Linear-programming solver (HiGHS): alternatives, imports, penalised surplus, shadow prices."""

from collections.abc import Mapping, Sequence

import numpy as np
from scipy.optimize import linprog

from .schema import Line, Solution, Weights


def solve_lp(
    lines: Sequence[Line],
    targets: Mapping[str, float],
    imports: set[str],
    forbid_imports: set[str],
    import_costs: Mapping[str, float],
    allow_surplus: bool,
    surplus_items: set[str],
    weights: Weights,
) -> Solution:
    keys = sorted({k for r in lines for k in r.balance} | set(targets))
    index = {k: i for i, k in enumerate(keys)}
    produced = {k for r in lines for k, v in r.balance.items() if v > 0}
    importable = [
        k for k in keys if (k not in produced or k in imports) and k not in forbid_imports and k not in targets
    ]
    surplus_keys = keys if allow_surplus else [k for k in keys if k in surplus_items or k in targets]
    L, I, S = len(lines), len(importable), len(surplus_keys)  # noqa: E741
    A = np.zeros((len(keys), L + I + S))
    for j, r in enumerate(lines):
        for k, v in r.balance.items():
            A[index[k], j] = v
    for j, k in enumerate(importable):
        A[index[k], L + j] = 1
    for j, k in enumerate(surplus_keys):
        A[index[k], L + I + j] = -1
    b = np.array([targets.get(k, 0.0) for k in keys])
    # Line variables are crafts per second, so per-machine costs scale by 1 / crafts_per_machine.
    c = [r.cost_weight * (weights.machines + weights.power_MW * r.power_W / 1e6) / r.crafts_per_machine for r in lines]
    c += [weights.imports * import_costs.get(k, 1.0) for k in importable]
    c += [weights.surplus] * S
    bounds: list[tuple[float, float | None]] = []
    for r in lines:
        if r.fixed_machines is not None:
            v = r.fixed_machines * r.crafts_per_machine
            bounds.append((v, v))
        else:
            bounds.append((0, None if r.max_machines is None else r.max_machines * r.crafts_per_machine))
    bounds += [(0, None)] * (I + S)
    res = linprog(c, A_eq=A, b_eq=b, bounds=bounds, method='highs')
    if not res.success:
        missing = sorted(k for k in targets if k not in produced and k not in importable)
        hint = (
            f'; no producing line for {missing}'
            if missing
            else '; check forbid_imports, fixed_machines/max_machines or allow_surplus'
        )
        raise ValueError(f'LP infeasible: {res.message}{hint}')
    x = res.x.tolist()
    prices = dict(zip(keys, res.eqlin.marginals, strict=True)) if getattr(res, 'eqlin', None) is not None else {}
    return Solution(
        x=x[:L],
        imports=dict(zip(importable, x[L : L + I], strict=True)),
        surplus=dict(zip(surplus_keys, x[L + I :], strict=True)),
        residual=float(np.max(np.abs(A @ res.x - b))) if len(b) else 0.0,
        objective=float(res.fun),
        prices={k: float(v) for k, v in prices.items()},
        status=res.message,
    )
