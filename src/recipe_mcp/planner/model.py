"""Line model: recipe + machine + modules + beacons -> per-craft balance, rates, power, research gates."""
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..database import JSON, Database

# A normalised production line (see Planner.line) and a candidate machine (prototype kind, name, prototype).
Line = JSON
Machine = tuple[str, str, JSON]


TIME = {'second': 1, 'minute': 60, 'hour': 3600}
CRAFTING_KINDS = ('assembling-machine', 'furnace', 'rocket-silo')
EFFECTS = ('speed', 'productivity', 'consumption', 'pollution', 'quality')
# Energy generation, voiding, packaging and creative helpers make degenerate or
# meaningless cycles in an auto-discovered plan; pass them explicitly if wanted.
AUTO_EXCLUDED_CATEGORIES = {
    'packaging', 'nullius-barrel', 'nullius-unbarrel', 'parameters', 'turbine-open', 'turbine-closed',
    'nullius-power-sink', 'fuel-depot', 'nullius-gas-void', 'nullius-liquid-void',
    'creative-mod_free-fluids', 'creative-mod_energy-absorption'}
AUTO_EXCLUDED_NAMES = ('recycling', 'legacy', 'creative')
AUTO_EXCLUDED_MACHINES = ('mirror', '-surge-', 'broken', 'creative')
OBJECTIVES = {  # weights per machine, per MW, per unit/s imported, per unit/s surplus
    'balanced': {'machines': 1, 'power_MW': 0, 'imports': 1e-2, 'surplus': 1e-1},
    'machines': {'machines': 1, 'power_MW': 0, 'imports': 1e-3, 'surplus': 1e-4},
    'power': {'machines': 1e-3, 'power_MW': 1, 'imports': 1e-3, 'surplus': 1e-4},
    'imports': {'machines': 1e-3, 'power_MW': 0, 'imports': 1, 'surplus': 1e-4},
}
EPS = 1e-9


def watts(value: str | None) -> float:
    m = re.fullmatch(r'\s*([0-9.eE+-]+)\s*([kMGTP]?)W\s*', str(value or '0W'))
    if not m: raise ValueError(f'Unrecognised power value: {value}')
    return float(m.group(1)) * {'': 1, 'k': 1e3, 'M': 1e6, 'G': 1e9, 'T': 1e12, 'P': 1e15}[m.group(2)]


def product_amount(entry: JSON, productivity: float = 0.0) -> float:
    """Expected output per craft; productivity skips ignored_by_productivity."""
    p = entry.get('probability', 1)
    avg = entry['amount'] if 'amount' in entry else (entry.get('amount_min', 0) + entry.get('amount_max', 0)) / 2
    ignored = min(avg, entry.get('ignored_by_productivity', 0))
    return p * avg + entry.get('extra_count_fraction', 0) + p * max(0.0, avg - ignored) * productivity


def entries(value: Any) -> list[JSON]:
    """Lua empty tables export as {}; non-empty arrays as lists."""
    return list(value.values()) if isinstance(value, dict) else list(value or [])


def beneficial(effect: str, value: float) -> bool:
    return value < 0 if effect == 'consumption' or effect == 'pollution' else value > 0


