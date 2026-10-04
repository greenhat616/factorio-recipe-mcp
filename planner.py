"""Helmod / Factory Planner style production solver over final prototypes.

Two solvers share one line model (recipe + machine + modules + beacons):

* ``lp``     – linear program (HiGHS). Picks among alternative recipes, allows
               raw imports and penalised surplus, minimises a weighted objective
               (machines / power / imports). Returns shadow prices per item.
* ``matrix`` – exact linear algebra (Factory Planner "matrix solver"). One
               unknown per line plus import/surplus unknowns chosen by item role;
               reports rank, degrees of freedom, null space and inconsistent items
               instead of silently guessing.

Effects follow the 2.0 prototype docs: module effects + beacon effects
(distribution_effectivity x profile[beacon count]) + machine base_effect + force
recipe productivity; speed/consumption/pollution multipliers are clamped at 20%,
productivity at [0, maximum_productivity] and zeroed when the recipe disallows it.
Electric drain defaults to energy_usage/30 for crafting machines. Quality,
surface effects, fluid-resource yield depletion and belt/pipe throughput are not
modelled. Rates are expected values (probability and amount ranges averaged).
"""
import math
import re
from collections import defaultdict

import numpy as np
from scipy.optimize import linprog

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


def watts(value):
    m = re.fullmatch(r'\s*([0-9.eE+-]+)\s*([kMGTP]?)W\s*', str(value or '0W'))
    if not m: raise ValueError(f'Unrecognised power value: {value}')
    return float(m.group(1)) * {'': 1, 'k': 1e3, 'M': 1e6, 'G': 1e9, 'T': 1e12, 'P': 1e15}[m.group(2)]


def product_amount(entry, productivity=0.0):
    """Expected output per craft; productivity skips ignored_by_productivity."""
    p = entry.get('probability', 1)
    avg = entry['amount'] if 'amount' in entry else (entry.get('amount_min', 0) + entry.get('amount_max', 0)) / 2
    ignored = min(avg, entry.get('ignored_by_productivity', 0))
    return p * avg + entry.get('extra_count_fraction', 0) + p * max(0.0, avg - ignored) * productivity


def entries(value):
    """Lua empty tables export as {}; non-empty arrays as lists."""
    return list(value.values()) if isinstance(value, dict) else list(value or [])


def beneficial(effect, value):
    return value < 0 if effect == 'consumption' or effect == 'pollution' else value > 0


