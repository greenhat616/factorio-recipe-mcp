"""Optimize a declared air/water -> methane -> methanol process boundary.

LP minimizes continuous operating electricity over module combinations. Reported
installed-machine power includes ceiling-rounded machines and fixed drains.
Not a global optimization of unrelated industry, biology, beacons or energy generation.
"""
import itertools
import argparse
import json
import math
from collections import defaultdict
from collections.abc import Iterable
import numpy as np
from scipy.optimize import linprog
from recipe_mcp.database import JSON, Database, amount
from recipe_mcp.paths import DATA_DIR
from recipe_mcp.planner import drain_watts

db = Database()

def mw(value: str) -> float:
    for suffix, factor in [('GW', 1000), ('MW', 1), ('kW', .001), ('W', .000001)]:
        if value.endswith(suffix): return float(value[:-len(suffix)]) * factor
    raise ValueError(value)

def solve(q: float = 100, route: str = 'normal', modules: str = 'optimize', water: str = 'sea', tier: int = 2,
          module_tier: int = 1, theoretical: bool = False, force: str | None = None) -> JSON:
    selected = None if theoretical else db.require_force(force)
    pairs = {
        'nullius-air': f'nullius-air-filter-{tier}',
        'nullius-air-separation-1': f'nullius-distillery-{tier}',
        'nullius-water-electrolysis': f'nullius-priority-electrolyzer-{tier}',
        'nullius-steam-electrolysis': f'nullius-priority-electrolyzer-{tier}',
        'nullius-boiling-water': 'nullius-boiler-1',
        'nullius-hydrogen-combustion-1': f'nullius-combustion-chamber-{tier}',
    }
    if water == 'sea':
        pairs.update({'nullius-seawater': f'nullius-seawater-intake-{tier}',
                      'nullius-boiling-seawater': 'nullius-boiler-1',
                      'nullius-seawater-filtration': f'nullius-hydro-plant-{tier}',
                      'nullius-water': f'nullius-distillery-{tier}'})
    else:
        pairs.update({'nullius-freshwater': 'nullius-well-1', 'nullius-freshwater-filtration': f'nullius-hydro-plant-{tier}'})
    if route == 'normal':
        pairs.update({'nullius-methane': f'nullius-chemical-plant-{tier}', 'nullius-methanol': f'nullius-chemical-plant-{tier}'})
    else:
        pairs.update({n: f'nullius-chemical-plant-{tier}' for n in ['nullius-pressure-methane', 'nullius-pressure-methanol']})
        pairs.update({n: f'nullius-priority-compressor-{tier}' for n in
                      ['nullius-compressed-hydrogen', 'nullius-compressed-oxygen', 'nullius-compressed-carbon-dioxide']})
        pairs['nullius-pressure-water-electrolysis'] = f'nullius-priority-electrolyzer-{tier}'
        pairs['nullius-air-separation-2'] = f'nullius-distillery-{tier}'
        if route == 'pressure-direct':
            for n in ['nullius-water-electrolysis', 'nullius-compressed-hydrogen', 'nullius-compressed-oxygen']: pairs.pop(n)
    variants = []
    excluded = []
    for n, machine in pairs.items():
        if not theoretical and (db.availability(n,selected)['usable_at_stage'] is not True or
                                db.machine_stage(machine,selected)['buildable_at_stage'] is not True):
            excluded.append({'recipe':n,'machine':machine})
            continue
        r, m = db.recipes[n], db.raw['assembling-machine'][machine]
        slots = m.get('module_slots', 0)
        choices = ['']
        configs: Iterable[tuple[str, ...]]
        if modules == 'save-feedstock' and n in ['nullius-methane','nullius-methanol','nullius-pressure-methane','nullius-pressure-methanol']:
            configs = [tuple([f'nullius-yield-module-{module_tier}'] * slots)]
        elif modules in ['efficiency','save-feedstock']:
            configs = [tuple([f'nullius-efficiency-module-{module_tier}'] * slots)]
        elif modules == 'none':
            configs = [()]
        else:
            choices += [f'nullius-efficiency-module-{module_tier}', f'nullius-haste-module-{module_tier}', f'nullius-speed-module-{module_tier}']
            if r.get('allow_productivity'): choices.append(f'nullius-yield-module-{module_tier}')
            configs = itertools.combinations_with_replacement(choices, slots)
        for config in configs:
            if not theoretical and any(mod and db.item_stage(mod,selected)['buildable_at_stage'] is not True for mod in config): continue
            effects: defaultdict[str, float] = defaultdict(float)
            for mod in config:
                if mod:
                    for k, v in db.raw['module'][mod]['effect'].items(): effects[k] += v
            if not all(k in m.get('allowed_effects', ['consumption','speed','productivity','pollution','quality']) for k in effects):
                continue
            prod = effects['productivity'] + (db.availability(n,selected).get('productivity_bonus',0) if not theoretical else 0)
            speed = m['crafting_speed'] * max(.2, 1 + effects['speed'])
            seconds = r.get('energy_required', .5) / speed
            active = mw(m['energy_usage']) * max(.2, 1 + effects['consumption']) if m['energy_source']['type']=='electric' else 0
            drain = drain_watts(m) / 1e6
            balance: defaultdict[str, float] = defaultdict(float)
            for e in r.get('ingredients', []): balance[e['name']] -= amount(e)
            for e in r.get('results', []):
                base = amount(e)
                ignored = e.get('ignored_by_productivity', 0)
                balance[e['name']] += base + max(0, base - ignored) * prod
            variants.append(dict(recipe=n, machine=machine, modules=[c for c in config if c], balance=dict(balance),
                                 seconds=seconds, active=active, drain=drain, cost=seconds*(active+drain)))
    voidable = ['nullius-nitrogen', 'nullius-oxygen', 'nullius-compressed-oxygen', 'nullius-brine', 'nullius-wastewater']
    for name in voidable:
        if not theoretical:
            void_recipe = 'nullius-void-'+name.removeprefix('nullius-')
            if void_recipe not in db.recipes or db.availability(void_recipe,selected)['usable_at_stage'] is not True: continue
        variants.append(dict(recipe='discard:'+name, balance={name:-1}, cost=0))
    materials = sorted({n for v in variants for n in v['balance']})
    matrix = np.array([[v['balance'].get(n, 0) for v in variants] for n in materials])
    target = np.array([q if n == 'nullius-methanol' else 0 for n in materials])
    result = linprog([v['cost'] for v in variants], A_eq=matrix, b_eq=target, bounds=(0,None), method='highs')
    if not result.success: raise RuntimeError(result.message)
    assert np.max(np.abs(matrix @ result.x - target)) < 1e-6
    rows, waste = [], {}
    for v, rate in zip(variants, result.x):
        if rate < 1e-9: continue
        if v['recipe'].startswith('discard:'):
            waste[v['recipe'][8:]] = float(rate)
            continue
        equiv = rate * v['seconds']
        count = math.ceil(equiv - 1e-8)
        rows.append({k:v[k] for k in ['recipe','machine','modules']} | dict(crafts_per_second=float(rate),
                     equivalent_machines=equiv, count=count, average_MW=equiv*v['active']+count*v['drain'],
                     peak_MW=count*(v['active']+v['drain'])))
    stage_validation = None if theoretical else db.validate_plan({r['recipe']:r['crafts_per_second'] for r in rows}, selected,
                        sorted({r['machine'] for r in rows}), sorted({m for r in rows for m in r['modules']}))
    return dict(target_per_second=q, route=route, module_policy=modules, machine_tier=tier, module_tier=module_tier,
                theoretical=theoretical, force=selected, stage_validation=stage_validation, excluded_locked_candidates=excluded,
                snapshot_provenance=None if theoretical else db.progress['provenance'],
                water_source=water, continuous_min_MW=float(result.fun), average_MW=sum(r['average_MW'] for r in rows),
                peak_MW=sum(r['peak_MW'] for r in rows), process_machines=sum(r['count'] for r in rows),
                rows=rows, waste=waste, max_balance_error=float(np.max(np.abs(matrix @ result.x-target))),
                boundary='Air and water extraction included; methane synthesis and methanol only. No free steam/hydrogen, mining, biology, beacons, transport, or generation credits. Zero-power voiding counted separately.')

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--theoretical',action='store_true',help='Explicitly ignore save research for historical comparisons')
    ap.add_argument('--force',help='Force from the save snapshot')
    args = ap.parse_args()
    if not args.theoretical:
        db.require_force(args.force)
        cases=[]
        for route in ['normal','pressure']:
            try:
                cases.append(solve(route=route,module_tier=2,force=args.force))
            except RuntimeError as e:
                cases.append({'route':route,'force':args.force,'feasible':False,'reason':str(e)})
        (DATA_DIR / 'methanol-current-stage.json').write_text(json.dumps(cases,indent=2),encoding='utf-8')
        for c in cases: print(c['route'], c.get('average_MW',c.get('reason')), 'stage valid:',c.get('stage_validation',{}).get('valid_at_stage'))
        raise SystemExit(0)
    cases = []
    for route in ['normal','pressure','pressure-direct']:
        for mods in ['none','efficiency','optimize']:
            cases.append(solve(route=route, modules=mods,theoretical=True))
    cases.append(solve(q=10,theoretical=True))
    cases.append(solve(water='fresh',theoretical=True))
    cases.append(solve(modules='save-feedstock',theoretical=True))
    for route in ['normal', 'pressure']:
        cases.append(solve(route=route, module_tier=2,theoretical=True))
    output = DATA_DIR / 'methanol-analysis.json'
    output.write_text(json.dumps(cases, indent=2), encoding='utf-8')
    for c in cases:
        print(c['target_per_second'], c['route'],c['module_policy'],c['water_source'],round(c['average_MW'],4),c['process_machines'])
        if c['module_policy']=='optimize':
            for r in c['rows']: print(' ',r['recipe'],r['modules'],round(r['crafts_per_second'],5),r['count'])
