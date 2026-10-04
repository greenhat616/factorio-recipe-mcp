"""Export the active Factorio prototype set without touching the running game."""

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import click

from ..database import JSON, Database
from ..paths import DATA_DIR, FACTORIO_EXE, FACTORIO_MODS


@click.command(help=__doc__)
@click.option(
    '--factorio',
    'exe',
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=FACTORIO_EXE,
    show_default=True,
    help='Factorio executable.',
)
@click.option(
    '--mods',
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=FACTORIO_MODS,
    show_default=True,
    help='Mods directory whose mod-list.json and settings are used.',
)
def main(exe: Path, mods: Path) -> None:
    work = DATA_DIR
    work.mkdir(exist_ok=True)
    config = work / 'config.ini'
    config.write_text(
        f'[path]\nread-data={exe.parents[2].as_posix()}/data\nwrite-data={work.as_posix()}\n', encoding='utf-8'
    )
    mod_list = (mods / 'mod-list.json').read_bytes()
    (work / 'mod-list.snapshot.json').write_bytes(mod_list)
    settings = mods / 'mod-settings.dat'
    manifest: JSON = {
        'exported_at': datetime.now(UTC).isoformat(),
        'mods_directory': str(mods),
        'mod_list_sha256': hashlib.sha256(mod_list).hexdigest(),
        'startup_settings_sha256': hashlib.sha256(settings.read_bytes()).hexdigest() if settings.exists() else None,
    }
    with (work / 'export-console.log').open('w', encoding='utf-8') as log:
        subprocess.run(
            [str(exe), '--config', str(config), '--mod-directory', str(mods), '--dump-data'],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    raw_path = work / 'script-output/data-raw-dump.json'
    raw = json.loads(raw_path.read_text(encoding='utf-8'))
    manifest.update(recipe_count=len(raw['recipe']), raw_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest())
    log_text = (work / 'export-console.log').read_text(encoding='utf-8', errors='replace')
    manifest['loaded_mod_versions'] = dict(re.findall(r'Loading mod ([^ ]+) ([^ ]+) \(data\.lua\)', log_text))
    db = Database(raw=raw)
    (work / 'recipes.json').write_text(
        json.dumps([db.recipe(n).model_dump(mode='json') for n in sorted(db.recipes)], ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    (work / 'technologies.json').write_text(
        json.dumps(
            [db.technology(n).model_dump(mode='json') for n in sorted(raw.get('technology', {}))],
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    (work / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