class Planner:
    def __init__(self, db, force='', validate_stage=True, research_productivity=True, mining_productivity=0.0):
        self.db, self.raw = db, db.raw
        self.validate = validate_stage
        self.force = db.require_force(force) if validate_stage else (db.force_name(force) if db.progress_compatible and (force or db.default_force) else None)
        self.research_productivity = research_productivity
        self.mining_productivity = mining_productivity
        self.warnings = []
        self._build_cache = {}
        self.types = defaultdict(set)
        for r in db.recipes.values():
            for e in entries(r.get('ingredients')) + entries(r.get('results')): self.types[e['name']].add(e.get('type', 'item'))
        self.resources = defaultdict(list)
        for n, res in self.raw.get('resource', {}).items():
            if any(s in n for s in AUTO_EXCLUDED_NAMES): continue
            for e in self.mining_results(res):
                self.types[e['name']].add(e.get('type', 'item'))
                self.resources[e.get('type', 'item') + ':' + e['name']].append(n)
        self.machines_by_category = defaultdict(list)
        for kind in CRAFTING_KINDS:
            for n, m in self.raw.get(kind, {}).items():
                for c in m.get('crafting_categories', []): self.machines_by_category[c].append((kind, n, m))
        self.drills_by_category = defaultdict(list)
        for n, m in self.raw.get('mining-drill', {}).items():
            for c in m.get('resource_categories', []): self.drills_by_category[c].append(('mining-drill', n, m))

    # ---------- identifiers ----------
    def key(self, name):
        if ':' in name and name.split(':', 1)[0] in ('item', 'fluid'): return name
        types = self.types.get(name) or ({'fluid'} if name in self.raw.get('fluid', {}) else {'item'})
        if len(types) > 1: raise ValueError(f'Ambiguous material {name}; use item:{name} or fluid:{name}')
        return next(iter(types)) + ':' + name

    @staticmethod
    def mining_results(res):
        minable = res.get('minable', {})
        if 'results' in minable: return entries(minable['results'])
        if 'result' in minable:
            entry = {'type': 'item', 'name': minable['result'], 'amount': minable.get('count', 1)}
            if 'amount_min' in res.get('minable', {}): entry.update(amount_min=minable['amount_min'], amount_max=minable['amount_max'])
            return [entry]
        return []

    # ---------- research gates ----------
    def buildable(self, entity=None, item=None):
        k = (entity, item)
        if k not in self._build_cache:
            s = self.db.machine_stage(entity, self.force) if entity else self.db.item_stage(item, self.force)
            self._build_cache[k] = s['buildable_at_stage']
        return self._build_cache[k]

    # ---------- line model ----------
    def recipe_view(self, name):
        if name.startswith('mining:'):
            rname = name[7:]
            res = self.raw.get('resource', {}).get(rname)
            if res is None: raise ValueError(f'Unknown resource: {rname}')
            minable = res.get('minable', {})
            ingredients = []
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

    def fluid_fit(self, recipe, proto):
        if recipe['mining']: return True
        need_in = sum(e.get('type') == 'fluid' for e in recipe['ingredients'])
        need_out = sum(e.get('type') == 'fluid' for e in recipe['results'])
        boxes = proto.get('fluid_boxes') or []
        have_in = sum(b.get('production_type') in ('input', 'input-output') for b in boxes)
        have_out = sum(b.get('production_type') in ('output', 'input-output') for b in boxes)
        return need_in <= have_in and need_out <= have_out

    def candidates(self, recipe):
        pool = self.drills_by_category if recipe['mining'] else self.machines_by_category
        return [(k, n, m) for k, n, m in pool.get(recipe['category'], [])
                if not m.get('fixed_recipe') or m['fixed_recipe'] == recipe['name']]

    def pick_machine(self, recipe, preference='fastest', preferred=()):
        speed_key = 'mining_speed' if recipe['mining'] else 'crafting_speed'
        opts = [(k, n, m) for k, n, m in self.candidates(recipe)
                if not any(s in n for s in AUTO_EXCLUDED_MACHINES) and self.fluid_fit(recipe, m)
                and (not self.validate or self.buildable(entity=n) is True)]
        if not opts: return None
        for p in preferred:
            hit = next((o for o in opts if o[1] == p), None)
            if hit: return hit
        def energy(m): return watts(m.get('energy_usage')) + watts(m.get('energy_source', {}).get('drain', '0W'))
        if preference == 'slowest': return min(opts, key=lambda o: (o[2].get(speed_key, 1), energy(o[2]), o[1]))
        if preference == 'efficient': return min(opts, key=lambda o: (energy(o[2]) / o[2].get(speed_key, 1), -o[2].get(speed_key, 1), o[1]))
        if preference != 'fastest': raise ValueError('machine preference must be fastest, efficient or slowest')
        return max(opts, key=lambda o: (o[2].get(speed_key, 1), -energy(o[2]), o[1]))

    @staticmethod
    def module_list(spec):
        if isinstance(spec, dict): return [n for n, c in spec.items() for _ in range(int(c))]
        return list(spec or [])

    def module_problems(self, module, allowed, recipe_allow, where):
        proto = self.raw.get('module', {}).get(module)
        if proto is None: return [f'{where}: unknown module {module}']
        out = []
        for eff, v in proto.get('effect', {}).items():
            if beneficial(eff, v) and eff not in allowed: out.append(f'{where}: {module} {eff} not allowed by entity')
            if beneficial(eff, v) and not recipe_allow.get(eff, True): out.append(f'{where}: {module} {eff} not allowed by recipe')
        return out

    def line(self, spec, defaults=None):
        """Normalise one line spec into its per-craft balance, rates, power and gates."""
        defaults = defaults or {}
        if isinstance(spec, str): spec = {'recipe': spec}
        recipe = self.recipe_view(spec['recipe'])
        problems, blocked = [], []
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
        effects = defaultdict(float)
        for mod in modules:
            problems += self.module_problems(mod, allowed, recipe['allow'], mname)
            if receiver.get('uses_module_effects', True):
                for eff, v in self.raw['module'].get(mod, {}).get('effect', {}).items(): effects[eff] += v
        beacons = spec.get('beacons', defaults.get('beacons', [])) or []
        total = sum(b.get('count', 1) for b in beacons)
        beacon_W, beacon_rows = 0.0, []
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
        balance, temps = defaultdict(float), defaultdict(set)
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
    def auto_ok(self, name, include_hidden):
        r = self.db.recipes[name]
        return not (self.db.virtual(name) or r.get('category', 'crafting') in AUTO_EXCLUDED_CATEGORIES
                    or any(s in name for s in AUTO_EXCLUDED_NAMES) or (r.get('hidden') and not include_hidden)
                    or (self.validate and self.db.availability(name, self.force)['usable_at_stage'] is not True))

    def build_lines(self, lines, targets, defaults, auto, imports, exclude, max_depth, max_lines, include_hidden, allow_mining):
        out, excluded = [], []
        seen_ids = defaultdict(int)
        for spec in lines:
            row = self.line(spec, defaults)
            seen_ids[row['id']] += 1
            if seen_ids[row['id']] > 1: row['id'] += f'#{seen_ids[row["id"]]}'
            if self.validate and row['blocked']:
                raise ValueError(f'Stage-locked line {row["id"]}: ' + ', '.join(row['blocked']) + '. Use validate_stage=false for theory mode.')
            out.append(row)
        truncated, frontier_left = False, []
        if auto:
            have = {r['recipe'] for r in out}
            frontier, expanded = [k for k in targets], set(imports)
            for depth in range(max_depth):
                nxt = []
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

    # ---------- solvers ----------
    def matrix(self, lines):
        keys = sorted({k for r in lines for k in r['balance']})
        A = np.zeros((len(keys), len(lines)))
        index = {k: i for i, k in enumerate(keys)}
        for j, r in enumerate(lines):
            for k, v in r['balance'].items(): A[index[k], j] = v
        return keys, A

    def solve_lp(self, lines, targets, imports, forbid_imports, import_costs, allow_surplus, surplus_items, weights):
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
        c = []
        for r in lines:
            per_craft_machines = 1 / r['crafts_per_machine']
            mw = (r['active_W'] + r['drain_W'] + r['beacon_W']) / 1e6 if r['energy_type'] == 'electric' else r['beacon_W'] / 1e6
            c.append(r['cost_weight'] * per_craft_machines * (weights['machines'] + weights['power_MW'] * mw))
        c += [weights['imports'] * import_costs.get(k, 1.0) for k in importable]
        c += [weights['surplus']] * S
        bounds = []
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

    def analyze_matrix(self, lines, targets, imports, surplus_items):
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

    def solve_matrix(self, lines, targets, imports, surplus_items):
        keys, cols, M, b, info, fixed_lines = self.analyze_matrix(lines, targets, imports, surplus_items)
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
            self.warnings.append('Negative unknowns (plan not physically realisable as declared): ' + ', '.join(negative) +
                                 '. A negative import is an unused surplus; a negative surplus is a deficit; a negative line runs backwards.')
        return dict(x=x.tolist(), imports=imports_out, surplus=surplus_out, residual=float(np.max(np.abs(resid), initial=0)),
                    matrix=info, negative=negative)


