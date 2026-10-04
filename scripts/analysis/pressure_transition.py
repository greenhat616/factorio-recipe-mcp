"""Six-pack pressure-bus planning, with explicit projected technology stages.

All material balances use final prototypes. Future unlocks are hypothetical and
never written to the actual progress snapshot. Gas compression/decompression at
the producer/consumer is accounted for in recipe rates and machine counts.
"""
import argparse
import json
import math
from collections import defaultdict

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csc_matrix, eye, hstack, vstack

from recipe_mcp.database import Database, amount
from recipe_mcp.paths import DATA_DIR
from science_fluids import watts

PACKS = ['nullius-' + n + '-pack' for n in
         ['geology', 'climatology', 'mechanical', 'electrical', 'chemical', 'physics']]
INDUSTRIAL = ['nullius-high-pressure-chemistry-2', 'nullius-air-separation-4',
              'nullius-aluminum-production-3', 'nullius-steelmaking-3',
              'nullius-silicon-production-3', 'nullius-miniaturization-1',
              'nullius-volcanism-2', 'lambent-nil-phosphorus-chemistry-4',
              'nullius-mechanical-engineering-2', 'nullius-electrical-engineering-2',
              'nullius-experimental-chemistry-2']

# Remove the ordinary equivalent only when a preferred compressed process is
# unlocked. Different chemical routes and salt-producing electrolysis remain.
PREFERRED = {
    'nullius-pressure-water-electrolysis': ['nullius-water-electrolysis'],
    'nullius-pressure-steam-electrolysis': ['nullius-steam-electrolysis'],
    'nullius-high-pressure-steam-electrolysis': ['nullius-pressure-steam-electrolysis'],
    'nullius-pressure-air-separation': ['nullius-air-separation-1', 'nullius-air-separation-2'],
    'nullius-pressure-residual-separation': ['nullius-residual-separation'],
    'nullius-pressure-trace-separation': ['nullius-trace-separation'],
    'nullius-pressure-carbon-dioxide': ['nullius-carbon-monoxide-to-dioxide'],
    'nullius-pressure-carbon-monoxide': ['nullius-carbon-monoxide'],
    'nullius-pressure-methane': ['nullius-methane'],
    'nullius-pressure-ethylene': ['nullius-ethylene'],
    'nullius-pressure-alkene-synthesis': ['nullius-carbon-monoxide-to-alkenes'],
    'nullius-pressure-methanol': ['nullius-methanol'],
    'nullius-pressure-solvent': ['nullius-solvent'],
    'nullius-pressure-monoxide-to-graphite': ['nullius-graphite'],
    'nullius-pressure-methane-to-graphite': ['nullius-methane-to-graphite'],
    'nullius-boxed-pressure-graphite': ['nullius-boxed-graphite'],
    'nullius-pressure-bpa': ['nullius-bpa'],
    'nullius-boxed-pressure-bpa': ['nullius-boxed-bpa'],
    'nullius-pressure-filter-1': ['nullius-filter-1'],
    'nullius-boxed-pressure-filter-1': ['nullius-boxed-filter-1'],
    'nullius-pressure-benzene-reforming': ['nullius-benzene-reforming'],
    'nullius-pressure-butadiene': ['nullius-butadiene'],
}


def closure(db, targets):
    seen = set()
    def visit(n):
        if n in seen:
            return
        seen.add(n)
        for p in db.raw['technology'][n].get('prerequisites', []):
            visit(p)
    for n in targets:
        visit(n)
    return seen


