"""Convert Helmod 2.x models to plans and plans to Helmod models.

Helmod nests blocks: a linked child block is scaled by its parent's solver to
supply what the parent (and the siblings before it) consume. Plans are flat, so
each Helmod block with recipes becomes one plan block, and a linked child gets
targets that refer to the imports of those blocks. Going back, links are frozen
to the amounts the plan currently resolves to, since Helmod cannot express them.
"""

import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ..database import Database
from ..paths import DATA_DIR
from ..planner.energy import ENERGY_KEYS, PREFIXES
from ..planner.model import TIME, Planner
from ..planner.schema import Line, LineSpec, Per
from ..plans.factory import solve_plan
from ..plans.models import BlockInput, BlockRequest, ConsumeRef, TargetRef
from ..plans.ops import line_id
from ..plans.store import PlanStore
from .codec import Lua, decode, encode

PER_BY_TIME = {1: 'second', 60: 'minute', 3600: 'hour'}
HELMOD_VERSION = 2
# Relative exchange file paths: the server's working directory depends on the MCP client.
EXCHANGE_DIR = DATA_DIR / 'helmod'
# Helmod's energy products, in joules per craft of a one-second recipe, i.e. watts.
ENERGY = {'energy': 'energy:electric', 'steam-heat': 'energy:heat'}
# Import cost multiplier for an intermediate a block may also buy (balanced objective: 1e4 per unit/s).
SHORTFALL_COST = 1e6
# Request fields Helmod has no counterpart for; exporting a block that sets one drops it with a warning.
NOT_EXPORTED = (
    'imports',
    'forbid_imports',
    'import_costs',
    'surplus_items',
    'objective',
    'weights',
    'mode',
    'limits',
    'disposal',
    'disposal_defaults',
    'integer_machines',
    'copies',
    'energy_mode',
    'allow_surplus',
)


class HelmodImport(BaseModel):
    plan: str
    saved: bool = Field(description='False for dry_run')
    per: Per
    blocks: list[BlockInput]
    helmod_machines: dict[str, dict[str, float]] = Field(
        description="Helmod's own machine counts per block and line in the export, for comparison with plan_solve"
    )
    warnings: list[str] = []


class HelmodExport(BaseModel):
    plan: str
    text: str | None = Field(
        description='Paste into the Helmod "Download Production line" dialog; it adds a new model. '
        'Omitted when written to path'
    )
    path: str | None = Field(None, description='File the string was written to')
    time: int = Field(description='Helmod base time in seconds; amounts are per this time')
    blocks: list[str]
    warnings: list[str] = []


def lua_list(v: Lua) -> list[Lua]:
    """Lua arrays parse as {1: x, 2: y}; Helmod's beacon/module lists may also be empty tables."""
    if isinstance(v, dict):
        return [v[k] for k in sorted(k for k in v if isinstance(k, int))]
    return list(v or [])


def ordered(block: dict[str, Lua]) -> list[dict[str, Lua]]:
    return sorted((block.get('children') or {}).values(), key=lambda c: c.get('index', 0))


def is_block(child: dict[str, Lua]) -> bool:
    return child.get('class') == 'Block' or 'children' in child


def modules_of(element: dict[str, Lua], where: str, warnings: list[str]) -> list[str]:
    raw = element.get('modules') or {}
    if raw and all(isinstance(v, (int, float)) for v in raw.values()):  # Helmod 1.x: {name = count}
        return [n for n, c in raw.items() for _ in range(int(c))]
    out: list[str] = []
    for m in lua_list(raw):
        if m.get('quality', 'normal') != 'normal':
            warnings.append(f'{where}: module {m["name"]} quality {m["quality"]} imported as normal')
        out += [m['name']] * int(m.get('amount', 1))
    return out