def report(planner, lines, solution, targets, factor, prices_limit=40):
    flows = defaultdict(lambda: {'produced': 0.0, 'consumed': 0.0})
    rows, total = [], defaultdict(float)
    for r, x in zip(lines, solution['x']):
        if abs(x) < EPS: continue
        machines = x / r['crafts_per_machine']
        count = math.ceil(machines - 1e-6) if machines > 0 else 0
        elec = r['energy_type'] == 'electric'
        power = machines * ((r['active_W'] + r['drain_W']) if elec else 0) + machines * r['beacon_W']
        installed = count * ((r['active_W'] + r['drain_W']) if elec else 0) + count * r['beacon_W']
        fuel = machines * r['active_W'] if not elec and r['energy_type'] != 'void' else 0
        for k, v in r['balance'].items():
            flows[k]['produced' if v * x > 0 else 'consumed'] += abs(v * x)
        machines, x = float(machines), float(x)
        rows.append({'id': r['id'], 'recipe': r['recipe'], 'machine': r['machine'], 'modules': r['modules'], 'beacons': r['beacons'],
                     'crafts': x * factor, 'machines': machines, 'machines_ceil': count,
                     'speed_multiplier': r['speed_multiplier'], 'productivity': r['productivity'],
                     'consumption_multiplier': r['consumption_multiplier'],
                     'power_MW': power / 1e6, 'installed_power_MW': installed / 1e6,
                     (r['energy_type'] + '_fuel_MW'): fuel / 1e6, 'pollution_per_minute': machines * r['pollution_per_minute'],
                     'inputs': {k: -v * x * factor for k, v in r['balance'].items() if v < 0},
                     'outputs': {k: v * x * factor for k, v in r['balance'].items() if v > 0}})
        total['machines'] += machines; total['machines_ceil'] += count
        total['power_MW'] += power / 1e6; total['installed_power_MW'] += installed / 1e6
        total['non_electric_fuel_MW'] += fuel / 1e6; total['pollution_per_minute'] += machines * r['pollution_per_minute']
    for k, v in solution['imports'].items(): flows[k]['import'] = v
    for k, v in solution['surplus'].items(): flows[k]['surplus'] = v
    items = []
    for k in sorted(flows):
        f = flows[k]
        if max(abs(v) for v in f.values()) < EPS: continue
        items.append({'item': k, **{n: v * factor for n, v in f.items()}, 'target': targets.get(k, 0) * factor})
    warnings = list(planner.warnings)
    active = [(r, x) for r, x in zip(lines, solution['x']) if x > EPS]
    for r, x in active:
        for k, need in r['ingredient_temperatures'].items():
            supply = [t for p, y in active if p['balance'].get(k, 0) > 0 for t in p['temperatures'].get(k, [])]
            lo, hi = need.get('minimum_temperature', need.get('temperature', -math.inf)), need.get('maximum_temperature', need.get('temperature', math.inf))
            if supply and not any(lo <= t <= hi for t in supply):
                warnings.append(f'{r["id"]}: needs {k} at {need}, producers in plan supply {sorted(set(supply))}')
    out = {'lines': rows, 'items': items,
           'imports': {k: v * factor for k, v in solution['imports'].items() if abs(v) > EPS},
           'surplus': {k: v * factor for k, v in solution['surplus'].items() if abs(v) > EPS},
           'totals': dict(total), 'max_balance_error': solution['residual'], 'warnings': warnings}
    if 'prices' in solution:
        # Marginal objective cost of one more unit/second of each item (targets and imports first).
        keep = list(dict.fromkeys(list(targets) + list(out['imports'])))
        others = sorted((k for k, v in solution['prices'].items() if k not in keep and abs(v) > EPS),
                        key=lambda k: -abs(solution['prices'][k]))
        keep += others[:max(0, prices_limit - len(keep))]
        out['shadow_prices'] = {k: float(solution['prices'][k]) / factor for k in keep if k in solution['prices']}
        out['objective'] = solution['objective']
    if 'matrix' in solution: out['matrix'] = solution['matrix']
    return out