class Stage:
    def __init__(self, db, force, targets=(), disabled=()):
        self.db, self.force = db, force
        self.current = {n for n, t in db.progress['forces'][force]['technologies'].items() if t['researched']}
        self.added = closure(db, targets) - self.current
        self.unlocks = defaultdict(list)
        for n in sorted(self.added):
            for pack in db.science(n, force)['science_packs']:
                if pack not in PACKS:
                    raise ValueError(f'{n} needs post-physics pack {pack}')
            for e in db.raw['technology'][n].get('effects', []):
                if e['type'] == 'unlock-recipe':
                    self.unlocks[e['recipe']].append(n)
        self.allowed = {n for n in db.recipes if not db.virtual(n) and
                        (db.availability(n, force)['usable_at_stage'] is True or n in self.unlocks)} - set(disabled)
        self.equipment = {}
        self.choices = defaultdict(list)
        for kind in ['assembling-machine', 'furnace', 'rocket-silo']:
            for n, m in db.raw.get(kind, {}).items():
                # Mirrored prototypes in this installation have incorrect mining
                # results; use normal prototypes with the real construction item.
                if 'mirror' in n or '-surge-' in n or 'broken' in n:
                    continue
                place = m.get('placeable_by', {})
                item = place.get('item') if isinstance(place, dict) else None
                item = item or m.get('minable', {}).get('result')
                if not item:
                    continue
                manufacturers = []
                for r in db.producers.get(item, []):
                    recipe = db.recipes[r]
                    if r not in self.allowed or recipe.get('category') in ['packaging', 'nullius-barrel', 'nullius-unbarrel']:
                        continue
                    net = sum(amount(e) for e in recipe.get('results', []) if e['name'] == item) - sum(amount(e) for e in recipe.get('ingredients', []) if e['name'] == item)
                    if net > 0 and not any(s in r for s in ['legacy', 'recycling']):
                        manufacturers.append(r)
                if not manufacturers or m.get('energy_source', {}).get('type') not in ['electric', 'void', 'heat']:
                    continue
                self.equipment[n] = dict(item=item, manufacturing_recipes=manufacturers)
                for category in m.get('crafting_categories', []):
                    self.choices[category].append((n, m))
        self.operations = {}
        for n in sorted(self.allowed):
            r = db.recipes[n]
            opts = [p for p in self.choices[r.get('category', 'crafting')] if not p[1].get('fixed_recipe') or p[1]['fixed_recipe'] == n]
            if not opts or not r.get('results') or r.get('category') in ['turbine-open', 'turbine-closed', 'nullius-power-sink']:
                continue
            machine, proto = min(opts, key=lambda p: (-p[1].get('crafting_speed', 1), watts(p[1].get('energy_usage', '0W')), p[0]))
            seconds = r.get('energy_required', .5) / proto.get('crafting_speed', 1)
            power = (watts(proto.get('energy_usage', '0W')) + watts(proto.get('energy_source', {}).get('drain', '0W'))) / 1e6
            self.operations[n] = dict(recipe=r, machine=machine, machine_seconds=seconds,
                                      MW_per_craft_per_second=power * seconds,
                                      energy_type=proto.get('energy_source', {}).get('type'))


