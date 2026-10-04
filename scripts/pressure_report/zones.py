"""Explicit workshop allocation and audited net interfaces for solved recipe rates.

This allocates the existing solution; it does not claim to optimize geography.
Gas conversion and disposal equipment are distributed to their local workshops.
"""
import re
from collections import defaultdict

from recipe_mcp.database import JSON, amount

WORKSHOPS = [
    dict(id='utility', name='公用工程', factories='取水、过滤、制汽、冷凝、空分、纯制氢',
         placement='靠水和热源；与碳源、化工相邻。制汽、电解、冷凝同区，空气就地获取。'),
    dict(id='carbon', name='碳源与石墨', factories='火山气分离、CO/甲烷合成、石墨、碳截存',
         placement='原气到站后直接进入分离；CO₂ 电解和碳沉积同区，旧加氢线预留旁路。'),
    dict(id='inorganic', name='盐氯与酸碱', factories='盐水电解、氯化氢、酸碱、氨、钠/锂/钙化工',
         placement='氯、氯化氢和酸碱的高耦合工段相邻，制氢副产优先送同区。'),
    dict(id='organic', name='有机与聚合物', factories='烯烃、裂解、溶剂、塑料、橡胶、纤维与润滑剂',
         placement='裂解与烯烃工段共用甲烷回收；与盐氯区相邻，优先内部消化 HCl。'),
    dict(id='mineral', name='矿物与建材', factories='破碎、浮选、磷化工、硅石、玻璃、水泥与回收',
         placement='矿石就近预处理；石料、矿尘与泥浆回收尽量区内消化。'),
    dict(id='metal', name='金属冶炼与轧制', factories='铁、钢、铝、钛、硅冶炼，板/棒/线材铸造',
         placement='冶炼与成形相邻；副产 CO/CO₂ 回碳源，压缩氢独立接入。'),
    dict(id='electronics', name='电子与精密材料', factories='硅晶体、晶圆、芯片、电路、电池、传感器',
         placement='硅提纯气体支线留本地解压；电子中间品短距离互供。'),
    dict(id='assembly', name='机械与设备装配', factories='齿轮、轴承、电机、管泵、物流设备和科研耗材',
         placement='按科研消耗生产设备，不是扩建超市；通用件保留独立出口。'),
    dict(id='science', name='科研终端', factories='六种科研瓶、装箱拆箱、化学瓶容器循环',
         placement='科研线按瓶种并排；化学瓶副产桶在本区拆桶并返回空桶。'),
]

LAYOUTS = {
    'rail': [
        dict(id='utilities', name='① 公用工程区', members=['utility']),
        dict(id='carbon', name='② 碳源与石墨区', members=['carbon']),
        dict(id='chemistry', name='③ 盐氯与有机化工区', members=['inorganic', 'organic']),
        dict(id='metallurgy', name='④ 矿物与冶金区', members=['mineral', 'metal']),
        dict(id='manufacturing', name='⑤ 电子与机械制造区', members=['electronics', 'assembly']),
        dict(id='science', name='⑥ 科研终端区', members=['science']),
    ],
    'bus': [dict(id=w['id'], name=f'{i+1:02} · {w["name"]}', members=[w['id']]) for i, w in enumerate(WORKSHOPS)],
}


def normalized(value: str) -> str:
    return re.sub(r'^(?:boxed-|unbox-|box-)', '', re.sub(r'^(?:nullius-|lambent-nil-)', '', value))


