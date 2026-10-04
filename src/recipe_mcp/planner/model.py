"""Line model: recipe + machine + modules + beacons -> per-craft balance, rates, power, research gates."""

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from ..database import Database
from ..models import JSON, entries
from .schema import (
    BeaconRow,
    Defaults,
    ExcludedCandidate,
    Line,
    LineSpec,
    Machine,
    ModuleSpec,
    RecipeEntry,
    RecipeView,
    TemperatureRange,
    Weights,
)

TIME = {'second': 1, 'minute': 60, 'hour': 3600}
CRAFTING_KINDS = ('assembling-machine', 'furnace', 'rocket-silo')
# Energy generation, voiding, packaging and creative helpers make degenerate or
# meaningless cycles in an auto-discovered plan; pass them explicitly if wanted.
AUTO_EXCLUDED_CATEGORIES = {
    'packaging',
    'nullius-barrel',
    'nullius-unbarrel',
    'parameters',
    'turbine-open',
    'turbine-closed',
    'nullius-power-sink',
    'fuel-depot',
    'nullius-gas-void',
    'nullius-liquid-void',
    'creative-mod_free-fluids',
    'creative-mod_energy-absorption',
}
AUTO_EXCLUDED_NAMES = ('recycling', 'legacy', 'creative')
AUTO_EXCLUDED_MACHINES = ('mirror', '-surge-', 'broken', 'creative')
OBJECTIVES = {
    'balanced': Weights(machines=1, power_MW=0, imports=1e-2, surplus=1e-1),
    'machines': Weights(machines=1, power_MW=0, imports=1e-3, surplus=1e-4),
    'power': Weights(machines=1e-3, power_MW=1, imports=1e-3, surplus=1e-4),
    'imports': Weights(machines=1e-3, power_MW=0, imports=1, surplus=1e-4),
}
EPS = 1e-9
TEMPERATURE_KEYS = ('temperature', 'minimum_temperature', 'maximum_temperature')


def watts(value: str | None) -> float:
    m = re.fullmatch(r'\s*([0-9.eE+-]+)\s*([kMGTP]?)W\s*', str(value or '0W'))
    if not m:
        raise ValueError(f'Unrecognised power value: {value}')
    return float(m.group(1)) * {'': 1, 'k': 1e3, 'M': 1e6, 'G': 1e9, 'T': 1e12, 'P': 1e15}[m.group(2)]


def drain_watts(machine: JSON, kind: str = 'assembling-machine') -> float:
    """Idle electric drain. Crafting machines without a declared drain use energy_usage / 30 (2.0 prototype docs)."""
    src = machine.get('energy_source', {})
    if src.get('type') != 'electric':
        return 0.0
    if 'drain' in src:
        return watts(src['drain'])
    return watts(machine.get('energy_usage')) / 30 if kind in CRAFTING_KINDS else 0.0


def beneficial(effect: str, value: float) -> bool:
    return value < 0 if effect == 'consumption' or effect == 'pollution' else value > 0


def module_list(spec: ModuleSpec | None) -> list[str]:
    if isinstance(spec, dict):
        return [n for n, c in spec.items() for _ in range(c)]
    return list(spec or [])