class Importer:
    def __init__(
        self, db: Database, model: dict[str, Lua], validate_stage: bool, mining_productivity: float | None
    ) -> None:
        self.db, self.validate_stage = db, validate_stage
        self.mining_productivity = mining_productivity
        # Mining productivity research per block, read back from the effects Helmod stored on its mining lines.
        self.mining: dict[str, list[float]] = {}
        self.p = Planner(db, validate_stage=False)
        self.energy = Planner(db, validate_stage=False, energy_mode='balance')
        self.time = model.get('time') or 1
        self.per: Per = PER_BY_TIME.get(self.time, 'second')  # type: ignore[assignment]
        self.scale = TIME[self.per] / self.time
        self.warnings: list[str] = []
        self.problems: list[str] = []
        self.blocks: list[BlockInput] = []
        self.machines: dict[str, dict[str, float]] = {}
        # Per plan block: item keys it imports (children may supply them) and leaves over (children may use them).
        self.imports: dict[str, set[str]] = {}
        self.leftovers: dict[str, set[str]] = {}
        # The LP form of each block given a matrix request, for when the matrix does not reproduce Helmod.
        self.lp: dict[str, BlockRequest] = {}

    def key(self, entry: dict[str, Lua], where: str) -> str | None:
        kind, name = entry.get('type', 'item'), entry['name']
        if kind == 'energy' and name in ENERGY:
            return ENERGY[name]
        if kind not in ('item', 'fluid'):
            self.warnings.append(f'{where}: {kind} {name} has no plan equivalent; skipped')
            return None
        if any(entry.get(t) is not None for t in ('temperature', 'minimum_temperature', 'maximum_temperature')):
            self.warnings.append(f'{where}: temperature of {name} not imported')
        return f'{kind}:{name}'

    def rate(self, key: str, per_time: float) -> float:
        """A Helmod amount per base time as a plan rate: per plan period, or MW for energy (Helmod uses J)."""
        return per_time / self.time / 1e6 if key in ENERGY_KEYS else per_time * self.scale

    def amounts(self, entries: dict[str, Lua], field: str, where: str, main: bool = False) -> dict[str, float]:
        """Typed item key -> plan rate; main keeps Helmod's driving elements (state 1) only."""
        out: dict[str, float] = {}
        for e in entries.values():
            v = e.get(field)
            if v is None or v <= 1e-9 or (main and e.get('state') != 1):
                continue
            k = self.key(e, where)
            if k is not None:
                out[k] = self.rate(k, v)
        return out

    def name(self, key: str) -> str:
        """The plain name where it is unambiguous, which keeps plan files readable."""
        kind, _, name = key.partition(':')
        if kind == 'energy':
            return key
        try:
            return name if self.p.key(name) == key else key
        except ValueError:
            return key

    def block_id(self, b: dict[str, Lua]) -> str:
        base = re.sub(r'[^A-Za-z0-9_.-]', '-', str(b.get('name') or b.get('id')))[:56] or 'block'
        ids = {x.id for x in self.blocks}
        bid, n = base, 2
        while bid in ids:
            bid, n = f'{base}-{n}', n + 1
        return bid

    def line(self, c: dict[str, Lua], bid: str, by_factory: bool, used: set[str]) -> tuple[LineSpec, Line] | None:
        kind, name = c.get('type', 'recipe'), c['name']
        where = f'{bid}/{name}'
        energy = kind == 'energy'
        if kind == 'recipe':
            recipe = name
        elif kind == 'resource':
            recipe = f'mining:{name}'
        elif energy:
            prefix = next(
                (x for x, kinds in PREFIXES.items() if any(name in self.db.raw.get(k, {}) for k in kinds)), None
            )
            if prefix is None:
                self.problems.append(f'{where}: energy entity {name} is not a generator, solar panel, reactor or wind')
                return None
            recipe = f'{prefix}:{name}'
        else:
            self.warnings.append(f'{where}: Helmod {kind} recipes have no plan equivalent; line skipped')
            return None
        lid, n = recipe, 2
        while lid in used:
            lid, n = f'{recipe}#{n}', n + 1
        used.add(lid)
        f = c.get('factory') or {}
        spec: dict[str, Any] = {'recipe': recipe}
        if lid != recipe:
            spec['id'] = lid
        if not energy:
            spec['machine'] = f.get('name')
            spec['modules'] = modules_of(f, where, self.warnings)
            beacons = []
            for be in lua_list(c.get('beacons')):
                bmods = modules_of(be, f'{where} beacon', self.warnings)
                if not bmods:  # Helmod gives a beacon without modules no effect and no count
                    continue
                combo = be.get('combo') or 1
                count = max(1, round(combo))
                if count != combo:
                    self.warnings.append(
                        f'{where}: beacon {be["name"]} affects {combo} per machine; rounded to {count}'
                    )
                if be.get('per_factory_constant'):
                    self.warnings.append(f'{where}: {be["per_factory_constant"]} extra beacons per block not imported')
                beacons.append(
                    {'beacon': be['name'], 'count': count, 'modules': bmods, 'per_machine': be.get('per_factory')}
                )
            spec['beacons'] = beacons
        if isinstance(f.get('fuel'), str):
            spec['fuel'] = f['fuel']
        if f.get('neighbour_bonus'):
            spec['neighbours'] = int(f['neighbour_bonus'])
        if by_factory and f.get('input'):
            spec['fixed_machines'] = f['input']
        if f.get('limit'):
            self.warnings.append(f'{where}: Helmod display limit of {f["limit"]} machines not imported')
        if c.get('quality', 'normal') != 'normal' or f.get('quality', 'normal') != 'normal':
            self.warnings.append(f'{where}: quality not imported')
        if c.get('contraints'):
            self.warnings.append(f'{where}: Helmod product constraints not imported')
        try:
            line = LineSpec.model_validate(spec)
            row = (self.energy if energy else self.p).line(line)
        except (ValueError, ValidationError) as e:
            self.problems.append(f'{where}: {e}')
            return None
        count = f.get('count_deep', f.get('count'))
        if isinstance(count, (int, float)):
            self.machines[bid][row.id] = count
        helmod_productivity = (f.get('effects') or {}).get('productivity')
        if kind == 'resource' and isinstance(helmod_productivity, (int, float)):
            self.mining.setdefault(bid, []).append(helmod_productivity - row.productivity)
        return line, row

    def mining_bonus(self, bid: str) -> float:
        if self.mining_productivity is not None:
            return self.mining_productivity
        found = self.mining.get(bid, [])
        if not found:
            self.warnings.append(f'{bid}: mining productivity unknown; pass mining_productivity')
            return 0.0
        if max(found) - min(found) > 1e-6:
            self.warnings.append(f'{bid}: Helmod mining productivity differs by resource {found}; using the largest')
        return max(0.0, round(max(found), 9))

    def visit(
        self, b: dict[str, Lua], sources: list[str], parent_by_product: bool, root: bool, demand: dict[str, float]
    ) -> list[str]:
        """Convert b and its subtree; return the plan block ids that stand for b towards its siblings.

        demand holds the amounts set on recipe-less ancestor blocks, which only their descendants can
        meet; the first linked descendant whose own lines make (or use) an item takes it."""
        by_product = b.get('by_product') is not False
        bid = None
        recipes = [c for c in ordered(b) if not is_block(c)]
        if not recipes:
            own = self.amounts(b.get('products' if by_product else 'ingredients') or {}, 'input', str(b.get('id')))
            demand = {**demand, **own}
        else:
            bid = self.block_id(b)
            self.machines[bid] = {}
            used: set[str] = set()
            converted = [(c, x) for c in recipes if (x := self.line(c, bid, b.get('by_factory') is True, used))]
            rows = [row for _, (_, row) in converted]
            for flag in ('by_limit', 'consumer'):
                if b.get(flag):
                    self.warnings.append(f'{bid}: Helmod {flag} not imported')
            if b.get('contraints'):
                self.warnings.append(f'{bid}: Helmod block constraints not imported')
            if b.get('solver') is not True and any(c.get('production', 1) != 1 for c in recipes):
                self.warnings.append(f'{bid}: Helmod production share below 100% not imported')
            # Helmod's result for this block's own lines, per base time; block totals also count child blocks.
            net: dict[str, float] = {}
            for c, (_, row) in converted:
                for k, v in row.balance.items():
                    net[k] = net.get(k, 0) + v * (c.get('count') or 0) * (b.get('count') or 1)
            request: dict[str, Any] = {
                'lines': [spec for _, (spec, _) in converted],
                'auto_discover': False,
                'validate_stage': self.validate_stage,
            }
            if any(r.recipe.startswith('mining:') for r in rows):
                request['mining_productivity'] = self.mining_bonus(bid)
            pivots = {pk for c in recipes if c.get('pivot') and (pk := self.key(c['pivot'], bid))}
            linked = not root and b.get('unlinked') is not True
            if b.get('by_factory') is not True:
                self.goals(
                    b,
                    bid,
                    request,
                    sources if linked else [],
                    parent_by_product,
                    by_product,
                    demand if linked else {},
                    net,
                    pivots,
                )
            self.supply(b, bid, request, rows, by_product, pivots)
        own_ids = [bid] if bid else sources
        earlier: list[str] = []
        for c in ordered(b):
            if is_block(c):
                earlier += self.visit(c, own_ids + earlier, by_product, False, demand)
        return [bid] if bid else earlier

    def goals(
        self,
        b: dict[str, Lua],
        bid: str,
        request: dict[str, Any],
        sources: list[str],
        parent_by_product: bool,
        by_product: bool,
        demand: dict[str, float],
        net: dict[str, float],
        pivots: set[str],
    ) -> None:
        if sources or demand:
            elements = b.get('products' if parent_by_product else 'ingredients') or {}
            ref = TargetRef if parent_by_product else ConsumeRef
            pool = self.imports if parent_by_product else self.leftovers
            sign = 1 if parent_by_product else -1
            goal: dict[str, Any] = {}
            for k in self.amounts(elements, 'amount', bid, main=True):
                if k in demand and sign * net.get(k, 0) > 0:
                    goal[self.name(k)] = demand.pop(k)
                elif refs := [s for s in sources if k in pool[s]]:
                    goal[self.name(k)] = ref.model_validate({'from': refs})
            if goal:
                request['targets' if parent_by_product else 'consume'] = goal
                return
            self.warnings.append(f'{bid}: linked Helmod block shares no item with {sources}; using its own amounts')
        field = 'targets' if by_product else 'consume'
        entries = b.get('products' if by_product else 'ingredients') or {}
        values = self.amounts(entries, 'input', bid)
        if not values:
            sign = 1 if by_product else -1
            # Energy lines' balance is already in MW, unlike Helmod's joules, so take the block amount for it.
            computed = {
                k: v if k in ENERGY_KEYS else self.rate(k, sign * net[k])
                for k, v in self.amounts(entries, 'amount', bid, main=True).items()
                if sign * net.get(k, 0) > 1e-9
            }
            # Without an input Helmod drives the block by its recipes' pivots; other outputs follow from them.
            values = {k: v for k, v in computed.items() if k in pivots} or computed
            if values:
                self.warnings.append(f'{bid}: no Helmod input set; {field} use the amounts Helmod computed')
        request[field] = {self.name(k): v for k, v in values.items()}

    def supply(
        self, b: dict[str, Lua], bid: str, request: dict[str, Any], rows: list[Line], by_product: bool, pivots: set[str]
    ) -> None:
        """Finish the request: what the block may import or leave over, and which solver reproduces Helmod.

        Helmod's default algebra solver gives each recipe one pivot item it must balance and turns any
        other shortfall or excess into a block input or product, without optimising. The matrix solver
        does the same when only the pivots balance. Helmod's simplex blocks, and blocks the matrix cannot
        solve, use the LP over the same lines instead."""
        produced = {k for r in rows for k, v in r.balance.items() if v > 0}
        consumed = {k for r in rows for k, v in r.balance.items() if v < 0}
        targets = {self.p.key(k) for k in request.get('targets', {})}
        supplied = {self.p.key(k) for k in request.get('consume', {})}
        helmod_inputs = set(self.amounts(b.get('ingredients') or {}, 'amount', bid))
        helmod_outputs = set(self.amounts(b.get('products') or {}, 'amount', bid))
        # An item the block makes can still be short; Helmod then lists it as a block input.
        shortfall = (produced & consumed & helmod_inputs) - ENERGY_KEYS - targets - supplied
        imports = (targets - produced) | shortfall
        energy_targets = targets & ENERGY_KEYS
        if energy_targets or any(r.recipe.partition(':')[0] in PREFIXES for r in rows):
            request['energy_mode'] = 'balance'
            # Helmod leaves electric demand out of the balance; importing it keeps that, unless power is the goal.
            imports |= ENERGY_KEYS - energy_targets
            if energy_targets:
                self.warnings.append(f'{bid}: {sorted(energy_targets)} is a net target after the block powers itself')
        self.imports[bid] = (targets - produced) | (consumed - produced - supplied) | shortfall
        self.leftovers[bid] = (produced | supplied) - consumed

        lp = dict(request)
        if imports:
            lp['imports'] = sorted(self.name(k) for k in imports)
        if shortfall:
            # Keeps the LP running the block's own lines before it buys an intermediate.
            lp['import_costs'] = {self.name(k): SHORTFALL_COST for k in sorted(shortfall)}
        if not by_product:
            # In input mode every recipe runs on what the ones before it made: no intermediate is left over.
            lp['allow_surplus'] = False
            lp['surplus_items'] = sorted(self.name(k) for k in (helmod_outputs | (produced - consumed)) - ENERGY_KEYS)
        block = BlockInput(id=bid, description=f'Helmod {b.get("id")} {b.get("name")}', request=lp)
        if pivots and b.get('solver') is not True and b.get('by_factory') is not True:
            free_in = {k for k in imports if k not in pivots and not (k in ENERGY_KEYS and k in produced)}
            free_out = (helmod_outputs & consumed) - targets - pivots - ENERGY_KEYS
            matrix = {**request, 'solver': 'matrix', 'imports': sorted(self.name(k) for k in free_in)}
            matrix['surplus_items'] = sorted(self.name(k) for k in free_out)
            self.lp[bid] = block.request
            block = block.model_copy(update={'request': BlockRequest.model_validate(matrix)})
        self.blocks.append(block)


