"""Planner regression: closed-form matrix solve, module/beacon effects, LP route choice, real-data plans."""
from database import Database
from planner import plan, machine_stats, production_matrix


def fluid(n, a): return {'type': 'fluid', 'name': n, 'amount': a}
def item(n, a): return {'type': 'item', 'name': n, 'amount': a}


RAW = {
    'recipe': {
        'aop': {'name': 'aop', 'category': 'oil', 'energy_required': 5, 'allow_productivity': True,
                'ingredients': [fluid('crude', 100)], 'results': [fluid('heavy', 25), fluid('light', 45), fluid('petro', 55)]},
        'hc': {'name': 'hc', 'category': 'chem', 'energy_required': 2, 'ingredients': [fluid('heavy', 40)], 'results': [fluid('light', 30)]},
        'lc': {'name': 'lc', 'category': 'chem', 'energy_required': 2, 'ingredients': [fluid('light', 30)], 'results': [fluid('petro', 20)]},
        'gear-a': {'name': 'gear-a', 'category': 'crafting', 'energy_required': 1, 'ingredients': [item('plate', 2)], 'results': [item('gear', 1)]},
        'gear-b': {'name': 'gear-b', 'category': 'crafting', 'energy_required': 4, 'ingredients': [item('plate', 1)], 'results': [item('gear', 1)]},
    },
    'fluid': {n: {'name': n} for n in ['crude', 'heavy', 'light', 'petro']},
    'assembling-machine': {
        'refinery': {'crafting_speed': 1, 'crafting_categories': ['oil'], 'energy_usage': '420kW', 'module_slots': 2,
                     'allowed_effects': ['speed', 'productivity', 'consumption', 'pollution'], 'energy_source': {'type': 'electric', 'drain': '0W'},
                     'fluid_boxes': [{'production_type': 'input'}] + [{'production_type': 'output'}] * 3},
        'plant': {'crafting_speed': 1, 'crafting_categories': ['chem'], 'energy_usage': '210kW', 'energy_source': {'type': 'electric'},
                  'module_slots': 1, 'allowed_effects': ['speed', 'productivity', 'consumption'],
                  'fluid_boxes': [{'production_type': 'input'}, {'production_type': 'output'}]},
        'asm': {'crafting_speed': 1, 'crafting_categories': ['crafting'], 'energy_usage': '75kW', 'energy_source': {'type': 'electric'}},
    },
    'module': {'spd': {'effect': {'speed': 0.5, 'consumption': 0.7}}, 'prod': {'effect': {'productivity': 0.1, 'speed': -0.15}}},
    'beacon': {'bcn': {'distribution_effectivity': 0.5, 'profile': [1, 0.7071], 'module_slots': 2,
                       'allowed_effects': ['speed', 'consumption'], 'energy_usage': '480kW'}},
    'technology': {},
}


def close(a, b, tol=1e-9): assert abs(a - b) <= tol * max(1, abs(b)), (a, b)


def expect_error(fn, *words):
    try:
        fn()
    except ValueError as e:
        assert any(w in str(e) for w in words), e
        return str(e)
    raise AssertionError('expected ValueError')


