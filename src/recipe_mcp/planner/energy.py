"""Energy rates use MW in every reporting period; material fuels keep ordinary item units."""

import math
import re
from typing import TYPE_CHECKING

from ..models import JSON, entries
from .schema import Defaults, Line, LineSpec, RecipeView

if TYPE_CHECKING:
    from .model import Planner

ENERGY_KEYS = {'energy:electric', 'energy:heat'}
PREFIXES = {
    'generate': ('generator', 'burner-generator'),
    'solar': ('solar-panel',),
    'wind': ('electric-energy-interface',),
    'heat': ('reactor',),
}


def rate_factor(key: str, factor: float) -> float:
    return 1.0 if key.startswith('energy:') else factor


def quantity(value: str | float | None) -> float:
    if isinstance(value, (float, int)):
        return float(value)
    match = re.fullmatch(r'\s*([0-9.eE+-]+)\s*([kMGTP]?)[JW]\s*', value or '0J')
    if not match:
        raise ValueError(f'Unrecognised energy value: {value}')
    return float(match[1]) * {'': 1, 'k': 1e3, 'M': 1e6, 'G': 1e9, 'T': 1e12, 'P': 1e15}[match[2]]


def check_factors(mode: str, solar: float, wind: float | None) -> None:
    if mode not in ('report', 'balance'):
        raise ValueError('energy_mode must be report or balance')
    if not math.isfinite(solar) or solar <= 0:
        raise ValueError('solar_factor must be finite and positive')
    if wind is not None and (not math.isfinite(wind) or wind <= 0):
        raise ValueError('wind_factor must be finite and positive')


def pick_fuel(p: 'Planner', source: JSON, spec: LineSpec, defaults: Defaults) -> tuple[str, JSON]:
    fluid = source.get('type') == 'fluid'
    filter_name = source.get('fluid_box', {}).get('filter')
    names = [spec.fuel] if spec.fuel else defaults.fuel or ([filter_name] if fluid and filter_name else [])
    categories = source.get('fuel_categories', [source.get('fuel_category', 'chemical')])
    errors = []
    for name in names:
        key = p.key(name)
        plain = key.split(':', 1)[1]
        proto = (
            p.raw.get('fluid', {}).get(plain)
            if fluid
            else next(
                (group[plain] for group in p.raw.values() if plain in group and group[plain].get('fuel_value')), None
            )
        )
        if (
            not proto
            or quantity(proto.get('fuel_value')) <= 0
            or (fluid and (not key.startswith('fluid:') or filter_name and plain != filter_name))
            or (not fluid and (key.startswith('fluid:') or proto.get('fuel_category', 'chemical') not in categories))
        ):
            errors.append(f'{name}: incompatible fuel category, filter or heat value')
            continue
        if p.validate and p.buildable(item=plain) is not True:
            errors.append(f'{name}: fuel is not buildable at this stage')
            continue
        return key, proto
    raise ValueError('Choose compatible fuel with line.fuel or defaults.fuel: ' + '; '.join(errors))


def fuel_flows(
    p: 'Planner', source: JSON, spec: LineSpec, defaults: Defaults, power: float
) -> tuple[str, float, dict[str, float]]:
    key, proto = pick_fuel(p, source, spec, defaults)
    efficiency = float(source.get('effectivity', 1))
    if efficiency <= 0:
        raise ValueError('Fuel energy effectivity must be positive')
    rate = power / (quantity(proto['fuel_value']) * efficiency)
    flow = {key: -rate}
    if proto.get('burnt_result'):
        flow['item:' + proto['burnt_result']] = rate
    return key, rate, flow