def plan(db, targets, lines=(), solver='lp', per='second', defaults=None, auto_discover=None, imports=(), forbid_imports=(),
         import_costs=None, allow_surplus=True, surplus_items=(), objective='balanced', weights=None, exclude_recipes=(),
         max_depth=12, max_lines=1000, include_hidden=False, allow_mining=True, mining_productivity=0.0,
         research_productivity=True, force='', validate_stage=True):
    if per not in TIME: raise ValueError('per must be second, minute or hour')
    if solver not in ('lp', 'matrix'): raise ValueError('solver must be lp or matrix')
    factor = TIME[per]
    p = Planner(db, force, validate_stage, research_productivity, mining_productivity)
    tgt = {}
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
        sol = p.solve_lp(rows, tgt, imports, forbid, costs, allow_surplus, surplus, w)
    else:
        sol = p.solve_matrix(rows, tgt, imports, surplus)
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


def machine_stats(db, recipe, machine='', modules=None, beacons=(), defaults=None, rate=None, item='', per='second',
                  force='', validate_stage=True, mining_productivity=0.0, research_productivity=True):
    """Single line calculator (one recipe in one machine), optional machine count for a rate."""
    if per not in TIME: raise ValueError('per must be second, minute or hour')
    factor = TIME[per]
    p = Planner(db, force, validate_stage, research_productivity, mining_productivity)
    spec = {'recipe': recipe, 'beacons': list(beacons)}
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


def production_matrix(db, lines, targets=None, imports=(), surplus_items=(), defaults=None, force='', validate_stage=False, dense_limit=60):
    """Stoichiometric matrix (items x lines), rank and Factory Planner style determinacy report."""
    p = Planner(db, force, validate_stage)
    rows = [p.line(s, defaults or {}) for s in lines]
    tgt = {p.key(k): v for k, v in (targets or {}).items()}
    keys, A = p.matrix(rows)
    _, _, _, _, info, _ = p.analyze_matrix(rows, tgt, {p.key(n) for n in imports}, {p.key(n) for n in surplus_items})
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