def synthetic():
    db = Database(raw=RAW)
    lines = ['aop', 'hc', 'lc']
    r = plan(db, {'petro': 100}, lines, solver='matrix', validate_stage=False)
    a = 100 / 97.5
    crafts = {l['recipe']: l['crafts'] for l in r['lines']}
    close(crafts['aop'], a); close(crafts['hc'], .625 * a); close(crafts['lc'], 2.125 * a)
    close(r['imports']['fluid:crude'], 100 * a)
    close({l['recipe']: l['machines'] for l in r['lines']}['aop'], 5 * a)
    close(r['totals']['power_MW'], 5 * a * .42 + 2 * .625 * a * .217 + 2 * 2.125 * a * .217)  # drain = usage/30
    assert r['max_balance_error'] < 1e-9 and r['matrix']['degrees_of_freedom'] == 0
    lp = plan(db, {'petro': 100}, lines, validate_stage=False)
    close({l['recipe']: l['crafts'] for l in lp['lines']}['aop'], a, 1e-7)
    m = plan(db, {'petro': 6000}, lines, solver='matrix', per='minute', validate_stage=False)
    close({l['recipe']: l['crafts'] for l in m['lines']}['aop'], a * 60)
    # Without light cracking, light oil becomes a byproduct and is a surplus unknown.
    r2 = plan(db, {'petro': 55}, ['aop', 'hc'], solver='matrix', validate_stage=False)
    close(r2['surplus']['fluid:light'], 45 + 25 / 40 * 30)
    expect_error(lambda: plan(db, {'petro': 55, 'light': 10}, ['aop'], solver='matrix', validate_stage=False), 'cannot balance')
    expect_error(lambda: plan(db, {'gear': 1}, ['gear-a', 'gear-b'], solver='matrix', validate_stage=False), 'underdetermined')
    pm = production_matrix(db, ['gear-a', 'gear-b'], {'gear': 1})
    assert pm['square_system']['degrees_of_freedom'] == 1 and pm['matrix'] == [[1, 1], [-2, -1]], pm
    g = plan(db, {'gear': 1}, ['gear-a', 'gear-b'], validate_stage=False)
    assert [l['recipe'] for l in g['lines']] == ['gear-a']
    g = plan(db, {'gear': 1}, ['gear-a', 'gear-b'], objective='imports', validate_stage=False)
    assert [l['recipe'] for l in g['lines']] == ['gear-b'], g['lines']
    close(g['shadow_prices']['item:gear'], 1 + 4e-3, 1e-6)  # 1 plate import + 4 machine-seconds * 1e-3
    g = plan(db, {'gear': 1}, [{'recipe': 'gear-b', 'fixed_machines': 2}, 'gear-a'], solver='matrix', validate_stage=False)
    close({l['recipe']: l['crafts'] for l in g['lines']}['gear-a'], .5)
    s = machine_stats(db, 'aop', modules=['spd', 'prod'], beacons=[{'beacon': 'bcn', 'count': 2, 'modules': ['spd', 'spd']}], validate_stage=False)
    close(s['speed_multiplier'], 1 + .5 - .15 + 2 * 2 * .5 * .5 * .7071)
    close(s['productivity'], .1)
    close(s['per_machine']['fluid:petro'], 55 * 1.1 * s['speed_multiplier'] / 5)
    close(s['consumption_multiplier'], 1 + .7 + 2 * 2 * .7 * .5 * .7071)
    close(s['power_per_machine_MW']['beacons'], .96)
    expect_error(lambda: machine_stats(db, 'hc', machine='plant', modules=['prod'], validate_stage=False), 'not allowed by recipe')
    expect_error(lambda: machine_stats(db, 'aop', beacons=[{'beacon': 'bcn', 'modules': ['prod']}], validate_stage=False), 'not allowed by entity')
    # Discovery must revisit a recipe first reached through an item it only uses as a catalyst.
    loop_raw = {
        'recipe': {
            'mk': {'name': 'mk', 'category': 'crafting', 'ingredients': [item('cat', 1), item('b', 1)], 'results': [item('prod', 1)]},
            'loop': {'name': 'loop', 'category': 'crafting', 'ingredients': [item('cat', 1)], 'results': [item('cat', 1), item('b', 1)]},
        },
        'assembling-machine': {'asm': {'crafting_speed': 1, 'crafting_categories': ['crafting'], 'energy_usage': '75kW',
                                       'energy_source': {'type': 'electric'}}},
        'technology': {},
    }
    r = plan(Database(raw=loop_raw), {'prod': 1}, forbid_imports=['b'], validate_stage=False)
    crafts = {l['recipe']: l['crafts'] for l in r['lines']}
    close(crafts['mk'], 1); close(crafts['loop'], 1)
    print('PASS: planner synthetic math (matrix closed form, roles, diagnostics, LP routes, shadow prices, fixed lines, modules, beacons)')


def real_data():
    real = Database()
    force = 'faction-a632079'
    out = plan(real, {'nullius-methanol': 10}, force=force)
    assert out['max_balance_error'] < 1e-6 and out['lines'], out
    for i in out['items']:
        net = i['produced'] + i.get('import', 0) - i['consumed'] - i.get('surplus', 0) - i['target']
        assert abs(net) < 1e-6, i
    pinned = plan(real, {'nullius-methanol': 10}, out['lines_for_matrix'], force=force)
    close(pinned['totals']['machines'], out['totals']['machines'], 1e-6)
    try:
        mx = plan(real, {'nullius-methanol': 10}, out['lines_for_matrix'], solver='matrix', force=force, **out['matrix_args'])
    except ValueError as e:
        raise AssertionError(f'LP plan must replay exactly in the matrix solver: {e}')
    close(mx['totals']['machines'], out['totals']['machines'], 1e-6)
    expect_error(lambda: plan(real, {'nullius-methanol': 1}, [{'recipe': 'nullius-methanol', 'machine': 'nullius-chemical-plant-3'}], force=force), 'Stage-locked')
    st = machine_stats(real, 'nullius-methanol', rate=10, force=force)
    assert st['valid_at_stage'] and st['for_rate']['machines'] > 0
    print('PASS: real data methanol 10/s:', len(out['lines']), 'lines of', out['candidate_lines'], 'candidates,',
          round(out['totals']['machines'], 2), 'machines,', round(out['totals']['power_MW'], 2), 'MW; imports',
          {k: round(v, 3) for k, v in out['imports'].items()}, 'surplus', {k: round(v, 3) for k, v in out['surplus'].items()})
    for l in out['lines']: print('  ', l['recipe'], l['machine'], round(l['machines'], 3))


if __name__ == '__main__':
    synthetic()
    real_data()