def exchange_path(path: str) -> Path:
    return (EXCHANGE_DIR / Path(path).expanduser()).resolve()


def import_helmod(
    store: PlanStore,
    text: str,
    name: str,
    description: str = '',
    force: str = '',
    overwrite: bool = False,
    dry_run: bool = False,
    validate_stage: bool = True,
    mining_productivity: float | None = None,
    path: str = '',
) -> HelmodImport:
    if bool(text) == bool(path):
        raise ValueError('Give either text or path')
    if path:
        source = exchange_path(path)
        if not source.is_file():
            raise ValueError(f'No file {source}')
        text = source.read_text(encoding='utf-8-sig')
    model = decode(text)
    root = model.get('block_root')
    if not isinstance(root, dict):
        raise ValueError('Not a Helmod 2.x model: no block_root (models from Helmod 1.x are not supported)')
    store.path(name)
    imp = Importer(store.db, model, validate_stage, mining_productivity)
    imp.visit(root, [], True, True, {})
    if imp.problems:
        raise ValueError('Cannot import the Helmod model: ' + '; '.join(imp.problems))
    if not imp.blocks:
        raise ValueError('The Helmod model has no recipes')
    if model.get('parameters', {}).get('effects') and any((model['parameters']['effects'] or {}).values()):
        imp.warnings.append('Helmod global effect bonuses not imported')
    description = description or f'Imported from Helmod {model.get("id", "model")}'
    blocks = imp.blocks
    with tempfile.TemporaryDirectory() as tmp:
        trial = PlanStore(Path(tmp), store.db)
        trial.save(name, blocks, description, force, imp.per)
        for o in solve_plan(trial, name, save_results=False).blocks:
            matrix_failed = (o.error or '').startswith('Matrix') or any('Negative unknowns' in w for w in o.warnings)
            if o.id in imp.lp and matrix_failed:
                imp.warnings.append(f'{o.id}: the matrix solver does not reproduce Helmod here; imported for the LP')
                blocks = [b.model_copy(update={'request': imp.lp[b.id]}) if b.id == o.id else b for b in blocks]
    if not dry_run:
        store.save(name, blocks, description, force, imp.per, overwrite)
    elif store.exists(name) and not overwrite:
        imp.warnings.append(f'Plan {name} exists; saving needs overwrite=true')
    return HelmodImport(
        plan=name,
        saved=not dry_run,
        per=imp.per,
        blocks=blocks,
        helmod_machines=imp.machines,
        warnings=imp.warnings,
    )


