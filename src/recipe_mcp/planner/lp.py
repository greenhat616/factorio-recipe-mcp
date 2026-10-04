"""Linear-programming solver (HiGHS): alternatives, imports, penalised surplus, shadow prices."""

from collections.abc import Mapping, Sequence
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import Bounds, LinearConstraint, linprog, milp
from scipy.sparse import csr_matrix

from .schema import ConstraintState, Infeasibility, LimitRow, Line, Mode, Solution, Weights

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
        self.integer: set[int] = set()

    def col(self, name: str, cost: float = 0.0, lb: float = 0.0, ub: float | None = None, integer: bool = False) -> int:
        if name in self.index:
            raise ValueError(f'Duplicate LP column {name}')
        if integer:
            self.integer.add(len(self.cols))
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

    def solve_milp(self, cost: Sequence[float] | None = None, time_limit: float = 10.0) -> Any:
        c, A_eq, b_eq, A_ub, b_ub = self.arrays()
        if cost is not None:
            c = np.array(cost, dtype=float)
        constraints = []
        if self.eq_names:
            constraints.append(LinearConstraint(A_eq, b_eq, b_eq))
        if self.ub_names:
            constraints.append(LinearConstraint(A_ub, -np.inf, b_ub))
        bounds = Bounds([b[0] for b in self.bounds], [np.inf if b[1] is None else b[1] for b in self.bounds])
        integrality = np.array([1 if j in self.integer else 0 for j in range(len(self.cols))])
        return milp(
            c,
            constraints=constraints,
            integrality=integrality,
            bounds=bounds,
            options={'time_limit': time_limit, 'mip_rel_gap': 1e-4},
        )

    def eq_residual(self, x: NDArray[np.float64]) -> float:
        _, A_eq, b_eq, _, _ = self.arrays()
        return float(np.max(np.abs(A_eq @ x - b_eq))) if len(b_eq) else 0.0


HIGHS_INFEASIBLE = 2
HIGHS_UNBOUNDED = 3
# scipy.optimize.milp: 1 = time or iteration limit (x is set when a feasible point was found),
# 4 = HiGHS could not tell infeasible from unbounded.
MILP_LIMIT = 1
MILP_INFEASIBLE_OR_UNBOUNDED = 4
# Floor for relative slack costs, so a zero limit still has a finite violation cost.
EPS_LIMIT = 1e-9
ELASTIC_NOTE = (
    'The elastic LP minimises the total relative violation, so these shortfalls are one combination of '
    'relaxations, not independent maxima; use mode=maximize to explore one target or limit at a time.'
)


