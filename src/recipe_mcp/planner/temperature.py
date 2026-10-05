"""Split constrained fluids into temperature pools connected by zero-cost matching flows."""

import math
from collections import defaultdict
from collections.abc import Sequence

from .energy import channel_recipe
from .model import Planner
from .schema import Line, TemperatureRange


def base_key(key: str) -> str:
    return key.split('@', 1)[0].split('[', 1)[0] if key.startswith('fluid:') else key


def pool(key: str, temperature: float) -> str:
    return f'{key}@{temperature:g}'


def bounds(need: TemperatureRange) -> tuple[float, float]:
    if need.temperature is not None:
        return need.temperature, need.temperature
    return (
        need.minimum_temperature if need.minimum_temperature is not None else -math.inf,
        need.maximum_temperature if need.maximum_temperature is not None else math.inf,
    )


def split_temperatures(p: Planner, lines: Sequence[Line], endpoints: set[str]) -> list[Line]:
    recipes = {
        r.id: (
            channel_recipe(p.recipe_view(r.recipe), r.machine)
            if p.energy_mode == 'balance'
            else p.recipe_view(r.recipe)
        )
        for r in lines
        if r.energy_type != 'producer'
    }
    active = any(r.ingredient_temperatures for r in lines) or any(
        e.temperature is not None for recipe in recipes.values() for e in recipe.results if e.type == 'fluid'
    )
    active |= any(r.temperature is not None for r in lines)
    if not active:
        return list(lines)
    outputs: defaultdict[str, set[float]] = defaultdict(set)
    needs: defaultdict[str, dict[str, tuple[float, float]]] = defaultdict(dict)
    rows = [r.model_copy(deep=True) for r in lines]
    for row in rows:
        recipe = recipes.get(row.id)
        if recipe is not None:
            for entry in [*recipe.ingredients, *recipe.results]:
                if entry.type == 'fluid':
                    row.balance.pop(entry.key, None)
            for entry in recipe.results:
                if entry.type != 'fluid':
                    continue
                temp = entry.temperature
                if temp is None:
                    temp = p.raw.get('fluid', {}).get(entry.name.split('~', 1)[0], {}).get('default_temperature', 15)
                outputs[entry.key].add(temp)
                key = pool(entry.key, temp)
                row.balance[key] = row.balance.get(key, 0) + entry.output(row.productivity, row.productivity_model)
            for entry in recipe.ingredients:
                if entry.type != 'fluid':
                    continue
                lo, hi = bounds(TemperatureRange.model_validate(entry.model_dump()))
                key = entry.key if (lo, hi) == (-math.inf, math.inf) else f'{entry.key}[{lo:g},{hi:g}]'
                needs[entry.key][key] = (lo, hi)
                row.balance[key] = row.balance.get(key, 0) - entry.average
        else:
            for key, value in list(row.balance.items()):
                if not key.startswith('fluid:') or value >= 0:
                    continue
                if row.temperature is not None:
                    target = f'{key}[{row.temperature:g},{row.temperature:g}]'
                    needs[key][target] = (row.temperature, row.temperature)
                    del row.balance[key]
                    row.balance[target] = value
                else:
                    needs[key][key] = (-math.inf, math.inf)
    for key in endpoints:
        if key in outputs:
            needs[key][key] = (-math.inf, math.inf)
    for key, temperatures in sorted(outputs.items()):
        for temp in sorted(temperatures):
            for target, (lo, hi) in sorted(needs[key].items()):
                if not lo <= temp <= hi:
                    continue
                source = pool(key, temp)
                rows.append(
                    Line(
                        id=f'match:{source}->{target}',
                        recipe='temperature-match',
                        machine='',
                        machine_type='temperature-match',
                        modules=[],
                        beacons=[],
                        speed_multiplier=1,
                        productivity=0,
                        consumption_multiplier=1,
                        pollution_multiplier=1,
                        research_productivity=0,
                        crafts_per_machine=1,
                        energy_type='void',
                        active_W=0,
                        drain_W=0,
                        beacon_W=0,
                        pollution_per_minute=0,
                        balance={source: -1, target: 1},
                        temperatures={},
                        ingredient_temperatures={},
                        fixed_machines=None,
                        max_machines=None,
                        cost_weight=0,
                        blocked=[],
                    )
                )
    return rows
