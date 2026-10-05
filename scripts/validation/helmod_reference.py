"""Regenerate golden values by executing methods from a pinned Helmod zip (uv run --with lupa)."""

import argparse
import hashlib
import importlib
import json
from pathlib import Path
from typing import Any
from zipfile import ZipFile

METHODS = {
    'model/EntityPrototype.lua': ['EntityPrototype:getEnergyProduction', 'EntityPrototype:getNeighbourBonus'],
    'model/EnergySourcePrototype.lua': ['BurnerPrototype:getFuelCount'],
    'model/Product.lua': ['Product:getElementAmount', 'Product:getBonusAmount', 'Product:getBaseAmount'],
    'data/ModelCompute.lua': ['ModelCompute.computeFactory'],
}
SHA256 = '5b0f00b7eddca81d46ad9d073b8b1af6a51e4a148507db68b67e15f1dbe949f3'


def generate(zip_path: Path) -> dict[str, Any]:
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if digest != SHA256:
        raise ValueError('Helmod zip differs from the pinned 2.2.14 reference')
    lua = importlib.import_module('lupa').LuaRuntime(unpack_returned_tuples=True)
    lua.execute('EntityPrototype={}; BurnerPrototype={}; Product={}; ModelCompute={}')
    hashes = {}
    with ZipFile(zip_path) as archive:
        for suffix, methods in METHODS.items():
            name = next(n for n in archive.namelist() if n.endswith('/' + suffix))
            source = archive.read(name).decode()
            for method in methods:
                start = source.index('function ' + method + '(')
                end = source.find(
                    '\n-------------------------------------------------------------------------------', start
                )
                body = source[start : end if end >= 0 else len(source)]
                # A method may be followed by a local declaration before the next method separator.
                if method == 'EntityPrototype:getEnergyProduction':
                    body = body.split('\n---@type ModuleEffects')[0]
                hashes[method] = hashlib.sha256(body.encode()).hexdigest()
                lua.execute(body)
    lua.execute(r"""
function energy_case(kind, fuel_rate, value, efficiency, watts)
    local self = setmetatable({lua_prototype={type=kind}}, {__index=EntityPrototype})
    function self:getElectricEnergySource()
        if kind == 'reactor' then return nil end
        return {getUsagePriority=function() return kind == 'solar-panel' and 'solar' or 'secondary-output' end}
    end
    function self:getMaxEnergyProduction() return watts / 60 end
    function self:getMaxPowerOutput() return watts / 60 end
    function self:getEffectivity() return efficiency end
    function self:getFluidFuelPrototype() return {getFuelValue=function() return value end} end
    function self:getFluidConsumption() return fuel_rate end
    function self:getBurnsFluid() return true end
    function self:getMaxEnergyUsage() return watts end
    function self:getEnergySource() return {getEffectivity=function() return efficiency end} end
    return self:getEnergyProduction()
end
function product_case(element, bonus)
    local self = setmetatable({lua_prototype=element}, {__index=Product})
    function self:getProductivityBonus() return bonus end
    return self:getBaseAmount({})
end
function factory_case(speed, consumption, beacon)
    local factory = {effects={speed=speed, consumption=consumption, pollution=0}}
    local recipe = {factory=factory, base_time=1}
    local proto = {}
    function proto:speedFactory() return 1 end
    function proto:getEnergyType() return 'electric' end
    function proto:getEnergyConsumption() return 100000 end
    function proto:getPollution() return 0 end
    function proto:getMinEnergyUsage() return 0 end
    function proto:getEnergyUsage() return 20000 end
    local old = EntityPrototype
    EntityPrototype = function() return proto end
    RecipePrototype = function() return {getEnergy=function() return 2 end, getEmissionsMultiplier=function() return 1 end} end
    Model = {countModulesModel=function() return 1 end}
    if beacon > 0 then recipe.beacons = {{per_factory=beacon}} end
    ModelCompute.computeFactory(recipe)
    EntityPrototype = old
    return recipe.factory.amount, recipe.energy_total
end
function fuel_case(watts, joules)
    local old = EntityPrototype
    EntityPrototype = function() return {getEnergyConsumption=function() return watts end} end
    local self = setmetatable({factory={}}, {__index=BurnerPrototype})
    function self:getFuelPrototype() return {getFuelValue=function() return joules end, native=function() return {name='rod'} end} end
    local result = self:getFuelCount().count
    EntityPrototype = old
    return result
end
""")
    g = lua.globals()
    factories = []
    for speed, consumption, beacon in [(0, 0, 0), (0.5, 0.2, 0), (0, -0.5, 0), (1, 0.4, 2)]:
        machines, power = g.factory_case(speed, consumption, beacon)
        factories.append(dict(speed=speed, consumption=consumption, beacon=beacon, machines=machines, power_W=power))
    products = []
    for element, bonus in [
        ({'amount': 2}, 0),
        ({'amount': 2, 'probability': 0.25}, 0.2),
        ({'amount_min': 2, 'amount_max': 6, 'probability': 0.5}, 0.3),
        ({'amount': 4, 'ignored_by_productivity': 1}, 0.5),
        ({'amount': 4, 'probability': 0.5, 'ignored_by_productivity': 1}, 0.5),
    ]:
        products.append(dict(element=element, bonus=bonus, output=g.product_case(lua.table_from(element), bonus)))
    return dict(
        reference='Helmod 2.2.14 original Lua methods with prototype API stubs; not an in-game UI capture',
        zip_sha256=digest,
        method_sha256=hashes,
        factories=factories,
        products=products,
        fuel_per_second=g.fuel_case(50e6, 4e9),
        generator_W=g.energy_case('generator', 1000 / 9, 10000, 0.9, 1e6),
        reactor_W=g.energy_case('reactor', 0, 0, 1, 50e6),
        solar_peak_W=g.energy_case('solar-panel', 0, 0, 1, 100000),
        neighbours=[
            g.EntityPrototype.getNeighbourBonus(
                lua.table_from(
                    dict(
                        lua_prototype=lua.table_from({'neighbour_bonus': 1}),
                        factory=lua.table_from({'neighbour_bonus': n}),
                    )
                )
            )
            for n in [0, 2, 4, 8]
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('zip', type=Path)
    parser.add_argument('--output', type=Path, default=Path('tests/fixtures/helmod-2.2.14.json'))
    args = parser.parse_args()
    result = generate(args.zip)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(f'Wrote {args.output}: original Helmod Lua executed successfully')


if __name__ == '__main__':
    main()
