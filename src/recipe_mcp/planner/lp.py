"""Linear-programming solver (HiGHS): alternatives, imports, penalised surplus, shadow prices."""

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import linprog
from scipy.sparse import csr_matrix

from .schema import Line, Solution, Weights

Coeffs = Mapping[int, float]


class LPBuilder:
    """Named columns and rows of one LP, so solutions and duals map back to names."""

    def __init__(self) -> None:
        self.cols: list[str] = []
        self.index: dict[str, int] = {}
        self.cost: list[float] = []
        self.bounds: list[tuple[float, float | None]] = []
        self.eq_names: list[str] = []
        self.eq_rows: list[Coeffs] = []
        self.eq_rhs: list[float] = []
        self.ub_names: list[str] = []
        self.ub_rows: list[Coeffs] = []
        self.ub_rhs: list[float] = []

    def col(self, name: str, cost: float = 0.0, lb: float = 0.0, ub: float | None = None) -> int:
        if name in self.index:
            raise ValueError(f'Duplicate LP column {name}')
        self.index[name] = len(self.cols)
        self.cols.append(name)
        self.cost.append(cost)
        self.bounds.append((lb, ub))
        return self.index[name]

    def add_eq(self, name: str, coeffs: Coeffs, rhs: float) -> int:
        self.eq_names.append(name)
        self.eq_rows.append(coeffs)
        self.eq_rhs.append(rhs)
        return len(self.eq_names) - 1

    def add_ub(self, name: str, coeffs: Coeffs, rhs: float) -> int:
        self.ub_names.append(name)
        self.ub_rows.append(coeffs)
        self.ub_rhs.append(rhs)
        return len(self.ub_names) - 1

    def _matrix(self, rows: Sequence[Coeffs]) -> csr_matrix:
        data: list[float] = []
        ri: list[int] = []
        ci: list[int] = []
        for i, row in enumerate(rows):
            for j, v in row.items():
                if v:
                    ri.append(i)
                    ci.append(j)
                    data.append(v)
        return csr_matrix((data, (ri, ci)), shape=(len(rows), len(self.cols)))

    def arrays(self) -> tuple[NDArray[np.float64], csr_matrix, NDArray[np.float64], csr_matrix, NDArray[np.float64]]:
        return (
            np.array(self.cost, dtype=float),
            self._matrix(self.eq_rows),
            np.array(self.eq_rhs, dtype=float),
            self._matrix(self.ub_rows),
            np.array(self.ub_rhs, dtype=float),
        )

    def solve(self, cost: Sequence[float] | None = None) -> Any:
        c, A_eq, b_eq, A_ub, b_ub = self.arrays()
        if cost is not None:
            c = np.array(cost, dtype=float)
        return linprog(
            c,
            A_ub=A_ub if self.ub_names else None,
            b_ub=b_ub if self.ub_names else None,
            A_eq=A_eq if self.eq_names else None,
            b_eq=b_eq if self.eq_names else None,
            bounds=self.bounds,
            method='highs',
        )

    def eq_residual(self, x: NDArray[np.float64]) -> float:
        _, A_eq, b_eq, _, _ = self.arrays()
        return float(np.max(np.abs(A_eq @ x - b_eq))) if len(b_eq) else 0.0


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
    produced = {k for r in lines for k, v in r.balance.items() if v > 0}
    importable = [
        k for k in keys if (k not in produced or k in imports) and k not in forbid_imports and k not in targets
    ]
    surplus_keys = keys if allow_surplus else [k for k in keys if k in surplus_items or k in targets]
    lp = LPBuilder()
    rows: dict[str, dict[int, float]] = {k: {} for k in keys}
    for j, r in enumerate(lines):
        ub: float | None
        if r.fixed_machines is not None:
            lb = ub = r.fixed_machines * r.crafts_per_machine
        else:
            lb, ub = 0, None if r.max_machines is None else r.max_machines * r.crafts_per_machine
        # Line variables are crafts per second, so per-machine costs scale by 1 / crafts_per_machine.
        cost = r.cost_weight * (weights.machines + weights.power_MW * r.power_W / 1e6) / r.crafts_per_machine
        c = lp.col(f'x:{j}', cost, lb, ub)
        for k, v in r.balance.items():
            rows[k][c] = v
    for k in importable:
        rows[k][lp.col(f'm:{k}', weights.imports * import_costs.get(k, 1.0))] = 1
    for k in surplus_keys:
        rows[k][lp.col(f'u:{k}', weights.surplus)] = -1
    for k in keys:
        lp.add_eq(k, rows[k], targets.get(k, 0.0))
    res = lp.solve()
    if not res.success:
        missing = sorted(k for k in targets if k not in produced and k not in importable)
        hint = (
            f'; no producing line for {missing}'
            if missing
            else '; check forbid_imports, fixed_machines/max_machines or allow_surplus'
        )
        raise ValueError(f'LP infeasible: {res.message}{hint}')
    x = res.x
    L = len(lines)
    prices = dict(zip(keys, res.eqlin.marginals, strict=True)) if getattr(res, 'eqlin', None) is not None else {}
    return Solution(
        x=x[:L].tolist(),
        imports={k: float(x[lp.index[f'm:{k}']]) for k in importable},
        surplus={k: float(x[lp.index[f'u:{k}']]) for k in surplus_keys},
        residual=lp.eq_residual(x),
        objective=float(res.fun),
        prices={k: float(v) for k, v in prices.items()},
        status=res.message,
    )