def recipe_of(row: Line) -> tuple[str, str]:
    """(Helmod recipe type, name) for a plan line."""
    prefix, _, rest = row.recipe.partition(':')
    if prefix == 'mining':
        return 'resource', rest
    if prefix in PREFIXES:
        return 'energy', rest.split('@', 1)[0]
    return 'recipe', row.recipe


def module_table(names: Sequence[str]) -> list[dict[str, Lua]]:
    counts: dict[str, int] = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    return [{'name': n, 'quality': 'normal', 'amount': c} for n, c in counts.items()]


def element(key: str, rate: float, time: int) -> tuple[str, dict[str, Lua]]:
    """A plan rate as a Helmod block element: base time equals the plan period, and energy is in joules."""
    if key in ENERGY_KEYS:
        name = next(n for n, k in ENERGY.items() if k == key)
        return name, {'name': name, 'type': 'energy', 'input': rate * 1e6 * time}
    kind, _, name = key.partition(':')
    return name, {'name': name, 'type': kind, 'input': rate}


def export_helmod(
    store: PlanStore, name: str, blocks: Sequence[str] = (), path: str = '', overwrite: bool = False
) -> HelmodExport:
    plan = store.read(name)
    wanted = list(blocks) or [b.id for b in plan.blocks if b.enabled]
    solved = {o.id: o for o in solve_plan(store, name, wanted, detail='full', save_results=False).blocks}
    time = TIME[plan.per]
    warnings: list[str] = []
    out: list[dict[str, Lua]] = []
    exported: list[str] = []
    for bid in wanted:
        block, outcome = plan.block(bid), solved.get(bid)
        if outcome is None or outcome.result is None:
            warnings.append(f'{bid}: not exported, the block does not solve: {outcome.error if outcome else "?"}')
            continue
        req = block.request
        p = Planner(
            store.db,
            plan.force,
            False,
            req.research_productivity,
            req.mining_productivity,
            req.energy_mode,
            req.solar_factor,
            req.wind_factor,
        )
        specs = list(req.lines) or list(outcome.result.lines_for_matrix)
        rows = [p.line(s, req.defaults) for s in specs]
        fixed = {line_id(s): s.fixed_machines for s in req.lines if isinstance(s, LineSpec) and s.fixed_machines}
        for f in sorted(set(NOT_EXPORTED) & req.model_fields_set):
            if getattr(req, f) != BlockRequest.model_fields[f].default:
                warnings.append(f'{bid}: {f} has no Helmod equivalent; not exported')
        for s in req.lines:
            if isinstance(s, LineSpec) and (s.max_machines is not None or s.temperature is not None):
                warnings.append(f'{bid}/{line_id(s)}: max_machines and temperature not exported')
        if any(isinstance(v, TargetRef) for v in (*req.targets.values(), *req.consume.values())):
            warnings.append(f'{bid}: links to other blocks exported as their current amounts')
        targets = {p.key(k): v for k, v in outcome.resolved_targets.items()}
        consume = {p.key(k): v for k, v in outcome.consume.items()}
        products: dict[str, Lua] = {}
        ingredients: dict[str, Lua] = {}
        for k, v in targets.items():
            n, e = element(k, v, time)
            products[n] = e
        by_product = True
        if consume and not targets:
            by_product = False
            for k, v in consume.items():
                n, e = element(k, v, time)
                ingredients[n] = e
        elif consume:
            warnings.append(f'{bid}: Helmod blocks take either outputs or inputs; consume amounts not exported')
        by_factory = bool(fixed) and not targets and not consume
        if fixed and not by_factory:
            warnings.append(f'{bid}: fixed machine counts with targets not exported; Helmod uses one or the other')
        children: dict[str, Lua] = {}
        for i, row in enumerate(rows):
            kind, rname = recipe_of(row)
            factory: dict[str, Lua] = {
                'class': 'Factory',
                'name': row.machine,
                'type': 'entity',
                'modules': module_table(row.modules),
            }
            if row.fuel:
                factory['fuel'] = row.fuel.partition(':')[2] or row.fuel
            if row.neighbours:
                factory['neighbour_bonus'] = row.neighbours
            if by_factory and row.id in fixed:
                factory['input'] = fixed[row.id]
            beacons = [
                {
                    'class': 'Beacon',
                    'name': b.entity,
                    'type': 'entity',
                    'combo': b.count,
                    'per_factory': b.per_machine,
                    'per_factory_constant': 0,
                    'modules': module_table(b.modules),
                }
                for b in row.beacons
            ]
            rid = f'R{len(out) + 1}_{i + 1}'
            children[rid] = {
                'class': 'Recipe',
                'id': rid,
                'index': i,
                'name': rname,
                'type': kind,
                'production': 1,
                'factory': factory,
                'beacons': beacons,
            }
        if not children:
            warnings.append(f'{bid}: no lines; not exported')
            continue
        first = children[next(iter(children))]
        exported.append(bid)
        out.append(
            {
                'class': 'Block',
                'name': first['name'],
                'type': first['type'],
                'children': children,
                'products': products,
                'ingredients': ingredients,
                'by_product': by_product,
                'by_factory': by_factory,
                'solver': req.solver == 'lp',
                'unlinked': True,
            }
        )
    if not out:
        raise ValueError('Nothing to export: ' + '; '.join(warnings))
    if len(out) == 1:
        root = out[0]
    else:
        for i, b in enumerate(out):
            b['id'], b['index'] = f'block_{i + 2}', i
        root = {'class': 'Block', 'name': out[0]['name'], 'type': out[0]['type'], 'children': {b['id']: b for b in out}}
    root = {**root, 'id': 'block_1', 'index': 0}
    model = {'class': 'Model', 'time': time, 'version': HELMOD_VERSION, 'block_root': root}
    text = encode(model)
    if not path:
        return HelmodExport(plan=name, text=text, time=time, blocks=exported, warnings=warnings)
    target = exchange_path(path)
    if target.exists() and not overwrite:
        raise ValueError(f'{target} exists; pass overwrite=true to replace it')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='ascii')
    return HelmodExport(plan=name, text=None, path=str(target), time=time, blocks=exported, warnings=warnings)
