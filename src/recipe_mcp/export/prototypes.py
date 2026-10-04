"""Export the active Factorio prototype set without touching the running game."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import re
from datetime import datetime, timezone

from ..database import Database
from ..paths import DATA_DIR

def main():
    ap = argparse.ArgumentParser(prog='recipe-mcp-export', description=__doc__)
    ap.add_argument('--factorio', default=r'D:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe')
    ap.add_argument('--mods', default=str(Path.home() / 'AppData/Roaming/Factorio/mods'))
    args = ap.parse_args()
    exe, mods = Path(args.factorio), Path(args.mods)
    work = DATA_DIR
    work.mkdir(exist_ok=True)
    config = work / 'config.ini'
    config.write_text(f'[path]\nread-data={exe.parents[2].as_posix()}/data\nwrite-data={work.as_posix()}\n', encoding='utf-8')
    mod_list = (mods / 'mod-list.json').read_bytes()
    (work / 'mod-list.snapshot.json').write_bytes(mod_list)
    settings = mods / 'mod-settings.dat'
    manifest = {'exported_at': datetime.now(timezone.utc).isoformat(), 'mods_directory': str(mods),
                'mod_list_sha256': hashlib.sha256(mod_list).hexdigest(),
                'startup_settings_sha256': hashlib.sha256(settings.read_bytes()).hexdigest() if settings.exists() else None}
    with (work / 'export-console.log').open('w', encoding='utf-8') as log:
        subprocess.run([str(exe), '--config', str(config), '--mod-directory', str(mods), '--dump-data'],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    raw_path = work / 'script-output/data-raw-dump.json'
    raw = json.loads(raw_path.read_text(encoding='utf-8'))
    manifest.update(recipe_count=len(raw['recipe']), raw_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest())
    log_text = (work / 'export-console.log').read_text(encoding='utf-8', errors='replace')
    manifest['loaded_mod_versions'] = dict(re.findall(r'Loading mod ([^ ]+) ([^ ]+) \(data\.lua\)', log_text))
    db = Database(raw=raw)
    (work / 'recipes.json').write_text(json.dumps([db.recipe(n) for n in sorted(db.recipes)], ensure_ascii=False, indent=2), encoding='utf-8')
    (work / 'technologies.json').write_text(json.dumps([db.technology(n) for n in sorted(raw.get('technology',{}))], ensure_ascii=False, indent=2), encoding='utf-8')
    (work / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, indent=2))

if __name__ == '__main__':
    main()