def apply_energy(p: 'Planner', row: Line, source: JSON, spec: LineSpec, defaults: Defaults) -> Line:
    flows: dict[str, float] = {}
    if row.power_W:
        flows['energy:electric'] = -row.power_W / 1e6
    if row.energy_type == 'heat':
        flows['energy:heat'] = -row.active_W / 1e6
    burning = row.energy_type == 'burner' or row.energy_type == 'fluid' and source.get('burns_fluid', False)
    if burning and (p.energy_mode == 'balance' or spec.fuel or defaults.fuel):
        row.fuel, row.fuel_per_machine, fuel = fuel_flows(p, source, spec, defaults, row.active_W)
        flows.update(fuel)
    elif spec.fuel:
        raise ValueError(f'{row.machine} does not burn fuel')
    if row.energy_type == 'fluid' and not burning and p.energy_mode == 'balance':
        name = source.get('fluid_box', {}).get('filter')
        usage = source.get('fluid_usage_per_tick', 0)
        if not name or not usage:
            raise ValueError(f'{row.machine}: fluid heat source needs a filter and fluid_usage_per_tick')
        flows['fluid:' + name] = -usage * 60
        p.warnings.append(f'{row.machine}: fluid heat consumption assumes full flow')
    if p.energy_mode == 'balance':
        for k, v in flows.items():
            row.balance[k] = row.balance.get(k, 0) + v / row.crafts_per_machine
    return row


def turbine_channel(machine: str) -> str | None:
    match = re.fullmatch(r'nullius-turbine-(?:generator-)?(open|closed)-(?:backup|standard|exhaust)-(\d+)', machine)
    return f'{match[1]}-{match[2]}' if match else None


def channel_recipe(recipe: RecipeView, machine: str) -> RecipeView:
    channel = turbine_channel(machine)
    if channel is None:
        return recipe
    recipe = recipe.model_copy(deep=True)
    for entry in recipe.results:
        if entry.type == 'fluid' and entry.name == 'nullius-energy':
            entry.name += '~' + channel
    return recipe


def energy_line(p: 'Planner', spec: LineSpec, defaults: Defaults) -> Line:
    prefix, _, name = spec.recipe.partition(':')
    if '@' in name:
        name, _, encoded_temperature = name.rpartition('@')
        spec = spec.model_copy(update={'temperature': float(encoded_temperature)})
    kind = next((kind for kind in PREFIXES[prefix] if name in p.raw.get(kind, {})), None)
    if kind is None:
        raise ValueError(f'Unknown energy entity: {spec.recipe}')
    if spec.machine and spec.machine != name:
        raise ValueError('Energy recipe machine must match its entity')
    if spec.modules or spec.beacons:
        raise ValueError('Energy recipes do not support modules or beacons')
    proto = p.raw[kind][name]
    source = proto.get('energy_source', {})
    balance: dict[str, float] = {}
    fuel = None
    fuel_rate = 0.0
    place = proto.get('placeable_by', {})
    item = (place.get('item') if isinstance(place, dict) else None) or proto.get('minable', {}).get('result')
    helper = re.fullmatch(r'nullius-turbine-generator-(open|closed)-(backup|standard|exhaust)-(\d+)', name)
    if helper:
        item = f'nullius-turbine-{helper[1]}-{helper[3]}'
        p.warnings.append(
            f'{name}: internal generator of {item}; generator and turbine rows describe the same placed entity'
        )
    buildable = p.buildable(item=item) if item else p.buildable(entity=name)
    blocked = [f'machine {name}'] if p.validate and buildable is not True else []
    output = 'energy:heat' if prefix == 'heat' else 'energy:electric'
    if prefix == 'solar':
        power = quantity(proto.get('production')) * p.solar_factor
    elif prefix == 'wind':
        if p.wind_factor is None:
            raise ValueError('wind_factor is required for wind recipes')
        power = quantity(proto.get('energy_production')) * p.wind_factor
    elif prefix == 'heat':
        consumption = quantity(proto.get('consumption'))
        efficiency = source.get('effectivity', 1)
        power = consumption * efficiency * (1 + proto.get('neighbour_bonus', 0) * spec.neighbours)
        if source.get('type') == 'burner' or source.get('type') == 'fluid' and source.get('burns_fluid'):
            # Reactor consumption is fuel input; its efficiency multiplies the heat output once.
            fuel, fuel_rate, balance = fuel_flows(p, {**source, 'effectivity': 1}, spec, defaults, consumption)
        elif source.get('type') == 'fluid':
            fluid = source.get('fluid_box', {}).get('filter')
            if not fluid or not source.get('fluid_usage_per_tick'):
                raise ValueError(f'{name}: fluid heat source needs filter and usage')
            balance['fluid:' + fluid] = -source['fluid_usage_per_tick'] * 60
            p.warnings.append(f'{name}: collector fluid consumption assumes full flow')
        elif source.get('type') == 'electric':
            balance['energy:electric'] = -consumption / 1e6
        if source.get('type') == 'void':
            p.warnings.append(f'{name}: geothermal heat assumes a suitable placement site')
    elif kind == 'burner-generator':
        power = quantity(proto.get('max_power_output'))
        fuel, fuel_rate, balance = fuel_flows(
            p, {**proto.get('burner', source), 'type': 'burner'}, spec, defaults, power
        )
    else:
        fluid = proto.get('fluid_box', {}).get('filter')
        fp = p.raw.get('fluid', {}).get(fluid, {})
        efficiency = proto.get('effectivity', 1)
        if proto.get('burns_fluid'):
            power = quantity(proto.get('max_power_output'))
            fuel, fuel_rate, balance = fuel_flows(
                p, {'type': 'fluid', 'fluid_box': {'filter': fluid}, 'effectivity': efficiency}, spec, defaults, power
            )
        else:
            if not fluid:
                raise ValueError(f'{name}: generator needs a filtered input fluid')
            temperature = spec.temperature if spec.temperature is not None else fp.get('default_temperature', 15)
            delta = min(temperature, proto.get('maximum_temperature', temperature)) - fp.get('default_temperature', 15)
            usage = proto.get('fluid_usage_per_tick', 0) * 60
            power = usage * delta * quantity(fp.get('heat_capacity')) * efficiency
            cap = quantity(proto.get('max_power_output'))
            if cap and power > cap:
                usage *= cap / power
                power = cap
            balance['fluid:' + fluid] = -usage
    if not math.isfinite(power) or power <= 0:
        raise ValueError(f'{name}: no positive energy output; steam generators need an input temperature')
    channel = turbine_channel(name)
    if channel and p.energy_mode == 'balance' and 'fluid:nullius-energy' in balance:
        balance['fluid:nullius-energy~' + channel] = balance.pop('fluid:nullius-energy')
    balance[output] = power / 1e6
    return Line(
        id=spec.id or spec.recipe,
        recipe=spec.recipe,
        machine=name,
        machine_type=kind,
        modules=[],
        beacons=[],
        speed_multiplier=1,
        productivity=0,
        consumption_multiplier=1,
        pollution_multiplier=1,
        research_productivity=0,
        crafts_per_machine=1,
        energy_type='producer',
        active_W=0,
        electric_consumption_W=quantity(proto.get('consumption'))
        if prefix == 'heat' and source.get('type') == 'electric'
        else 0,
        drain_W=0,
        beacon_W=0,
        pollution_per_minute=source.get('emissions_per_minute', {}).get('pollution', 0),
        balance=balance,
        temperatures={},
        ingredient_temperatures={},
        fixed_machines=spec.fixed_machines,
        max_machines=spec.max_machines,
        cost_weight=spec.cost_weight,
        blocked=blocked,
        fuel=fuel,
        fuel_per_machine=fuel_rate,
        neighbours=spec.neighbours,
        temperature=spec.temperature,
    )


