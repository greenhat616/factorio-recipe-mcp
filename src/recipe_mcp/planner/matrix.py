"""Exact square-system solver (Factory Planner "matrix solver") and stoichiometric matrix analysis."""
import numpy as np


def recipe_matrix(lines):
    """Items x lines net-per-craft matrix."""
    keys = sorted({k for r in lines for k in r['balance']})
    A = np.zeros((len(keys), len(lines)))
    index = {k: i for i, k in enumerate(keys)}
    for j, r in enumerate(lines):
        for k, v in r['balance'].items(): A[index[k], j] = v
    return keys, A


def analyze_matrix(lines, targets, imports, surplus_items):
    """Factory Planner style square system: unknown roles chosen from item roles."""
    keys = sorted({k for r in lines for k in r['balance']} | set(targets))
    produced = {k for r in lines for k, v in r['balance'].items() if v > 0}
    consumed = {k for r in lines for k, v in r['balance'].items() if v < 0}
    roles, imp, sur = {}, [], []
    for k in keys:
        if k in targets: roles[k] = 'target'
        elif k not in produced: roles[k] = 'raw'
        elif k not in consumed: roles[k] = 'byproduct'
        else: roles[k] = 'intermediate'
        if roles[k] == 'raw' or (k in imports and roles[k] != 'target'): imp.append(k)
        if roles[k] == 'byproduct' or k in surplus_items: sur.append(k)
    free_lines = [j for j, r in enumerate(lines) if r['fixed_machines'] is None]
    fixed_lines = [j for j, r in enumerate(lines) if r['fixed_machines'] is not None]
    index = {k: i for i, k in enumerate(keys)}
    cols = [('line', j) for j in free_lines] + [('import', k) for k in imp] + [('surplus', k) for k in sur]
    M = np.zeros((len(keys), len(cols)))
    b = np.array([targets.get(k, 0.0) for k in keys])
    for c, (kind, ref) in enumerate(cols):
        if kind == 'line':
            for k, v in lines[ref]['balance'].items(): M[index[k], c] = v
        else:
            M[index[ref], c] = 1 if kind == 'import' else -1
    for j in fixed_lines:
        for k, v in lines[j]['balance'].items(): b[index[k]] -= v * lines[j]['fixed_machines'] * lines[j]['crafts_per_machine']
    rank = int(np.linalg.matrix_rank(M)) if M.size else 0
    info = dict(items=len(keys), unknowns=len(cols), rank=rank, degrees_of_freedom=len(cols) - rank,
                roles=roles, unknown_columns=[f'{k}:{lines[r]["id"] if k == "line" else r}' for k, r in cols])
    return keys, cols, M, b, info, fixed_lines

def solve_matrix(lines, targets, imports, surplus_items, warnings):
    keys, cols, M, b, info, fixed_lines = analyze_matrix(lines, targets, imports, surplus_items)
    label = info['unknown_columns']
    if info['degrees_of_freedom'] > 0:
        _, s, vt = np.linalg.svd(M)
        null = vt[info['rank']:]
        directions = [{label[c]: round(float(v), 6) for c, v in enumerate(vec) if abs(v) > 1e-6} for vec in null]
        raise ValueError('Matrix underdetermined: ' + str(info['degrees_of_freedom']) + ' free direction(s). '
                         'Remove alternative recipes, add fixed_machines to a line, or drop surplus_items/imports. '
                         f'Null space: {directions}')
    sol, *_ = np.linalg.lstsq(M, b, rcond=None)
    resid = M @ sol - b
    if np.max(np.abs(resid), initial=0) > 1e-6 * max(1, np.max(np.abs(b), initial=0)):
        bad = {keys[i]: float(v) for i, v in enumerate(resid) if abs(v) > 1e-6}
        raise ValueError('Matrix inconsistent (over-determined): these items cannot balance exactly: ' + str(bad) +
                         '. Mark them as surplus_items or imports, or add a recipe that consumes/produces them.')
    x = np.zeros(len(lines))
    imports_out, surplus_out = {}, {}
    for c, (kind, ref) in enumerate(cols):
        if kind == 'line': x[ref] = sol[c]
        elif kind == 'import': imports_out[ref] = float(sol[c])
        else: surplus_out[ref] = float(sol[c])
    for j in fixed_lines: x[j] = lines[j]['fixed_machines'] * lines[j]['crafts_per_machine']
    negative = [label[c] for c, v in enumerate(sol) if v < -1e-9]
    if negative:
        warnings.append('Negative unknowns (plan not physically realisable as declared): ' + ', '.join(negative) +
                             '. A negative import is an unused surplus; a negative surplus is a deficit; a negative line runs backwards.')
    return dict(x=x.tolist(), imports=imports_out, surplus=surplus_out, residual=float(np.max(np.abs(resid), initial=0)),
                matrix=info, negative=negative)