def solve_lp(
    lines: Sequence[Line],
    targets: Mapping[str, float],
    imports: set[str],
    forbid_imports: set[str],
    import_costs: Mapping[str, float],
    allow_surplus: bool,
    surplus_items: set[str],
    weights: Weights,
    mode: Mode = 'targets',
    limits: Sequence[LimitRow] = (),
    consume: Mapping[str, float] | None = None,
    integer: bool = False,
    time_limit: float = 10.0,
) -> Solution:
    """mode='targets' meets targets at minimum cost; mode='maximize' reads targets as ratios and first
    maximises the scale s (output = s x ratio), then minimises cost with s held at its maximum.
    consume: external supplies the lines must use up exactly; those items get no import or surplus.
    An infeasible problem with limits or consume is re-solved elastically to report what to relax.
    integer: whole machine counts n_l >= crafts / crafts_per_machine carry the machine cost and the machine,
    power, pollution, beacon and module limits (a MILP, so no duals and no elastic diagnosis)."""
    consume = consume or {}
    maximize = mode == 'maximize'
    caps = {r.item: r for r in limits if r.item is not None}
    rows_limits = [r for r in limits if r.item is None]
    for k in caps:
        if k in targets:
            raise ValueError(f'limits.imports caps {k}, which is a target; targets are never imported')
    for k in consume:
        if k in caps:
            raise ValueError(f'{k} is both consumed and import-capped; consumed items are never imported')
    keys = sorted({k for r in lines for k in r.balance} | set(targets) | set(consume))
    produced = {k for r in lines for k, v in r.balance.items() if v > 0}
    importable = [
        k
        for k in keys
        if (k not in produced or k in imports or k in caps)
        and k not in forbid_imports
        and k not in targets
        and k not in consume
    ]
    surplus_keys = [k for k in keys if (allow_surplus or k in surplus_items or k in targets) and k not in consume]

    def build(elastic: bool, whole: bool = False) -> LPBuilder:
        """elastic=True gives every limit, target and consume amount a slack priced by its relative size;
        line bounds (fixed_machines, max_machines) stay hard because the user declared them."""
        lp = LPBuilder()
        # The relaxed plan should still be a sensible one, so the original cost stays as a tie-breaker.
        base = 1e-6 if elastic else 1.0
        rows: dict[str, dict[int, float]] = {k: {} for k in keys}
        for j, r in enumerate(lines):
            ub: float | None
            if r.fixed_machines is not None:
                lb = ub = r.fixed_machines * r.crafts_per_machine
            else:
                lb, ub = 0, None if r.max_machines is None else r.max_machines * r.crafts_per_machine
            # Line variables are crafts per second, so per-machine costs scale by 1 / crafts_per_machine.
            machine_cost = r.cost_weight * weights.machines
            power_cost = r.cost_weight * weights.power_MW * r.power_W / 1e6
            if whole:
                c = lp.col(f'x:{j}', base * power_cost / r.crafts_per_machine, lb, ub)
                n = lp.col(f'n:{j}', base * machine_cost, integer=True)
                lp.add_ub(f'whole:{j}', {c: 1, n: -r.crafts_per_machine}, 0)
            else:
                c = lp.col(f'x:{j}', base * (machine_cost + power_cost) / r.crafts_per_machine, lb, ub)
            for k, v in r.balance.items():
                rows[k][c] = v
        for k in importable:
            cap = caps[k].limit if k in caps else None
            m = lp.col(f'm:{k}', base * weights.imports * import_costs.get(k, 1.0), ub=None if elastic else cap)
            rows[k][m] = 1
            if elastic and cap is not None:
                slack = lp.col(f'slack:{caps[k].name}', 1 / max(cap, EPS_LIMIT))
                lp.add_ub(caps[k].name, {m: 1, slack: -1}, cap)
        for k in surplus_keys:
            rows[k][lp.col(f'u:{k}', base * weights.surplus)] = -1
        if maximize:
            s_col = lp.col('s')
            for k, v in targets.items():
                rows[k][s_col] = -v
        if elastic:
            if not maximize:
                for k, v in targets.items():
                    if v > 0:
                        rows[k][lp.col(f'slack:target:{k}', 1 / v, ub=v)] = 1
            for k, v in consume.items():
                rows[k][lp.col(f'slack:consume:{k}', 1 / v, ub=v)] = -1
        for k in keys:
            lp.add_eq(k, rows[k], (0.0 if maximize else targets.get(k, 0.0)) - consume.get(k, 0.0))
        for row in rows_limits:
            if whole:
                coeffs = {lp.index[f'n:{j}']: v * lines[j].crafts_per_machine for j, v in enumerate(row.coef) if v}
            else:
                coeffs = {lp.index[f'x:{j}']: v for j, v in enumerate(row.coef) if v}
            if elastic:
                coeffs[lp.col(f'slack:{row.name}', 1 / max(abs(row.limit), EPS_LIMIT))] = -1
            lp.add_ub(row.name, coeffs, row.limit)
        return lp

    lp = build(elastic=False, whole=integer)
    warnings: list[str] = []

    def run(cost: Sequence[float] | None = None) -> Any:
        return lp.solve_milp(cost, time_limit) if integer else lp.solve(cost)

    def found(res: Any) -> bool:
        return bool(res.status == 0 or (integer and res.status == MILP_LIMIT and res.x is not None))

    def unbounded(res: Any) -> bool:
        if res.status == HIGHS_UNBOUNDED:
            return True
        if integer and res.status == MILP_INFEASIBLE_OR_UNBOUNDED:
            relaxed = build(elastic=False)
            s = relaxed.index['s']
            return bool(relaxed.solve([-1.0 if j == s else 0.0 for j in range(len(relaxed.cols))]).status == 3)
        return False

    if maximize:
        s_col = lp.index['s']
        res_scale = run([-1.0 if j == s_col else 0.0 for j in range(len(lp.cols))])
        if unbounded(res_scale):
            raise ValueError(
                'Maximize is unbounded: nothing limits the scale. Add limits.imports, power_MW, machines, '
                'machines_by_type or pollution_per_minute, consume, or fix a line with fixed_machines/max_machines.'
            )
        res = res_scale
        if found(res_scale):
            lp.add_ub('scale_floor', {s_col: -1.0}, -float(res_scale.x[s_col]) * (1 - 1e-9))
            res = run()
            if not found(res):
                warnings.append(
                    f'Cost minimisation at the maximum scale failed ({res.message}); returning the scale-only solution'
                )
                res = res_scale
        duals = res_scale
    else:
        res = duals = run()
    if not found(duals):
        if integer:
            raise ValueError(
                f'Integer machine counts infeasible ({duals.message}); run without integer_machines '
                'for the elastic diagnosis of limits and consume amounts.'
            )
        if duals.status == HIGHS_INFEASIBLE and (limits or consume):
            return elastic_solution(build(elastic=True), lines, importable, surplus_keys, targets, consume, limits)
        missing = sorted(k for k in targets if k not in produced and k not in importable)
        hint = (
            f'; no producing line for {missing}'
            if missing
            else '; check limits, consume, forbid_imports, fixed_machines/max_machines or allow_surplus'
        )
        raise ValueError(f'LP infeasible: {duals.message}{hint}')
    x = res.x
    counts = [round(float(x[lp.index[f'n:{j}']])) for j in range(len(lines))] if integer else None
    states: list[ConstraintState] = []
    ineq = duals.ineqlin.marginals if lp.ub_names and not integer else None
    for i, row in enumerate(rows_limits):
        if counts is not None:
            used = sum(v * lines[j].crafts_per_machine * counts[j] for j, v in enumerate(row.coef))
        else:
            used = sum(v * x[lp.index[f'x:{j}']] for j, v in enumerate(row.coef))
        states.append(
            ConstraintState(
                name=row.name,
                limit=row.limit,
                used=used,
                marginal=None if ineq is None else -float(ineq[i]),
                rate=row.rate,
                unit=row.unit,
            )
        )
    for k, cap_row in caps.items():
        col = lp.index.get(f'm:{k}')
        used = float(x[col]) if col is not None else 0.0
        marginal = None if integer else -float(duals.upper.marginals[col]) if col is not None else 0.0
        states.append(
            ConstraintState(
                name=cap_row.name, limit=cap_row.limit, used=used, marginal=marginal, rate=True, unit=cap_row.unit
            )
        )
    prices = dict(zip(keys, res.eqlin.marginals, strict=True)) if getattr(res, 'eqlin', None) is not None else {}
    timed_out = integer and res.status == MILP_LIMIT
    return Solution(
        x=[float(x[lp.index[f'x:{j}']]) for j in range(len(lines))],
        counts=counts,
        mip_gap=float(res.mip_gap) if integer and res.mip_gap is not None else None,
        imports={k: float(x[lp.index[f'm:{k}']]) for k in importable},
        surplus={k: float(x[lp.index[f'u:{k}']]) for k in surplus_keys},
        residual=lp.eq_residual(x),
        objective=float(np.array(lp.cost) @ x),
        prices={k: float(v) for k, v in prices.items()},
        status='time_limit' if timed_out else res.message,
        scale=float(x[lp.index['s']]) if maximize else None,
        constraints=states,
        warnings=warnings,
    )