class Planner:
    def __init__(self, db: Database, force: str = '', validate_stage: bool = True, research_productivity: bool = True,
                 mining_productivity: float = 0.0) -> None:
        self.db, self.raw = db, db.raw
        self.validate = validate_stage
        self.force = db.require_force(force) if validate_stage else (db.force_name(force) if db.progress_compatible and (force or db.default_force) else None)
        self.research_productivity = research_productivity
        self.mining_productivity = mining_productivity
        self.warnings: list[str] = []
        self._build_cache: dict[tuple[str | None, str | None], bool | None] = {}
        self.types: defaultdict[str, set[str]] = defaultdict(set)
        for r in db.recipes.values():
            for e in entries(r.get('ingredients')) + entries(r.get('results')): self.types[e['name']].add(e.get('type', 'item'))
        self.resources: defaultdict[str, list[str]] = defaultdict(list)
        for n, res in self.raw.get('resource', {}).items():
            if any(s in n for s in AUTO_EXCLUDED_NAMES): continue
            for e in self.mining_results(res):
                self.types[e['name']].add(e.get('type', 'item'))
                self.resources[e.get('type', 'item') + ':' + e['name']].append(n)
        self.machines_by_category: defaultdict[str, list[Machine]] = defaultdict(list)
        for kind in CRAFTING_KINDS:
            for n, m in self.raw.get(kind, {}).items():
                for c in m.get('crafting_categories', []): self.machines_by_category[c].append((kind, n, m))
        self.drills_by_category: defaultdict[str, list[Machine]] = defaultdict(list)
        for n, m in self.raw.get('mining-drill', {}).items():
            for c in m.get('resource_categories', []): self.drills_by_category[c].append(('mining-drill', n, m))

    # ---------- identifiers ----------
    def key(self, name: str) -> str:
        if ':' in name and name.split(':', 1)[0] in ('item', 'fluid'): return name
        types = self.types.get(name) or ({'fluid'} if name in self.raw.get('fluid', {}) else {'item'})
        if len(types) > 1: raise ValueError(f'Ambiguous material {name}; use item:{name} or fluid:{name}')
        return next(iter(types)) + ':' + name

    @staticmethod
    def mining_results(res: JSON) -> list[JSON]:
        minable = res.get('minable', {})
        if 'results' in minable: return entries(minable['results'])
        if 'result' in minable:
            entry: JSON = {'type': 'item', 'name': minable['result'], 'amount': minable.get('count', 1)}
            if 'amount_min' in res.get('minable', {}): entry.update(amount_min=minable['amount_min'], amount_max=minable['amount_max'])
            return [entry]
        return []

    # ---------- research gates ----------
    def buildable(self, entity: str | None = None, item: str | None = None) -> bool | None:
        k = (entity, item)
        if k not in self._build_cache:
            s = self.db.machine_stage(entity, self.force) if entity else self.db.item_stage(item or '', self.force)
            self._build_cache[k] = s['buildable_at_stage']
        return self._build_cache[k]

    # ---------- line model ----------
    def recipe_view(self, name: str) -> JSON:
        if name.startswith('mining:'):
            rname = name[7:]
            res = self.raw.get('resource', {}).get(rname)
            if res is None: raise ValueError(f'Unknown resource: {rname}')
            minable = res.get('minable', {})
            ingredients: list[JSON] = []
            if minable.get('required_fluid'):
                # fluid_amount is per 10 mining cycles.
                ingredients.append({'type': 'fluid', 'name': minable['required_fluid'], 'amount': minable.get('fluid_amount', 0) / 10})
            return dict(name=name, mining=True, category=res.get('category', 'basic-solid'), energy_required=minable.get('mining_time', 1),
                        ingredients=ingredients, results=self.mining_results(res), allow_productivity=True, maximum_productivity=math.inf,
                        allow=dict(speed=True, productivity=True, consumption=True, pollution=True, quality=True))
        r = self.db.recipes.get(name)
        if r is None: raise ValueError(f'Unknown recipe: {name}')
        return dict(name=name, mining=False, category=r.get('category', 'crafting'), energy_required=r.get('energy_required', .5),
                    ingredients=entries(r.get('ingredients')), results=entries(r.get('results')),
                    allow_productivity=r.get('allow_productivity', False), maximum_productivity=r.get('maximum_productivity', 3.0),
                    allow=dict(speed=r.get('allow_speed', True), productivity=r.get('allow_productivity', False),
                               consumption=r.get('allow_consumption', True), pollution=r.get('allow_pollution', True),
                               quality=r.get('allow_quality', True)))

    def fluid_fit(self, recipe: JSON, proto: JSON) -> bool:
        if recipe['mining']: return True
        need_in = sum(e.get('type') == 'fluid' for e in recipe['ingredients'])
        need_out = sum(e.get('type') == 'fluid' for e in recipe['results'])
        boxes = proto.get('fluid_boxes') or []
        have_in = sum(b.get('production_type') in ('input', 'input-output') for b in boxes)
        have_out = sum(b.get('production_type') in ('output', 'input-output') for b in boxes)
        return need_in <= have_in and need_out <= have_out

    def candidates(self, recipe: JSON) -> list[Machine]:
        pool = self.drills_by_category if recipe['mining'] else self.machines_by_category
        return [(k, n, m) for k, n, m in pool.get(recipe['category'], [])
                if not m.get('fixed_recipe') or m['fixed_recipe'] == recipe['name']]

    def pick_machine(self, recipe: JSON, preference: str = 'fastest', preferred: Iterable[str] = ()) -> Machine | None:
        speed_key = 'mining_speed' if recipe['mining'] else 'crafting_speed'
        opts = [(k, n, m) for k, n, m in self.candidates(recipe)
                if not any(s in n for s in AUTO_EXCLUDED_MACHINES) and self.fluid_fit(recipe, m)
                and (not self.validate or self.buildable(entity=n) is True)]
        if not opts: return None
        for p in preferred:
            hit = next((o for o in opts if o[1] == p), None)
            if hit: return hit
        def energy(m: JSON) -> float: return watts(m.get('energy_usage')) + watts(m.get('energy_source', {}).get('drain', '0W'))
        if preference == 'slowest': return min(opts, key=lambda o: (o[2].get(speed_key, 1), energy(o[2]), o[1]))
        if preference == 'efficient': return min(opts, key=lambda o: (energy(o[2]) / o[2].get(speed_key, 1), -o[2].get(speed_key, 1), o[1]))
        if preference != 'fastest': raise ValueError('machine preference must be fastest, efficient or slowest')
        return max(opts, key=lambda o: (o[2].get(speed_key, 1), -energy(o[2]), o[1]))

    @staticmethod
    def module_list(spec: Mapping[str, int] | Sequence[str] | None) -> list[str]:
        if isinstance(spec, dict): return [n for n, c in spec.items() for _ in range(int(c))]
        return list(spec or [])

    def module_problems(self, module: str, allowed: set[str], recipe_allow: Mapping[str, bool], where: str) -> list[str]:
        proto = self.raw.get('module', {}).get(module)
        if proto is None: return [f'{where}: unknown module {module}']
        out: list[str] = []
        for eff, v in proto.get('effect', {}).items():
            if beneficial(eff, v) and eff not in allowed: out.append(f'{where}: {module} {eff} not allowed by entity')
            if beneficial(eff, v) and not recipe_allow.get(eff, True): out.append(f'{where}: {module} {eff} not allowed by recipe')
        return out

    def line(self, spec: str | JSON, defaults: JSON | None = None) -> Line:
        """Normalise one line spec into its per-craft balance, rates, power and gates."""
        defaults = defaults or {}
        if isinstance(spec, str): spec = {'recipe': spec}
        recipe = self.recipe_view(spec['recipe'])
        problems: list[str] = []
        blocked: list[str] = []
        hit: Machine | None
        if spec.get('machine'):
            name = spec['machine']
            hit = next(((k, n, m) for k, n, m in self.candidates(recipe) if n == name), None)
            if hit is None: raise ValueError(f'{name} cannot run {recipe["name"]} (category {recipe["category"]})')
            if not self.fluid_fit(recipe, hit[2]): self.warnings.append(f'{spec.get("id", recipe["name"])}: {name} may lack fluid boxes for this recipe')
        else:
            hit = self.pick_machine(recipe, defaults.get('machine_preference', 'fastest'), defaults.get('machines', []))
            if hit is None:
                raise ValueError(f'No {"buildable " if self.validate else ""}machine for {recipe["name"]} (category {recipe["category"]})')
        kind, mname, m = hit
        allowed = set(m.get('allowed_effects', []))
        receiver = m.get('effect_receiver') or {}
        slots = m.get('module_slots', 0) or 0

        modules: list[str]
        if 'modules' in spec:
            modules = self.module_list(spec['modules'])
        else:  # default: fill all slots with the first compatible module in priority order
            modules = []
            for mod in defaults.get('modules', []):
                if slots and not self.module_problems(mod, allowed, recipe['allow'], 'machine') and \
                        (not self.validate or self.buildable(item=mod) is True):
                    modules = [mod] * slots
                    break
        if len(modules) > slots: problems.append(f'{len(modules)} modules exceed {slots} slots of {mname}')
        effects: defaultdict[str, float] = defaultdict(float)
        for mod in modules:
            problems += self.module_problems(mod, allowed, recipe['allow'], mname)
            if receiver.get('uses_module_effects', True):
                for eff, v in self.raw['module'].get(mod, {}).get('effect', {}).items(): effects[eff] += v
        beacons = spec.get('beacons', defaults.get('beacons', [])) or []
        total = sum(b.get('count', 1) for b in beacons)
        beacon_W = 0.0
        beacon_rows: list[JSON] = []
        for b in beacons:
            bp = self.raw.get('beacon', {}).get(b['beacon'])
            if bp is None: raise ValueError(f'Unknown beacon: {b["beacon"]}')
            count, bmods = b.get('count', 1), self.module_list(b.get('modules', []))
            if len(bmods) > bp.get('module_slots', 0): problems.append(f'{b["beacon"]}: {len(bmods)} modules exceed slots')
            same = sum(x.get('count', 1) for x in beacons if x['beacon'] == b['beacon'])
            n = same if bp.get('beacon_counter') == 'same_type' else total
            profile = bp.get('profile') or [1]
            factor = bp.get('distribution_effectivity', 1) * profile[min(max(n, 1), len(profile)) - 1]
            for mod in bmods:
                problems += self.module_problems(mod, set(bp.get('allowed_effects', [])), recipe['allow'], b['beacon'])
                if receiver.get('uses_beacon_effects', True):
                    for eff, v in self.raw['module'].get(mod, {}).get('effect', {}).items(): effects[eff] += v * factor * count
            per_machine = b.get('per_machine', count)
            beacon_W += per_machine * watts(bp.get('energy_usage'))
            beacon_rows.append({'beacon': b['beacon'], 'count': count, 'modules': bmods, 'per_machine': per_machine, 'effect_factor': factor})
            if self.validate:
                blocked += [f'beacon {b["beacon"]}'] if self.buildable(entity=b['beacon']) is not True else []
                blocked += [f'module {x}' for x in sorted(set(bmods)) if self.buildable(item=x) is not True]
        for eff, v in (receiver.get('base_effect') or {}).items(): effects[eff] += v
        research_bonus = 0.0
        if recipe['mining']:
            research_bonus = self.mining_productivity
        elif self.force and self.research_productivity:
            research_bonus = self.db.availability(recipe['name'], self.force).get('productivity_bonus', 0) or 0
        effects['productivity'] += research_bonus
        if problems and not spec.get('ignore_module_rules'):
            raise ValueError(f'{spec.get("id", recipe["name"])}: ' + '; '.join(problems))

        speed_mult = max(0.2, 1 + effects['speed'])
        cons_mult = max(0.2, 1 + effects['consumption'])
        poll_mult = max(0.2, 1 + effects['pollution'])
        prod = min(max(0.0, effects['productivity']), recipe['maximum_productivity']) if recipe['allow_productivity'] else 0.0
        base_speed = m.get('mining_speed' if recipe['mining'] else 'crafting_speed', 1)
        crafts_per_machine = base_speed * speed_mult / recipe['energy_required']
        balance: defaultdict[str, float] = defaultdict(float)
        temps: defaultdict[str, set[float]] = defaultdict(set)
        for e in recipe['ingredients']:
            balance[e.get('type', 'item') + ':' + e['name']] -= e.get('amount', 0)
        for e in recipe['results']:
            k = e.get('type', 'item') + ':' + e['name']
            balance[k] += product_amount(e, prod)
            if e.get('type') == 'fluid':
                temps[k].add(e.get('temperature', self.raw.get('fluid', {}).get(e['name'], {}).get('default_temperature', 15)))
        src = m.get('energy_source', {})
        usage = watts(m.get('energy_usage'))
        active_W = usage * cons_mult
        if src.get('type') == 'electric':
            drain_W = watts(src['drain']) if 'drain' in src else (usage / 30 if kind in CRAFTING_KINDS else 0)
        else:
            drain_W = 0.0
        if self.validate:
            if not recipe['mining']:
                a = self.db.availability(recipe['name'], self.force)
                if a['usable_at_stage'] is not True: blocked.append(f'recipe {recipe["name"]} ({a["state"]})')
            if self.buildable(entity=mname) is not True: blocked.append(f'machine {mname}')
            blocked += [f'module {x}' for x in sorted(set(modules)) if self.buildable(item=x) is not True]
        return dict(id=spec.get('id') or recipe['name'], recipe=recipe['name'], machine=mname, machine_type=kind, modules=modules,
                    beacons=beacon_rows, speed_multiplier=speed_mult, productivity=prod, consumption_multiplier=cons_mult,
                    pollution_multiplier=poll_mult, research_productivity=research_bonus, crafts_per_machine=crafts_per_machine,
                    energy_type=src.get('type', 'void'), active_W=active_W, drain_W=drain_W, beacon_W=beacon_W,
                    pollution_per_minute=(src.get('emissions_per_minute') or {}).get('pollution', 0) * poll_mult * cons_mult,
                    balance={k: v for k, v in balance.items() if abs(v) > 1e-12}, temperatures={k: sorted(v) for k, v in temps.items()},
                    ingredient_temperatures={e.get('type', 'item') + ':' + e['name']: {t: e[t] for t in ('temperature', 'minimum_temperature', 'maximum_temperature') if t in e}
                                             for e in recipe['ingredients'] if e.get('type') == 'fluid' and any(t in e for t in ('temperature', 'minimum_temperature', 'maximum_temperature'))},
                    fixed_machines=spec.get('fixed_machines'), max_machines=spec.get('max_machines'), cost_weight=spec.get('cost_weight', 1),
                    blocked=blocked)

    # ---------- line set ----------
    def auto_ok(self, name: str, include_hidden: bool) -> bool:
        r = self.db.recipes[name]
        return not (self.db.virtual(name) or r.get('category', 'crafting') in AUTO_EXCLUDED_CATEGORIES
                    or any(s in name for s in AUTO_EXCLUDED_NAMES) or (r.get('hidden') and not include_hidden)
                    or (self.validate and self.db.availability(name, self.force)['usable_at_stage'] is not True))

    def build_lines(self, lines: Sequence[str | JSON], targets: Mapping[str, float], defaults: JSON, auto: bool,
                    imports: set[str], exclude: set[str], max_depth: int, max_lines: int, include_hidden: bool,
                    allow_mining: bool) -> tuple[list[Line], list[JSON], bool, list[str]]:
        out: list[Line] = []
        excluded: list[JSON] = []
        seen_ids: defaultdict[str, int] = defaultdict(int)
        for spec in lines:
            row = self.line(spec, defaults)
            seen_ids[row['id']] += 1
            if seen_ids[row['id']] > 1: row['id'] += f'#{seen_ids[row["id"]]}'
            if self.validate and row['blocked']:
                raise ValueError(f'Stage-locked line {row["id"]}: ' + ', '.join(row['blocked']) + '. Use validate_stage=false for theory mode.')
            out.append(row)
        truncated = False
        frontier_left: list[str] = []
        if auto:
            have = {r['recipe'] for r in out}
            frontier, expanded = [k for k in targets], set(imports)
            for depth in range(max_depth):
                nxt: list[str] = []
                for k in frontier:
                    if k in expanded: continue
                    expanded.add(k)
                    names = [n for n in self.db.producers.get(k.split(':', 1)[1], []) if n not in have and n not in exclude]
                    names += ['mining:' + r for r in self.resources.get(k, []) if allow_mining and 'mining:' + r not in have and 'mining:' + r not in exclude]
                    for n in names:
                        if not n.startswith('mining:') and not self.auto_ok(n, include_hidden): continue
                        if len(out) >= max_lines:
                            truncated = True
                            break
                        try:
                            row = self.line({'recipe': n}, defaults)
                        except ValueError as e:
                            excluded.append({'recipe': n, 'reason': str(e)})
                            have.add(n)
                            continue
                        if self.validate and row['blocked']:
                            have.add(n)
                            continue
                        # No net output of k (e.g. k is only a catalyst) says nothing about other
                        # outputs, so leave n eligible for a later frontier item.
                        if row['balance'].get(k, 0) <= EPS:
                            continue
                        have.add(n)
                        out.append(row)
                        nxt += [i for i, v in row['balance'].items() if v < 0]
                frontier = nxt
            frontier_left = sorted(set(frontier) - expanded)
        return out, excluded, truncated, frontier_left
