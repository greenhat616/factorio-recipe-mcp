"""Read a save COPY in an isolated benchmark, export force research via helper mod."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile
from ..paths import DATA_DIR, PROGRESS, RAW_DUMP, WORKSPACE_ROOT

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def signature(recipe, runtime=False):
    def entries(values):
        return sorted((e.get('type','item'), e['name'],
                       round(e.get('probability',1) * e.get('amount',(e.get('amount_min',0)+e.get('amount_max',0))/2),6),
                       e.get('temperature'),e.get('minimum_temperature'),e.get('maximum_temperature')) for e in values)
    return (recipe.get('category','crafting'), recipe.get('energy' if runtime else 'energy_required', .5),
            entries(recipe.get('ingredients',[])), entries(recipe.get('products' if runtime else 'results',[])))

def main():
    ap = argparse.ArgumentParser(prog='recipe-mcp-export-save', description=__doc__)
    ap.add_argument('--save', help='Explicit save path; defaults to the newest .zip in Factorio/saves')
    ap.add_argument('--factorio', default=r'D:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe')
    ap.add_argument('--mods', default=str(Path.home()/'AppData/Roaming/Factorio/mods'))
    args = ap.parse_args()
    mods = Path(args.mods)
    source = Path(args.save) if args.save else max((mods.parent/'saves').glob('*.zip'), key=lambda p:p.stat().st_mtime_ns)
    work = Path(tempfile.mkdtemp(prefix='progress-', dir=DATA_DIR))
    local_mods = work/'mods'
    local_mods.mkdir()
    listed = json.loads((mods/'mod-list.json').read_text())
    enabled = {m['name']:m for m in listed['mods'] if m['enabled']}
    archives = {}
    for p in mods.glob('*.zip'):
        parts = p.stem.rsplit('_',1)
        if len(parts)!=2 or parts[0] not in enabled: continue
        name,version = parts
        if enabled[name].get('version') and enabled[name]['version']!=version: continue
        number = tuple(int(v) for v in version.split('.'))
        if name not in archives or number>archives[name][0]: archives[name]=(number,p)
    # Archive links are read-only inputs; mutable mod-list/settings are separate copies.
    for _,p in archives.values():
        os.link(p, local_mods/p.name) if p.drive.lower()==work.drive.lower() else shutil.copy2(p, local_mods/p.name)
    for p in mods.iterdir():
        if p.is_dir() and (p/'info.json').exists() and json.loads((p/'info.json').read_text(encoding='utf-8-sig'))['name'] in enabled:
            shutil.copytree(p, local_mods/p.name)
    helper_name = 'factorio-recipe-progress-helper'
    listed['mods'] = [m for m in listed['mods'] if m['name'] != helper_name]
    listed['mods'].append({'name':helper_name,'enabled':True})
    (local_mods/'mod-list.json').write_text(json.dumps(listed), encoding='utf-8')
    if (mods/'mod-settings.dat').exists(): shutil.copy2(mods/'mod-settings.dat', local_mods/'mod-settings.dat')
    helper = WORKSPACE_ROOT/helper_name
    archive = WORKSPACE_ROOT/f'{helper_name}_0.1.0.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in helper.rglob('*'):
            if p.is_file(): z.write(p, helper_name+'/'+p.relative_to(helper).as_posix())
    shutil.copy2(archive,local_mods/archive.name)
    copy = work/'source.zip'
    shutil.copy2(source,copy)
    copied_sha = sha(copy)
    with zipfile.ZipFile(copy) as z: bad=z.testzip()
    if bad: raise RuntimeError(f'Invalid save-copy archive: {bad}')
    exe = Path(args.factorio)
    config = work/'config.ini'
    config.write_text(f'[path]\nread-data={exe.parents[2].as_posix()}/data\nwrite-data={work.as_posix()}\n',encoding='utf-8')
    with (work/'console.log').open('w',encoding='utf-8') as log:
        subprocess.run([str(exe),'--config',str(config),'--mod-directory',str(local_mods),
                        '--benchmark',str(copy),'--benchmark-ticks','1','--benchmark-runs','1'],
                       stdout=log,stderr=subprocess.STDOUT,check=True)
    if sha(copy)!=copied_sha: raise RuntimeError('Benchmark unexpectedly modified the input save copy')
    progress = json.loads((work/'script-output/recipe-mcp/progress.json').read_text(encoding='utf-8'))
    raw_path = RAW_DUMP
    raw = json.loads(raw_path.read_text(encoding='utf-8'))
    runtime = progress.pop('prototype_recipes')
    mismatches = sorted(n for n in set(raw['recipe']) | set(runtime)
                        if n not in raw['recipe'] or n not in runtime or signature(raw['recipe'][n]) != signature(runtime[n],True))
    checksums = lambda p: dict(re.findall(r'Checksum of (.*): (\d+)',p.read_text(encoding='utf-8',errors='replace')))
    original_checksums, loaded_checksums = checksums(DATA_DIR/'export-console.log'), checksums(work/'console.log')
    loaded_checksums.pop(helper_name,None)
    progress['provenance'] = dict(source_save=str(source.resolve()), source_copy_sha256=copied_sha,
        exported_at=datetime.now(timezone.utc).isoformat(), extraction='one-tick benchmark of save copy with helper added; configuration-change handlers may run',
        prototype_raw_sha256=sha(raw_path), recipe_definitions_match=not mismatches,
        data_stage_checksums_match=bool(original_checksums) and original_checksums==loaded_checksums,
        mismatched_recipe_count=len(mismatches), mismatched_recipes=mismatches[:30], isolated_directory=str(work))
    target = PROGRESS
    target.write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8')
    summary = dict(output=str(target), provenance=progress['provenance'], forces={n:{'researched':sum(t['researched'] for t in f['technologies'].values()),
                 'enabled_recipes':sum(r['enabled'] for r in f['recipes'].values()),'current_research':f.get('current_research')} for n,f in progress['forces'].items()})
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