def elastic_solution(
    lp: LPBuilder,
    lines: Sequence[Line],
    importable: Sequence[str],
    surplus_keys: Sequence[str],
    targets: Mapping[str, float],
    consume: Mapping[str, float],
    limits: Sequence[LimitRow],
) -> Solution:
    res = lp.solve()
    if not res.success:
        bounded = [r.id for r in lines if r.fixed_machines is not None or r.max_machines is not None]
        where = f'check fixed_machines/max_machines on {bounded}' if bounded else 'no line has fixed bounds'
        raise ValueError(
            f'LP infeasible even with every limit, target and consume amount relaxed ({res.message}); '
            f'{where}, forbid_imports and allow_surplus.'
        )
    x = res.x
    found: list[Infeasibility] = []
    for row in limits:
        slack = float(x[lp.index[f'slack:{row.name}']]) if f'slack:{row.name}' in lp.index else 0.0
        if slack > 1e-9 * max(1.0, abs(row.limit)):
            found.append(
                Infeasibility(
                    kind='limit',
                    name=row.name,
                    requested=row.limit,
                    achievable=row.limit + slack,
                    shortfall=slack,
                    relative=slack / row.limit if row.limit > 0 else None,
                    rate=row.rate,
                    unit=row.unit,
                )
            )
    amounts: tuple[tuple[Literal['target', 'consume'], Mapping[str, float]], ...] = (
        ('target', targets),
        ('consume', consume),
    )
    for kind, given in amounts:
        for k, v in given.items():
            col = lp.index.get(f'slack:{kind}:{k}')
            slack = float(x[col]) if col is not None else 0.0
            if slack > 1e-9 * max(1.0, v):
                found.append(
                    Infeasibility(
                        kind=kind,
                        name=k,
                        requested=v,
                        achievable=v - slack,
                        shortfall=slack,
                        relative=slack / v,
                        rate=True,
                        unit='unit',
                    )
                )
    return Solution(
        x=[float(x[lp.index[f'x:{j}']]) for j in range(len(lines))],
        imports={k: float(x[lp.index[f'm:{k}']]) for k in importable},
        surplus={k: float(x[lp.index[f'u:{k}']]) for k in surplus_keys},
        residual=lp.eq_residual(x),
        status='infeasible',
        scale=float(x[lp.index['s']]) if 's' in lp.index else None,
        infeasibility=found,
    )
