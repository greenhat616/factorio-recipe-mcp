"""Balance the six pre-astronomy science packs using a compatible save snapshot.

Continuous, no-module reference plan minimizing process electricity. Mining,
transport, power generation and mall production are outside the boundary.
"""

import json
from collections import defaultdict

import click
import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csc_matrix, eye, hstack

from recipe_mcp.database import JSON, Database, amount
from recipe_mcp.paths import DATA_DIR
from recipe_mcp.planner import drain_watts, watts


def process_MW(seconds_per_craft: float, machine: JSON) -> float:
    seconds = seconds_per_craft / machine.get('crafting_speed', 1)
    return seconds * (watts(machine.get('energy_usage', '0W')) + drain_watts(machine)) / 1e6


def analyze(force: str | None, rate: float, power: bool = True) -> JSON:
    db = Database()
    force = db.require_force(force)
    packs = [
        'nullius-' + x + '-pack' for x in ['geology', 'climatology', 'mechanical', 'electrical', 'chemical', 'physics']
    ]
    machine_choices = defaultdict(list)
    verified_barrel_pumps = {}
    for kind in ['assembling-machine', 'furnace', 'rocket-silo']:
        for name, machine in db.raw.get(kind, {}).items():
            stage = db.machine_stage(name, force)
            # MCP's broad packaging-name filter also catches barrel-pump machines.
            # Verify their actual manufacturing recipe directly, without assuming
            # that a missing equipment mapping means the machine is available.
            if stage['buildable_at_stage'] is None and name.startswith('nullius-barrel-pump-') and name in db.recipes:
                evidence = db.recipe(name, force)
                if any(e['name'] == name and e['type'] == 'item' for e in evidence.get('results', [])):
                    stage = dict(
                        machine=name,
                        buildable_at_stage=evidence['availability']['usable_at_stage'],
                        manufacturing_recipes=[name],
                        direct_recipe_evidence=evidence['availability'],
                    )
                    verified_barrel_pumps[name] = stage
            if stage['buildable_at_stage'] is not True:
                continue
            if machine.get('energy_source', {}).get('type') not in ['electric', 'void']:
                continue
            for category in machine.get('crafting_categories', []):
                machine_choices[category].append((name, machine))

    columns = []
    for name, recipe in sorted(db.recipes.items()):
        if db.virtual(name) or db.availability(name, force)['usable_at_stage'] is not True:
            continue
        if not recipe.get('results'):
            continue
        category = recipe.get('category', 'crafting')
        # Steam temperatures and electrical generation are deliberately excluded.
        if category in ['turbine-open', 'turbine-closed', 'nullius-power-sink']:
            continue
        options = [p for p in machine_choices[category] if not p[1].get('fixed_recipe') or p[1]['fixed_recipe'] == name]
        if not options:
            continue
        energy = recipe.get('energy_required', 0.5)
        if power:
            machine_name, machine = min(options, key=lambda p: process_MW(energy, p[1]))
        else:
            machine_name, machine = max(options, key=lambda p: p[1].get('crafting_speed', 1))
        balance: defaultdict[str, float] = defaultdict(float)
        for key, sign in [('ingredients', -1), ('results', 1)]:
            for entry in recipe.get(key, []):
                balance[entry['type'] + ':' + entry['name']] += sign * amount(entry)
        # Tiny positive tie-break avoids arbitrary zero-cost circulation.
        objective = process_MW(energy, machine) if power else energy / machine.get('crafting_speed', 1)
        columns.append(
            dict(name=name, machine=machine_name, balance=dict(balance), cost=max(objective, 1e-5), recipe=recipe)
        )

    # Only resources that naturally generate in this mod set are external inputs.
    resources = []
    for name, resource in sorted(db.raw.get('resource', {}).items()):
        if name.startswith('creative-') or not resource.get('autoplace'):
            continue
        mining = resource.get('minable', {})
        outputs = mining.get('results') or [
            {'type': 'item', 'name': mining['result'], 'amount': mining.get('count', 1)}
        ]
        for entry in outputs:
            material = entry['type'] + ':' + entry['name']
            resources.append(material)
            # Procurement is a boundary, not an estimate of mining energy.
            columns.append(dict(name='extract:' + name, balance={material: 1}, cost=1e-5))

    materials = sorted({n for c in columns for n in c['balance']} | {'item:' + n for n in packs})
    index = {n: i for i, n in enumerate(materials)}
    rr, cc, vv = [], [], []
    for j, column in enumerate(columns):
        for material, value in column['balance'].items():
            if value:
                rr.append(index[material])
                cc.append(j)
                vv.append(value)
    matrix = csc_matrix((vv, (rr, cc)), shape=(len(materials), len(columns)))
    target = np.array([rate if n in {'item:' + p for p in packs} else 0 for n in materials])
    result = linprog([c['cost'] for c in columns], A_eq=matrix, b_eq=target, bounds=(0, None), method='highs')
    if not result.success:
        diagnostic = linprog(
            [1e-9] * len(columns) + [1e6 if n.endswith('-pack') else 1 for n in materials] + [1] * len(materials),
            A_eq=hstack([matrix, eye(len(materials)), -eye(len(materials))]),
            b_eq=target,
            bounds=(0, None),
            method='highs',
        )
        shortages = [
            (n, float(diagnostic.x[len(columns) + i]))
            for i, n in enumerate(materials)
            if diagnostic.success and diagnostic.x[len(columns) + i] > 1e-6
        ]
        raise RuntimeError(result.message + '; missing inputs: ' + repr(shortages))
    error = float(np.max(np.abs(matrix @ result.x - target)))
    if error > 1e-5:
        raise RuntimeError(f'Balance residual: {error}')

    gas_equivalence = {}
    for recipe in db.recipes.values():
        if recipe.get('category') == 'compression':
            inputs, outputs = recipe.get('ingredients', []), recipe.get('results', [])
            if len(inputs) == len(outputs) == 1:
                gas_equivalence[outputs[0]['name']] = (inputs[0]['name'], amount(inputs[0]) / amount(outputs[0]))
    demand: defaultdict[str, float] = defaultdict(float)
    produced: defaultdict[str, float] = defaultdict(float)
    voided: defaultdict[str, float] = defaultdict(float)
    procured_fluids: defaultdict[str, float] = defaultdict(float)
    physical_input: defaultdict[str, float] = defaultdict(float)
    physical_output: defaultdict[str, float] = defaultdict(float)
    consumers: defaultdict[str, list[JSON]] = defaultdict(list)
    active, rates = [], {}
    for column, crafts in zip(columns, result.x, strict=True):
        if crafts < 1e-8:
            continue
        if column['name'].startswith('extract:'):
            active.append(dict(recipe=column['name'], units_per_second=float(crafts)))
            for material, quantity in column['balance'].items():
                if material.startswith('fluid:'):
                    procured_fluids[material[6:]] += quantity * crafts
            continue
        recipe, name = column['recipe'], column['name']
        rates[name] = float(crafts)
        active.append(dict(recipe=name, machine=column['machine'], crafts_per_second=float(crafts)))
        category = recipe.get('category', 'crafting')
        for key, inventory in [('ingredients', physical_input), ('results', physical_output)]:
            for e in recipe.get(key, []):
                if e['type'] == 'fluid':
                    inventory[e['name']] += amount(e) * crafts
        # Compression/decompression is transport handling, not a second chemical use.
        if category in ['compression', 'decompression']:
            continue
        disposal = category in ['nullius-gas-void', 'nullius-liquid-void']
        for key, inventory in [('ingredients', voided if disposal else demand), ('results', produced)]:
            for e in recipe.get(key, []):
                if e['type'] != 'fluid':
                    continue
                fluid, multiplier = gas_equivalence.get(e['name'], (e['name'], 1))
                quantity = amount(e) * crafts * multiplier
                inventory[fluid] += quantity
                if key == 'ingredients' and not disposal:
                    consumers[fluid].append(dict(recipe=name, per_second=float(quantity)))

    used_machines = sorted({r['machine'] for r in active if 'machine' in r})
    validation = db.validate_plan(rates, force, [n for n in used_machines if n not in verified_barrel_pumps])
    validation['equipment'] += [verified_barrel_pumps[n] for n in used_machines if n in verified_barrel_pumps]
    validation['valid_at_stage'] = validation['valid_at_stage'] and all(
        e['buildable_at_stage'] is True for e in validation['equipment']
    )
    if not validation['valid_at_stage']:
        raise RuntimeError('Stage validation failed')
    # Independently recalculate the base material balance with the MCP database.
    recalculated = db.balance(rates)
    for row in active:
        if row['recipe'].startswith('extract:'):
            column = next(c for c in columns if c['name'] == row['recipe'])
            for material, quantity in column['balance'].items():
                recalculated[material] = recalculated.get(material, 0) + quantity * row['units_per_second']
    independent_error = max(abs(recalculated.get(n, 0) - target[i]) for i, n in enumerate(materials))
    fluids = [
        dict(
            fluid=n,
            consumption_per_second=float(v),
            production_per_second=float(produced[n]),
            externally_procured_per_second=float(procured_fluids[n]),
            discarded_per_second=float(voided[n]),
            largest_consumers=sorted(consumers[n], key=lambda x: -x['per_second'])[:6],
        )
        for n, v in sorted(demand.items(), key=lambda x: -x[1])
        if v > 1e-6
    ]
    return dict(
        force=force,
        each_pack_per_second=rate,
        packs=packs,
        modules='none',
        objective='minimum continuous process electricity' if power else 'minimum continuous machine count',
        snapshot_provenance=db.progress['provenance'],
        prototype_raw_sha256=db.raw_sha256,
        reference_process_MW=float(result.fun) if power else None,
        balance_residual=error,
        independent_balance_residual=float(independent_error),
        stage_validation=validation,
        imported_resource_materials=resources,
        fluid_consumption=fluids,
        fluid_physical_inputs=dict(sorted(physical_input.items(), key=lambda x: -x[1])),
        fluid_physical_outputs=dict(sorted(physical_output.items(), key=lambda x: -x[1])),
        gas_equivalence=gas_equivalence,
        active_recipes=active,
        boundary='Six science packs at equal rates, no modules/beacons/research productivity; naturally generated ores and volcanic gas are procurement inputs. Full co-product balance and real enabled disposal recipes. Mining, transport, generation and mall excluded. Normalized gas demand excludes compression/decompression and voiding; actual chemical/boiling/barreling inputs include recovered fluid throughput. Not a blueprint or global discrete layout optimum.',
    )


@click.command(help=__doc__)
@click.option('--force', default='faction-a632079', show_default=True, help='Force from the save snapshot.')
@click.option('--rate', type=float, default=150, show_default=True, help='Each science pack per second.')
@click.option('--machines', is_flag=True, help='Minimise machine count instead of process electricity.')
def main(force: str, rate: float, machines: bool) -> None:
    report = analyze(force, rate, not machines)
    output = DATA_DIR / ('science-fluid-machine-reference.json' if machines else 'science-fluid-reference.json')
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(
        json.dumps(
            dict(
                output=str(output),
                stage_valid=report['stage_validation']['valid_at_stage'],
                residual=report['independent_balance_residual'],
                fluid_consumption=report['fluid_consumption'],
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == '__main__':
    main()