def solve(db, force, name, targets, rate, conserve_volcanic=False, disabled=(), volcanic_cap=None):
    stage = Stage(db, force, targets, disabled)
    banned = {old for preferred, older in PREFERRED.items() if preferred in stage.operations for old in older}
    # Keep the already-adopted second geology/climatology processes. Otherwise a
    # volcanic-minimization diagnostic reverts to enormous early-game air/sea use.
    if 'nullius-geology-pack-2' in stage.operations:
        banned.add('nullius-geology-pack')
    if 'nullius-climatology-pack-2' in stage.operations:
        banned.add('nullius-climatology-pack')
    # A true compression operation has exactly one input and one output of
    # different forms of the same gas. Carbon deposition is NOT such an operation.
    compress, decompress, gas_map = {}, {}, {}
    conversions = set()
    for n, op in stage.operations.items():
        r = op['recipe']
        ins, outs = r.get('ingredients', []), r.get('results', [])
        if len(ins) != 1 or len(outs) != 1 or ins[0]['type'] != 'fluid' or outs[0]['type'] != 'fluid':
            continue
        if r.get('category') == 'compression' and outs[0]['name'].startswith('nullius-compressed-'):
            base, pressed = ins[0]['name'], outs[0]['name']
            compress[base] = n
            gas_map[base] = (pressed, amount(ins[0]) / amount(outs[0]))
        elif r.get('category') == 'decompression' and ins[0]['name'].startswith('nullius-compressed-'):
            decompress[outs[0]['name']] = n
    gas_map = {n: p for n, p in gas_map.items() if n in decompress}
    conversions = {compress[n] for n in gas_map} | {decompress[n] for n in gas_map}
    columns = []
    for n, op in stage.operations.items():
        if n in banned or n in conversions:
            continue
        balance, extra = defaultdict(float), defaultdict(float)
        for key, sign in [('ingredients', -1), ('results', 1)]:
            for e in op['recipe'].get(key, []):
                material, quantity = e['type'] + ':' + e['name'], amount(e)
                if e['type'] == 'fluid' and e['name'] in gas_map:
                    pressed, ratio = gas_map[e['name']]
                    material = 'fluid:' + pressed
                    conversion = compress[e['name']] if sign == 1 else decompress[e['name']]
                    cr = stage.operations[conversion]['recipe']
                    divisor = amount(cr['ingredients'][0]) if sign == 1 else amount(cr['results'][0])
                    extra[conversion] += quantity / divisor
                    quantity /= ratio
                balance[material] += sign * quantity
        cost = op['machine_seconds'] + sum(stage.operations[r]['machine_seconds'] * v for r, v in extra.items())
        columns.append(dict(name=n, balance=dict(balance), extras=dict(extra), cost=max(cost, 1e-7)))
    imported = {}
    for n, r in sorted(db.raw.get('resource', {}).items()):
        if n.startswith('creative-') or not r.get('autoplace'):
            continue
        mining = r['minable']
        outs = mining.get('results') or [dict(type='item', name=mining['result'], amount=1)]
        for e in outs:
            material = e['type'] + ':' + e['name']
            imported['extract:' + n] = material
            columns.append(dict(name='extract:' + n, balance={material: 1}, extras={}, cost=1e-7))
    materials = sorted({n for c in columns for n in c['balance']} | {'item:' + n for n in PACKS})
    indices = {n: i for i, n in enumerate(materials)}
    rr, cc, vv = [], [], []
    for j, c in enumerate(columns):
        for n, v in c['balance'].items():
            if v:
                rr.append(indices[n]); cc.append(j); vv.append(v)
    matrix = csc_matrix((vv, (rr, cc)), shape=(len(materials), len(columns)))
    target = np.array([rate if n in {'item:' + p for p in PACKS} else 0 for n in materials])
    primary = np.array([1 if c['name'] == 'extract:nullius-fumarole' else 0 for c in columns], dtype=float)
    costs = np.array([c['cost'] for c in columns])
    limits = {}
    if volcanic_cap is not None:
        limits = dict(A_ub=csc_matrix(primary.reshape(1, -1)), b_ub=[volcanic_cap])
    if conserve_volcanic:
        first = linprog(primary, A_eq=matrix, b_eq=target, bounds=(0, None), method='highs')
        if not first.success:
            raise RuntimeError(name + ': ' + first.message)
        limits = dict(A_ub=csc_matrix(primary.reshape(1, -1)), b_ub=[first.fun + 1e-5])
    result = linprog(costs, A_eq=matrix, b_eq=target, bounds=(0, None), method='highs', **limits)
    if not result.success:
        diagnostic = linprog([1e-9] * len(columns) + [1e6 if n.endswith('-pack') else 1 for n in materials] + [1] * len(materials),
                             A_eq=hstack([matrix, eye(len(materials)), -eye(len(materials))]), b_eq=target, bounds=(0, None), method='highs')
        missing = [(n, diagnostic.x[len(columns)+i]) for i, n in enumerate(materials) if diagnostic.success and diagnostic.x[len(columns)+i] > 1e-6]
        raise RuntimeError(name + ': ' + result.message + repr(missing))
    rates, supplies = defaultdict(float), {}
    for c, v in zip(columns, result.x):
        if v < 1e-8:
            continue
        if c['name'] in imported:
            supplies[imported[c['name']]] = float(v)
            continue
        rates[c['name']] += float(v)
        for n, multiplier in c['extras'].items():
            rates[n] += float(v) * multiplier
    check = db.balance(rates)
    for n, v in supplies.items():
        check[n] = check.get(n, 0) + v
    all_materials = set(check) | {'item:' + n for n in PACKS}
    residual = max(abs(check.get(n, 0) - (rate if n in {'item:' + p for p in PACKS} else 0)) for n in all_materials)
    assert residual < 1e-4, residual
    assert all(n in stage.allowed for n in rates)
    demand, output, disposal = defaultdict(float), defaultdict(float), defaultdict(float)
    native_use, decompressed_use, co_products = defaultdict(float), defaultdict(float), defaultdict(float)
    uses = defaultdict(list)
    active = []
    for n, crafts in sorted(rates.items()):
        op = stage.operations[n]
        r = op['recipe']
        active.append(dict(recipe=n, crafts_per_second=crafts, machine=op['machine'],
                           machines=crafts * op['machine_seconds'],
                           MW=crafts * op['MW_per_craft_per_second'], energy_type=op['energy_type'],
                           projected_unlocks=stage.unlocks.get(n, [])))
        if n in conversions:
            continue
        void = r.get('category') in ['nullius-gas-void', 'nullius-liquid-void']
        for key, container in [('ingredients', disposal if void else demand), ('results', output)]:
            for e in r.get(key, []):
                if e['type'] != 'fluid':
                    continue
                fluid, qty = e['name'], amount(e) * crafts
                if fluid in gas_map:
                    pressed, ratio = gas_map[fluid]
                    fluid, qty = pressed, qty / ratio
                    if key == 'ingredients' and not void:
                        decompressed_use[fluid] += qty
                elif key == 'ingredients' and not void:
                    native_use[fluid] += qty
                container[fluid] += qty
                if key == 'ingredients' and not void:
                    uses[fluid].append(dict(recipe=n, units_per_second=qty))
    pipes = [dict(fluid=n, process_demand_per_second=v,
                  direct_compressed_recipe_demand=native_use[n], decompression_for_local_use=decompressed_use[n],
                  production_per_second=output[n], disposal_per_second=disposal[n],
                  pump2_banks_at_80pct=math.ceil(v / 4800 - 1e-9), pump3_banks_at_80pct=math.ceil(v / 9600 - 1e-9),
                  top_consumers=sorted(uses[n], key=lambda r: -r['units_per_second'])[:6])
             for n, v in sorted(demand.items(), key=lambda p: -p[1]) if v > 1e-6]
    upgrades = []
    for n in sorted(stage.added):
        t = db.raw['technology'][n]
        upgrades.append(dict(technology=n, prerequisites=t.get('prerequisites', []), unit=t.get('unit'),
                             recipes=[e['recipe'] for e in t.get('effects', []) if e['type'] == 'unlock-recipe']))
    return dict(name=name, target_each_pack_per_second=rate, added_targets=targets,
                future_projection=bool(stage.added), new_technologies=upgrades,
                stage_policy='Actual unlocked recipes plus explicit prerequisite closure; checkpoint completion assumed only in future projections.',
                route_policy='Prefer compressed recipe counterparts; compress gas at producers, decompress only at local consumers. No modules or beacons.',
                objective=('First minimize volcanic-gas extraction, then continuous machine count.' if conserve_volcanic else
                           'Minimize continuous machine count with the declared volcanic extraction cap.' if volcanic_cap is not None else
                           'Minimize continuous machine count; volcanic-gas supply unconstrained.'),
                volcanic_extraction_cap=volcanic_cap,
                boundary='Mining/extraction equipment, generation fuel, mall, transport pump energy and layout excluded. Heat exchanger duty is external thermal supply, not free steam. Fluid distribution flow excludes local excess venting.',
                balance_residual=residual, raw_sha256=db.raw_sha256, provenance=db.progress['provenance'],
                fluid_bus=pipes, supplies=supplies, gas_equivalence=gas_map, excluded_ordinary_recipes=sorted(banned),
                active_recipes=active, machines_continuous=sum(r['machines'] for r in active),
                electrical_MW=sum(r['MW'] for r in active if r['energy_type'] == 'electric'),
                thermal_MW=sum(r['MW'] for r in active if r['energy_type'] == 'heat'),
                equipment={n: stage.equipment[n] for n in sorted({r['machine'] for r in active})})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rate', type=float, default=150)
    parser.add_argument('--force', default='faction-a632079')
    args = parser.parse_args()
    db = Database()
    db.require_force(args.force)
    cases = [
        ('entry_volcanic', [], False, []),
        ('entry_water_electrolysis', [], False, ['nullius-pressure-steam-electrolysis', 'nullius-steam-electrolysis']),
        ('mechanical_upgrade', ['nullius-mechanical-engineering-2'], False, []),
        ('science_upgrades', ['nullius-mechanical-engineering-2', 'nullius-electrical-engineering-2', 'nullius-experimental-chemistry-2'], False, []),
        ('industrial_volcanic', INDUSTRIAL, False, []),
        ('industrial_capped', INDUSTRIAL, False, []),
        ('carbon_volcanic', INDUSTRIAL + ['nullius-carbon-sequestration-3'], False, []),
        ('carbon_without_new_recipes', INDUSTRIAL + ['nullius-carbon-sequestration-3'], False,
         ['nullius-carbon-dioxide-electrolysis', 'nullius-carbon-deposition', 'nullius-carbon-sink']),
    ]
    results = []
    for name, targets, conserve, disabled in cases:
        cap = results[0]['supplies']['fluid:nullius-volcanic-gas'] if name == 'industrial_capped' else None
        report = solve(db, args.force, name, targets, args.rate, conserve, disabled, cap)
        path = DATA_DIR / ('pressure-transition-' + name + '.json')
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(name, 'new techs:', len(report['new_technologies']), 'balance:', report['balance_residual'],
              'machines:', round(report['machines_continuous']), 'volcanic:', round(report['supplies'].get('fluid:nullius-volcanic-gas', 0)), flush=True)
        results.append(report)
    (DATA_DIR / 'pressure-transition-all.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
