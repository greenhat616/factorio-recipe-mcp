"""Surplus disposal accounting: void machines (chimneys, outfalls) needed for each byproduct.

Pure post-processing: the LP route choice never sees disposal costs."""

from collections import defaultdict
from collections.abc import Mapping

from ..models import entries
from .model import EPS, Planner
from .report import add_line, plan_line
from .schema import Defaults, Line, LineSpec, PlanLine, Totals

# Energy sinks void a power fluid, not a byproduct.
NOT_DISPOSAL_CATEGORIES = {'nullius-power-sink'}


def void_recipes(p: Planner) -> dict[str, list[str]]:
    """Item key -> recipes that take exactly that one input and yield nothing (all results expected 0)."""
    out: defaultdict[str, list[str]] = defaultdict(list)
    for name, r in p.db.recipes.items():
        if r.get('category') in NOT_DISPOSAL_CATEGORIES or p.db.virtual(name):
            continue
        ingredients = entries(r.get('ingredients'))
        if len(ingredients) != 1:
            continue
        results = entries(r.get('results'))
        if all(e.get('probability', 1) == 0 or (e.get('amount', 1) or 0) == 0 for e in results):
            e = ingredients[0]
            out[f'{e.get("type", "item")}:{e["name"]}'].append(name)
    return out


def disposal_defaults(defaults: Defaults | Mapping[str, object] | None) -> Defaults:
    """Disposal machines default to the most power-efficient one rather than the fastest."""
    d = defaults if isinstance(defaults, Defaults) else Defaults.model_validate(defaults or {})
    if 'machine_preference' not in d.model_fields_set:
        d = d.model_copy(update={'machine_preference': 'efficient'})
    return d


def dispose(
    p: Planner, surplus: Mapping[str, float], defaults: Defaults, factor: float
) -> tuple[list[PlanLine], Totals, list[str]]:
    """surplus in internal per-second units; returns one row per handled item, their totals and unhandled items."""
    index = void_recipes(p)
    rows: list[PlanLine] = []
    total = Totals()
    unhandled: list[str] = []
    for k, rate in sorted(surplus.items()):
        if rate <= EPS or k.startswith('energy:'):
            continue
        best: tuple[float, Line] | None = None
        for name in index.get(k, []):
            if p.validate and p.db.availability(name, p.force).usable_at_stage is not True:
                continue
            try:
                line = p.line(LineSpec(recipe=name, id=f'dispose:{k}'), defaults)
            except ValueError:
                continue
            if p.validate and line.blocked:
                continue
            crafts = rate / -line.balance[k]
            machines = crafts / line.crafts_per_machine
            if best is None or machines < best[0]:
                best = (machines, line)
        if best is None:
            unhandled.append(k)
            continue
        line = best[1]
        row = plan_line(line, rate / -line.balance[k], factor)
        rows.append(row)
        add_line(total, row)
    return rows, total, unhandled
