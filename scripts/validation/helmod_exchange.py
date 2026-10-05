"""Check helmod_import and helmod_export against the Helmod in your save (two one-tick benchmarks of a copy).

1. A helper mod writes every Helmod model of the save as Helmod's own export string
   (serpent.dump + helpers.encode_string, which is what Helmod's Upload dialog shows).
2. Each model is imported as a plan; plan_solve's machine counts must equal Helmod's.
3. Each plan is exported; a copy of Helmod with one added test interface runs the Download dialog's
   import code (Converter.read, ModelBuilder.copyModel, ModelCompute.update) on it, and Helmod's
   machine counts must equal the plan's. Helmod applies the force's current mining productivity,
   so mining lines are compared by output (machines x (1 + productivity)).

Usage: uv run python scripts/validation/helmod_exchange.py [--save PATH] [--keep]
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from recipe_mcp.database import Database
from recipe_mcp.helmod.convert import export_helmod, import_helmod
from recipe_mcp.paths import DATA_DIR, FACTORIO_EXE, FACTORIO_MODS
from recipe_mcp.plans import PlanStore
from recipe_mcp.plans.factory import solve_plan

DUMP = """
script.on_event(defines.events.on_tick, function()
  for id, model in pairs(remote.call("helmod_interface", "get_models") or {}) do
    helpers.write_file("helmod/" .. id .. ".txt", helpers.encode_string(serpent.dump(model)), false)
  end
  script.on_event(defines.events.on_tick, nil)
end)
"""
CHECK = """
local cases = require("cases")
script.on_event(defines.events.on_tick, function()
  local out = {}
  for id, text in pairs(cases) do
    local ok, result = pcall(remote.call, "helmod_import_test", "import", text)
    out[id] = ok and result or {error = tostring(result)}
  end
  helpers.write_file("helmod/check.json", helpers.table_to_json(out), false)
  script.on_event(defines.events.on_tick, nil)
end)
"""
# Appended to Helmod's data/RemoteAPI.lua in the isolated copy only.
TEST_INTERFACE = """
remote.add_interface("helmod_import_test", {
    import = function(text)
        Player.set(game.players[1])
        local data = Converter.read(text)
        if data == nil then return {error = "Converter.read failed"} end
        local model = {class = "Model", id = "model_check", owner = "check", blocks = {}, ingredients = {},
            resources = {}, time = 1, version = Model.version, index = 0}
        model.block_root = Model.newBlock(model, {name = model.id})
        model.block_root.parent_id = model.id
        model.time = data.time
        ModelBuilder.copyModel(model, data)
        ModelCompute.update(model)
        local blocks = {}
        local function walk(block)
            local children, recipes = {}, {}
            for _, child in pairs(block.children) do table.insert(children, child) end
            table.sort(children, function(a, b) return a.index < b.index end)
            for _, child in ipairs(children) do
                if child.class == "Block" then
                    walk(child)
                else
                    local factory = child.factory or {}
                    table.insert(recipes, {name = child.name, type = child.type, machines = factory.count_deep or 0,
                        productivity = factory.effects and factory.effects.productivity or 0})
                end
            end
            if #recipes > 0 then table.insert(blocks, recipes) end
        end
        walk(model.block_root)
        return {blocks = blocks}
    end
})
"""


def benchmark(save: Path, helper: dict[str, str], patch_helmod: bool) -> Path:
    """Run one tick of a save copy with the enabled mods plus a helper mod; return the script-output folder."""
    work = Path(tempfile.mkdtemp(prefix='helmod-exchange-', dir=DATA_DIR))
    mods = work / 'mods'
    mods.mkdir()
    listed = json.loads((FACTORIO_MODS / 'mod-list.json').read_text())
    enabled = {m['name'] for m in listed['mods'] if m['enabled']}
    newest: dict[str, tuple[tuple[int, ...], Path]] = {}
    for p in FACTORIO_MODS.glob('*.zip'):
        name, _, version = p.stem.rpartition('_')
        if name in enabled:
            number = tuple(int(v) for v in version.split('.'))
            if name not in newest or number > newest[name][0]:
                newest[name] = (number, p)
    for name, (_, p) in newest.items():
        if name == 'helmod' and patch_helmod:
            with zipfile.ZipFile(p) as src, zipfile.ZipFile(mods / p.name, 'w', zipfile.ZIP_DEFLATED) as dst:
                for entry in src.infolist():
                    data = src.read(entry.filename)
                    if entry.filename.endswith('/data/RemoteAPI.lua'):
                        data += TEST_INTERFACE.encode()
                    dst.writestr(entry, data)
        else:
            shutil.copy2(p, mods / p.name)
    for p in FACTORIO_MODS.iterdir():
        info = p / 'info.json'
        if p.is_dir() and info.exists() and json.loads(info.read_text(encoding='utf-8-sig'))['name'] in enabled:
            shutil.copytree(p, mods / p.name)
    listed['mods'].append({'name': 'helmod-exchange-check', 'enabled': True})
    (mods / 'mod-list.json').write_text(json.dumps(listed))
    if (FACTORIO_MODS / 'mod-settings.dat').exists():
        shutil.copy2(FACTORIO_MODS / 'mod-settings.dat', mods / 'mod-settings.dat')
    manifest = {
        'name': 'helmod-exchange-check',
        'version': '0.1.0',
        'title': 'Helmod exchange check',
        'author': 'recipe-mcp',
        'factorio_version': '2.0',
        'dependencies': ['base >= 2.0.0', 'helmod'],
    }
    with zipfile.ZipFile(mods / 'helmod-exchange-check_0.1.0.zip', 'w') as z:
        z.writestr('helmod-exchange-check/info.json', json.dumps(manifest))
        for name, text in helper.items():
            z.writestr(f'helmod-exchange-check/{name}', text)
    shutil.copy2(save, work / 'source.zip')
    config = work / 'config.ini'
    config.write_text(f'[path]\nread-data={FACTORIO_EXE.parents[2].as_posix()}/data\nwrite-data={work.as_posix()}\n')
    with (work / 'console.log').open('w', encoding='utf-8') as log:
        subprocess.run(
            [str(FACTORIO_EXE), '--config', str(config), '--mod-directory', str(mods), '--benchmark']
            + [str(work / 'source.zip'), '--benchmark-ticks', '1', '--benchmark-runs', '1'],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    return work


def close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--save', type=Path, help='Save to read; defaults to the newest in Factorio/saves')
    parser.add_argument('--keep', action='store_true', help='Keep the isolated benchmark folders')
    args = parser.parse_args()
    save = args.save or max((FACTORIO_MODS.parent / 'saves').glob('*.zip'), key=lambda p: p.stat().st_mtime_ns)
    work = [benchmark(save, {'control.lua': DUMP}, False)]
    db = Database()
    store = PlanStore(work[0] / 'plans', db)
    failures: list[str] = []
    cases: dict[str, str] = {}
    expected: dict[str, list[list[tuple[float, float]]]] = {}
    skipped: dict[str, str] = {}
    lines = 0
    for path in sorted((work[0] / 'script-output/helmod').glob('*.txt')):
        name = path.stem
        try:
            imported = import_helmod(store, path.read_text(), name, validate_stage=False)
        except ValueError as e:
            skipped[name] = str(e)
            continue
        results = {
            o.id: o.result for o in solve_plan(store, name, detail='full', save_results=False).blocks if o.result
        }
        for bid, counts in imported.helmod_machines.items():
            got = {line.id: line.machines for line in results[bid].lines} if bid in results else {}
            for lid, count in counts.items():
                lines += 1
                if not close(got.get(lid, 0.0), count):
                    failures.append(f'import {name}/{bid}/{lid}: Helmod {count:.6g}, plan {got.get(lid, 0.0):.6g}')
        exported = export_helmod(store, name)
        cases[name] = exported.text or ''
        plan = store.read(name)
        expected[name] = []
        for bid in exported.blocks:
            by_id = {line.id: line for line in results[bid].lines} if bid in results else {}
            ids = [s if isinstance(s, str) else s.id or s.recipe for s in plan.block(bid).request.lines]
            expected[name].append(
                [(by_id[i].machines, by_id[i].productivity) if i in by_id else (0.0, 0.0) for i in ids]
            )
    lua = 'return {\n' + ''.join(f'  ["{n}"] = "{t}",\n' for n, t in cases.items()) + '}\n'
    work.append(benchmark(save, {'control.lua': CHECK, 'cases.lua': lua}, True))
    checked = json.loads((work[1] / 'script-output/helmod/check.json').read_text())
    for name, blocks in expected.items():
        r = checked.get(name, {'error': 'missing'})
        if 'error' in r:
            failures.append(f'export {name}: Helmod could not read it: {r["error"]}')
            continue
        got = r['blocks']
        if [len(b) for b in got] != [len(b) for b in blocks]:
            failures.append(
                f'export {name}: Helmod has {[len(b) for b in got]} recipes, plan {[len(b) for b in blocks]}'
            )
            continue
        for want_block, got_block in zip(blocks, got, strict=True):
            for (machines, productivity), recipe in zip(want_block, got_block, strict=True):
                mining = recipe['type'] == 'resource'
                want = machines * (1 + productivity) if mining else machines
                have = recipe['machines'] * (1 + recipe['productivity']) if mining else recipe['machines']
                if not close(want, have):
                    failures.append(f'export {name}/{recipe["name"]}: plan {want:.6g}, Helmod {have:.6g}')
    summary = {
        'save': str(save),
        'models': len(cases) + len(skipped),
        'imported': len(cases),
        'skipped': skipped,
        'lines': lines,
        'failures': failures,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if not args.keep:
        for w in work:
            shutil.rmtree(w, ignore_errors=True)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