def producers(p: 'Planner', key: str) -> list[str]:
    prefixes = ['heat'] if key == 'energy:heat' else ['generate', 'solar', 'wind']
    names = [
        prefix + ':' + name
        for prefix in prefixes
        if prefix != 'wind' or p.wind_factor is not None
        for kind in PREFIXES[prefix]
        for name, proto in p.raw.get(kind, {}).items()
        if not any(s in name for s in ('creative', 'hidden', 'mirror', 'broken'))
        and (not proto.get('hidden') or name.startswith('nullius-turbine-generator-') and '-standard-' in name)
        and (prefix != 'wind' or quantity(proto.get('energy_production')) > 0)
    ]

    output: list[str] = []
    for name in names:
        prefix, _, entity = name.partition(':')
        proto = p.raw.get('generator', {}).get(entity)
        if prefix == 'generate' and proto and not proto.get('burns_fluid'):
            fluid = proto.get('fluid_box', {}).get('filter')
            base = p.raw.get('fluid', {}).get(fluid, {}).get('default_temperature', 15)
            temperatures = sorted(
                {
                    e.get('temperature', base)
                    for recipe in p.db.recipes.values()
                    for e in entries(recipe.get('results'))
                    if e.get('type') == 'fluid' and e['name'] == fluid
                }
            )
            output.extend(f'{name}@{t:g}' for t in temperatures if t > base)
        else:
            output.append(name)
    return output