def classify(name: str, recipe: JSON) -> str:
    n = normalized(name)
    cat = recipe.get('category', 'crafting')
    if 'pack' in n and re.search(r'(geology|climatology|mechanical|electrical|chemical|physics)-pack', n):
        return 'science'
    if cat in ['nullius-barrel', 'nullius-unbarrel']:
        return 'science'
    if n in ['air', 'seawater', 'freshwater', 'condensation', 'residual-gas', 'sludge-disposal-2'] or any(x in n for x in [
        'boiling-', 'desalination-', 'water-filtration', 'water-electrolysis', 'steam-electrolysis',
        'air-separation', 'residual-separation', 'trace-separation', 'salination']):
        return 'utility'
    if any(x in n for x in ['volcanic', 'carbon-deposition', 'carbon-dioxide-electrolysis', 'pressure-carbon-monoxide', 'pressure-methane', 'graphite']):
        return 'carbon'
    if any(x in n for x in ['saline-electrolysis', 'brine-electrolysis', 'acid-hydrochloric', 'acid-nitric', 'acid-sulfuric',
        'hydrogen-chloride', 'ammonia', 'caustic-solution', 'calcium-chloride', 'sodium', 'soda-ash', 'lithium', 'eutectic-salt']) or n in ['calcium', 'salt']:
        return 'inorganic'
    if any(x in n for x in ['alkene', 'acrylic', 'acrylonitrile', 'ethylene', 'propene', 'butadiene', 'epoxy', 'glycerol',
        'lubricant', 'methanol', 'phosphate-ester', 'pressure-bpa', 'plastic', 'rubber', 'solvent', 'styrene', 'chelating',
        'carbon-fiber', 'carbon-composite', 'textile', 'explosive', 'pressure-filter', 'insulation']) or n == 'ech' or name == 'cliff-explosives':
        return 'organic'
    if any(x in n for x in ['polycrystalline', 'monocrystalline', 'wafer', 'processor', 'circuit', 'capacitor', 'battery',
        'graphene', 'sensor', 'antenna', 'electron-gun', 'module-', 'optical-cable', 'insulated-wire', 'red-wire', 'green-wire',
        'levitation-field', 'telekinesis-field', 'beacon']):
        return 'electronics'
    if any(x in n for x in ['crushed-', 'flotation', 'phosphor', 'apatite', 'nonmetal-recovery', 'calcium-hexaboride',
        'silica', 'glass', 'cement', 'concrete', 'mortar', 'sand', 'gravel', 'gypsum', 'slag-', 'dust-', 'sludge-dehydration',
        'limestone', 'mineral-dust', 'stone', 'boron', 'refractory', 'ceramic']) or n in ['lime', 'iron-ore', 'phosphorite', 'acid-boric'] or name in ['stone-brick', 'refined-concrete']:
        return 'mineral'
    if any(x in n for x in ['aluminum-', 'alumina', 'bauxite', 'iron-oxide', 'rutile', 'titanium-', 'silicon-ingot', 'thermite']) or re.search(r'^(iron|steel)-(ingot|plate|rod|sheet|wire|beam|cable)', n):
        return 'metal'
    if cat in ['basic-chemistry', 'distillation', 'nullius-electrolysis', 'bulk-smelting', 'vacuum-chemistry', 'ore-flotation', 'pressure-boiling', 'compression', 'decompression']:
        raise ValueError(f'Unassigned chemical process: {name} ({cat})')
    return 'assembly'


def conversion(recipe: JSON) -> bool:
    ins, outs = recipe.get('ingredients', []), recipe.get('results', [])
    return (recipe.get('category') in ['compression', 'decompression'] and len(ins) == len(outs) == 1
            and ins[0]['type'] == outs[0]['type'] == 'fluid')


def is_void(recipe: JSON) -> bool:
    return recipe.get('category') in ['nullius-liquid-void', 'nullius-gas-void']