class Planner:
    def __init__(
        self,
        db: Database,
        force: str = '',
        validate_stage: bool = True,
        research_productivity: bool = True,
        mining_productivity: float = 0.0,
    ) -> None:
        self.db, self.raw = db, db.raw
        self.validate = validate_stage
        self.force: str | None
        if validate_stage:
            self.force = db.require_force(force)
        else:
            self.force = db.force_name(force) if db.progress_compatible and (force or db.default_force) else None
        self.research_productivity = research_productivity
        self.mining_productivity = mining_productivity
        self.warnings: list[str] = []
        self._build_cache: dict[tuple[str | None, str | None], bool | None] = {}
        self.types: defaultdict[str, set[str]] = defaultdict(set)
        for r in db.recipes.values():
            for e in entries(r.get('ingredients')) + entries(r.get('results')):
                self.types[e['name']].add(e.get('type', 'item'))
        self.resources: defaultdict[str, list[str]] = defaultdict(list)
        for n, res in self.raw.get('resource', {}).items():
            if any(s in n for s in AUTO_EXCLUDED_NAMES):
                continue
            for e in self.mining_results(res):
                self.types[e.name].add(e.type)
                self.resources[e.key].append(n)
        self.machines_by_category: defaultdict[str, list[Machine]] = defaultdict(list)
        for kind in CRAFTING_KINDS:
            for n, m in self.raw.get(kind, {}).items():
                for c in m.get('crafting_categories', []):
                    self.machines_by_category[c].append(Machine(kind=kind, name=n, proto=m))
        self.drills_by_category: defaultdict[str, list[Machine]] = defaultdict(list)
        for n, m in self.raw.get('mining-drill', {}).items():
            for c in m.get('resource_categories', []):
                self.drills_by_category[c].append(Machine(kind='mining-drill', name=n, proto=m))

    def key(self, name: str) -> str:
        if ':' in name and name.split(':', 1)[0] in ('item', 'fluid'):
            return name
        types = self.types.get(name) or ({'fluid'} if name in self.raw.get('fluid', {}) else {'item'})
        if len(types) > 1:
            raise ValueError(f'Ambiguous material {name}; use item:{name} or fluid:{name}')
        return next(iter(types)) + ':' + name

    @staticmethod
    def mining_results(res: JSON) -> list[RecipeEntry]:
        minable = res.get('minable', {})
        if 'results' in minable:
            return [RecipeEntry.model_validate(e) for e in entries(minable['results'])]
        if 'result' in minable:
            return [RecipeEntry(name=minable['result'], amount=minable.get('count', 1))]
        return []

    def buildable(self, entity: str | None = None, item: str | None = None) -> bool | None:
        k = (entity, item)
        if k not in self._build_cache:
            s = self.db.machine_stage(entity, self.force) if entity else self.db.item_stage(item or '', self.force)
            self._build_cache[k] = s.buildable_at_stage
        return self._build_cache[k]

    def recipe_view(self, name: str) -> RecipeView:
        if name.startswith('mining:'):
            rname = name.removeprefix('mining:')
            res = self.raw.get('resource', {}).get(rname)
            if res is None:
                raise ValueError(f'Unknown resource: {rname}')
            minable = res.get('minable', {})
            ingredients: list[RecipeEntry] = []
            if minable.get('required_fluid'):
                # fluid_amount is per 10 mining cycles.
                fluid = RecipeEntry(
                    type='fluid', name=minable['required_fluid'], amount=minable.get('fluid_amount', 0) / 10
                )
                ingredients.append(fluid)
            return RecipeView(
                name=name,
                mining=True,
                category=res.get('category', 'basic-solid'),
                energy_required=minable.get('mining_time', 1),
                ingredients=ingredients,
                results=self.mining_results(res),
                allow_productivity=True,
                maximum_productivity=math.inf,
                allow=dict(speed=True, productivity=True, consumption=True, pollution=True, quality=True),
            )
        r = self.db.recipes.get(name)
        if r is None:
            raise ValueError(f'Unknown recipe: {name}')
        return RecipeView(
            name=name,
            mining=False,
            category=r.get('category', 'crafting'),
            energy_required=r.get('energy_required', 0.5),
            ingredients=entries(r.get('ingredients')),
            results=entries(r.get('results')),
            allow_productivity=r.get('allow_productivity', False),
            maximum_productivity=r.get('maximum_productivity', 3.0),
            allow=dict(
                speed=r.get('allow_speed', True),
                productivity=r.get('allow_productivity', False),
                consumption=r.get('allow_consumption', True),
                pollution=r.get('allow_pollution', True),
                quality=r.get('allow_quality', True),
            ),
        )

    def fluid_fit(self, recipe: RecipeView, proto: JSON) -> bool:
        if recipe.mining:
            return True
        need_in = sum(e.type == 'fluid' for e in recipe.ingredients)
        need_out = sum(e.type == 'fluid' for e in recipe.results)
        boxes = proto.get('fluid_boxes') or []
        have_in = sum(b.get('production_type') in ('input', 'input-output') for b in boxes)
        have_out = sum(b.get('production_type') in ('output', 'input-output') for b in boxes)
        return need_in <= have_in and need_out <= have_out

    def candidates(self, recipe: RecipeView) -> list[Machine]:
        pool = self.drills_by_category if recipe.mining else self.machines_by_category
        return [
            m
            for m in pool.get(recipe.category, [])
            if not m.proto.get('fixed_recipe') or m.proto['fixed_recipe'] == recipe.name
        ]

    def pick_machine(
        self, recipe: RecipeView, preference: str = 'fastest', preferred: Iterable[str] = ()
    ) -> Machine | None:
        speed_key = 'mining_speed' if recipe.mining else 'crafting_speed'
        opts = [
            m
            for m in self.candidates(recipe)
            if not any(s in m.name for s in AUTO_EXCLUDED_MACHINES)
            and self.fluid_fit(recipe, m.proto)
            and (not self.validate or self.buildable(entity=m.name) is True)
        ]
        if not opts:
            return None
        for p in preferred:
            hit = next((o for o in opts if o.name == p), None)
            if hit:
                return hit

        def energy(o: Machine) -> float:
            return watts(o.proto.get('energy_usage')) + drain_watts(o.proto, o.kind)

        def speed(o: Machine) -> float:
            return o.proto.get(speed_key, 1)

        if preference == 'slowest':
            return min(opts, key=lambda o: (speed(o), energy(o), o.name))
        if preference == 'efficient':
            return min(opts, key=lambda o: (energy(o) / speed(o), -speed(o), o.name))
        return max(opts, key=lambda o: (speed(o), -energy(o), o.name))

    def module_problems(
        self, module: str, allowed: set[str], recipe_allow: Mapping[str, bool], where: str
    ) -> list[str]:
        proto = self.raw.get('module', {}).get(module)
        if proto is None:
            return [f'{where}: unknown module {module}']
        out: list[str] = []
        for eff, v in proto.get('effect', {}).items():
            if beneficial(eff, v) and eff not in allowed:
                out.append(f'{where}: {module} {eff} not allowed by entity')
            if beneficial(eff, v) and not recipe_allow.get(eff, True):
                out.append(f'{where}: {module} {eff} not allowed by recipe')
        return out

    def line(self, spec: str | LineSpec, defaults: Defaults | None = None) -> Line:
        """Normalise one line spec into its per-craft balance, rates, power and gates."""
        defaults = defaults or Defaults()
        if isinstance(spec, str):
            spec = LineSpec(recipe=spec)
        recipe = self.recipe_view(spec.recipe)
        line_id = spec.id or recipe.name
        problems: list[str] = []
        blocked: list[str] = []
        hit: Machine | None
        if spec.machine:
            hit = next((m for m in self.candidates(recipe) if m.name == spec.machine), None)
            if hit is None:
                raise ValueError(f'{spec.machine} cannot run {recipe.name} (category {recipe.category})')
            if not self.fluid_fit(recipe, hit.proto):
                self.warnings.append(f'{line_id}: {spec.machine} may lack fluid boxes for this recipe')
        else:
            hit = self.pick_machine(recipe, defaults.machine_preference, defaults.machines)
            if hit is None:
                buildable = 'buildable ' if self.validate else ''
                raise ValueError(f'No {buildable}machine for {recipe.name} (category {recipe.category})')
        m = hit.proto
        allowed = set(m.get('allowed_effects', []))
        receiver = m.get('effect_receiver') or {}
        slots = m.get('module_slots', 0) or 0

        modules: list[str] = []
        if spec.modules is not None:
            modules = module_list(spec.modules)
        else:  # default: fill all slots with the first compatible module in priority order
            for mod in defaults.modules:
                if (
                    slots
                    and not self.module_problems(mod, allowed, recipe.allow, 'machine')
                    and (not self.validate or self.buildable(item=mod) is True)
                ):
                    modules = [mod] * slots
                    break
        if len(modules) > slots:
            problems.append(f'{len(modules)} modules exceed {slots} slots of {hit.name}')
        effects: defaultdict[str, float] = defaultdict(float)
        for mod in modules:
            problems += self.module_problems(mod, allowed, recipe.allow, hit.name)
            if receiver.get('uses_module_effects', True):
                for eff, v in self.raw['module'].get(mod, {}).get('effect', {}).items():
                    effects[eff] += v
        beacons = defaults.beacons if spec.beacons is None else spec.beacons
        total = sum(b.count for b in beacons)
        beacon_W = 0.0
        beacon_rows: list[BeaconRow] = []
        for b in beacons:
            bp = self.raw.get('beacon', {}).get(b.beacon)
            if bp is None:
                raise ValueError(f'Unknown beacon: {b.beacon}')
            bmods = module_list(b.modules)
            if len(bmods) > bp.get('module_slots', 0):
                problems.append(f'{b.beacon}: {len(bmods)} modules exceed slots')
            same = sum(x.count for x in beacons if x.beacon == b.beacon)
            n = same if bp.get('beacon_counter') == 'same_type' else total
            profile = bp.get('profile') or [1]
            factor = bp.get('distribution_effectivity', 1) * profile[min(max(n, 1), len(profile)) - 1]
            for mod in bmods:
                problems += self.module_problems(mod, set(bp.get('allowed_effects', [])), recipe.allow, b.beacon)
                if receiver.get('uses_beacon_effects', True):
                    for eff, v in self.raw['module'].get(mod, {}).get('effect', {}).items():
                        effects[eff] += v * factor * b.count
            per_machine = b.count if b.per_machine is None else b.per_machine
            beacon_W += per_machine * watts(bp.get('energy_usage'))
            beacon_rows.append(
                BeaconRow(beacon=b.beacon, count=b.count, modules=bmods, per_machine=per_machine, effect_factor=factor)
            )
            if self.validate:
                if self.buildable(entity=b.beacon) is not True:
                    blocked.append(f'beacon {b.beacon}')
                blocked += [f'module {x}' for x in sorted(set(bmods)) if self.buildable(item=x) is not True]
        for eff, v in (receiver.get('base_effect') or {}).items():
            effects[eff] += v
        research_bonus = 0.0
        if recipe.mining:
            research_bonus = self.mining_productivity
        elif self.force and self.research_productivity:
            research_bonus = self.db.availability(recipe.name, self.force).productivity_bonus
        effects['productivity'] += research_bonus
        if problems and not spec.ignore_module_rules:
            raise ValueError(f'{line_id}: ' + '; '.join(problems))

        speed_mult = max(0.2, 1 + effects['speed'])
        cons_mult = max(0.2, 1 + effects['consumption'])
        poll_mult = max(0.2, 1 + effects['pollution'])
        prod = 0.0
        if recipe.allow_productivity:
            prod = min(max(0.0, effects['productivity']), recipe.maximum_productivity)
        base_speed = m.get('mining_speed' if recipe.mining else 'crafting_speed', 1)
        balance: defaultdict[str, float] = defaultdict(float)
        temps: defaultdict[str, set[float]] = defaultdict(set)
        for e in recipe.ingredients:
            balance[e.key] -= e.average
        for e in recipe.results:
            balance[e.key] += e.output(prod)
            if e.type == 'fluid':
                default = self.raw.get('fluid', {}).get(e.name, {}).get('default_temperature', 15)
                temps[e.key].add(default if e.temperature is None else e.temperature)
        src = m.get('energy_source', {})
        if self.validate:
            if not recipe.mining:
                a = self.db.availability(recipe.name, self.force)
                if a.usable_at_stage is not True:
                    blocked.append(f'recipe {recipe.name} ({a.state})')
            if self.buildable(entity=hit.name) is not True:
                blocked.append(f'machine {hit.name}')
            blocked += [f'module {x}' for x in sorted(set(modules)) if self.buildable(item=x) is not True]
        return Line(
            id=line_id,
            recipe=recipe.name,
            machine=hit.name,
            machine_type=hit.kind,
            modules=modules,
            beacons=beacon_rows,
            speed_multiplier=speed_mult,
            productivity=prod,
            consumption_multiplier=cons_mult,
            pollution_multiplier=poll_mult,
            research_productivity=research_bonus,
            crafts_per_machine=base_speed * speed_mult / recipe.energy_required,
            energy_type=src.get('type', 'void'),
            active_W=watts(m.get('energy_usage')) * cons_mult,
            drain_W=drain_watts(m, hit.kind),
            beacon_W=beacon_W,
            pollution_per_minute=(src.get('emissions_per_minute') or {}).get('pollution', 0) * poll_mult * cons_mult,
            balance={k: v for k, v in balance.items() if abs(v) > 1e-12},
            temperatures={k: sorted(v) for k, v in temps.items()},
            ingredient_temperatures={
                e.key: TemperatureRange.model_validate(e.model_dump(include=set(TEMPERATURE_KEYS)))
                for e in recipe.ingredients
                if e.type == 'fluid' and any(getattr(e, t) is not None for t in TEMPERATURE_KEYS)
            },
            fixed_machines=spec.fixed_machines,
            max_machines=spec.max_machines,
            cost_weight=spec.cost_weight,
            blocked=blocked,
        )

    def auto_ok(self, name: str, include_hidden: bool) -> bool:
        r = self.db.recipes[name]
        return not (
            self.db.virtual(name)
            or r.get('category', 'crafting') in AUTO_EXCLUDED_CATEGORIES
            or any(s in name for s in AUTO_EXCLUDED_NAMES)
            or (r.get('hidden') and not include_hidden)
            or (self.validate and self.db.availability(name, self.force).usable_at_stage is not True)
        )

    def build_lines(
        self,
        lines: Sequence[str | LineSpec],
        targets: Mapping[str, float],
        defaults: Defaults,
        auto: bool,
        imports: set[str],
        exclude: set[str],
        max_depth: int,
        max_lines: int,
        include_hidden: bool,
        allow_mining: bool,
    ) -> tuple[list[Line], list[ExcludedCandidate], bool, list[str]]:
        out: list[Line] = []
        excluded: list[ExcludedCandidate] = []
        seen_ids: defaultdict[str, int] = defaultdict(int)
        for spec in lines:
            row = self.line(spec, defaults)
            seen_ids[row.id] += 1
            if seen_ids[row.id] > 1:
                row.id += f'#{seen_ids[row.id]}'
            if self.validate and row.blocked:
                raise ValueError(
                    f'Stage-locked line {row.id}: '
                    + ', '.join(row.blocked)
                    + '. Use validate_stage=false for theory mode.'
                )
            out.append(row)
        truncated = False
        frontier_left: list[str] = []
        if auto:
            have = {r.recipe for r in out}
            frontier, expanded = list(targets), set(imports)
            for _ in range(max_depth):
                nxt: list[str] = []
                for k in frontier:
                    if k in expanded:
                        continue
                    expanded.add(k)
                    names = [
                        n for n in self.db.producers.get(k.split(':', 1)[1], []) if n not in have and n not in exclude
                    ]
                    if allow_mining:
                        mined = ['mining:' + r for r in self.resources.get(k, [])]
                        names += [n for n in mined if n not in have and n not in exclude]
                    for n in names:
                        if not n.startswith('mining:') and not self.auto_ok(n, include_hidden):
                            continue
                        if len(out) >= max_lines:
                            truncated = True
                            break
                        try:
                            row = self.line(n, defaults)
                        except ValueError as e:
                            excluded.append(ExcludedCandidate(recipe=n, reason=str(e)))
                            have.add(n)
                            continue
                        if self.validate and row.blocked:
                            have.add(n)
                            continue
                        # No net output of k (e.g. k is only a catalyst) says nothing about other
                        # outputs, so leave n eligible for a later frontier item.
                        if row.balance.get(k, 0) <= EPS:
                            continue
                        have.add(n)
                        out.append(row)
                        nxt += [i for i, v in row.balance.items() if v < 0]
                frontier = nxt
            frontier_left = sorted(set(frontier) - expanded)
        return out, excluded, truncated, frontier_left