def build_zones(scenarios: list[JSON], raw: JSON) -> JSON:
    recipes = raw['recipe']
    definitions = {}
    output = {}
    for s in scenarios:
        allocations = defaultdict(list)
        pending_conversions, pending_voids = [], []
        for op in s['active_recipes']:
            r = recipes[op['recipe']]
            definitions[op['recipe']] = {key: [dict(material=e['type'] + ':' + e['name'], amount=amount(e)) for e in r.get(key, []) if amount(e)] for key in ['ingredients', 'results']}
            definitions[op['recipe']]['category'] = r.get('category', 'crafting')
            if conversion(r):
                pending_conversions.append(op)
            elif is_void(r):
                pending_voids.append(op)
            else:
                allocations[classify(op['recipe'], r)].append(dict(op, allocation='whole'))

        def gross(zone: str, material: str, direction: str, processes_only: bool = False) -> float:
            return sum(op['crafts_per_second'] * e['amount'] for op in allocations[zone]
                       if not processes_only or op['allocation'] == 'whole'
                       for e in definitions[op['recipe']][direction] if e['material'] == material)

        def distribute(op: JSON, weights: dict[str, float], why: str) -> None:
            total = sum(weights.values())
            assert total > 1e-8, (s['name'], op['recipe'])
            for zone, weight in weights.items():
                if weight <= 1e-9:
                    continue
                split = dict(op, allocation=why)
                for k in ['crafts_per_second', 'machines', 'MW']:
                    split[k] *= weight / total
                allocations[zone].append(split)

        # The pressure solver compresses all ordinary-gas production and
        # decompresses at ordinary-gas consumers. Keep those devices local.
        for op in pending_conversions:
            r = recipes[op['recipe']]
            compress = r['category'] == 'compression'
            material = 'fluid:' + r['ingredients' if compress else 'results'][0]['name']
            direction = 'results' if compress else 'ingredients'
            weights = {w['id']: gross(w['id'], material, direction, processes_only=True) for w in WORKSHOPS}
            distribute(op, weights, 'local-compression' if compress else 'local-decompression')

        # Place disposal at net-surplus workshops, so unwanted byproducts do not
        # have to take a train merely to be vented. Sum of recipe rates is unchanged.
        for op in pending_voids:
            material = definitions[op['recipe']]['ingredients'][0]['material']
            weights = {w['id']: max(0, gross(w['id'], material, 'results') - gross(w['id'], material, 'ingredients')) for w in WORKSHOPS}
            distribute(op, weights, 'local-disposal')

        # Audit recipe, equipment and power conservation through every split.
        for original in s['active_recipes']:
            for k in ['crafts_per_second', 'machines', 'MW']:
                actual = sum(op[k] for ops in allocations.values() for op in ops if op['recipe'] == original['recipe'])
                assert abs(actual - original[k]) < 1e-6, (s['name'], original['recipe'], k)
        scenario_layouts = {}
        for layout, groups in LAYOUTS.items():
            zones: list[JSON] = []
            for group in groups:
                ops = [dict(op, workshop=member) for member in group['members'] for op in allocations[member]]
                net: defaultdict[str, float] = defaultdict(float)
                ins: defaultdict[str, float] = defaultdict(float)
                outs: defaultdict[str, float] = defaultdict(float)
                disposal: defaultdict[str, float] = defaultdict(float)
                for op in ops:
                    definition = definitions[op['recipe']]
                    for key, sign, bucket in [('ingredients', -1, ins), ('results', 1, outs)]:
                        for e in definition[key]:
                            qty = e['amount'] * op['crafts_per_second']
                            net[e['material']] += sign * qty
                            bucket[e['material']] += qty
                            if op['allocation'] == 'local-disposal' and key == 'ingredients':
                                disposal[e['material']] += qty
                # Ordinary-gas conversions must remain local to each workshop;
                # otherwise false cross-zone gas interfaces appear despite a
                # globally balanced solution.
                for ordinary in s['gas_equivalence']:
                    assert abs(net.get('fluid:' + ordinary, 0)) < 1e-6, (s['name'], group['id'], ordinary)
                zones.append(dict(group, recipes=ops, net={k: v for k, v in net.items() if abs(v) > 1e-6},
                                  internal_reuse={k: min(v, outs[k]) for k, v in ins.items() if min(v, outs[k]) > 1e-6},
                                  disposal=dict(disposal)))
            total_net: defaultdict[str, float] = defaultdict(float)
            for z in zones:
                for k, v in z['net'].items():
                    total_net[k] += v
            for k, v in s['supplies'].items():
                total_net[k] += v
            for pack in ['geology', 'climatology', 'mechanical', 'electrical', 'chemical', 'physics']:
                total_net['item:nullius-' + pack + '-pack'] -= s['target_each_pack_per_second']
            residual = max(map(abs, total_net.values()), default=0)
            assert residual < 1e-4, (s['name'], layout, residual)
            # Suppliers/consumers are a material pool; no invented origin-destination allocation.
            materials = []
            for k in sorted({k for z in zones for k in z['net']} | set(s['supplies'])):
                exporters = [{'zone': z['id'], 'rate': z['net'][k]} for z in zones if z['net'].get(k, 0) > 1e-6]
                importers = [{'zone': z['id'], 'rate': -z['net'][k]} for z in zones if z['net'].get(k, 0) < -1e-6]
                materials.append(dict(material=k, exporters=exporters, importers=importers,
                                      external_supply=s['supplies'].get(k, 0),
                                      total_import=sum(e['rate'] for e in importers)))
            scenario_layouts[layout] = dict(zones=zones, materials=materials, balance_residual=residual)
        output[s['name']] = scenario_layouts
    return dict(workshops=WORKSHOPS, scenarios=output, definitions=definitions)
