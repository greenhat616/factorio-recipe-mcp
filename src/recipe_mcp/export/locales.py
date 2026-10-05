"""Snapshot installed locale files for the exact exported prototype mod versions."""

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import click

from ..paths import DATA_DIR, FACTORIO_EXE


def parse_cfg(text: str) -> dict[str, str]:
    section = ''
    values: dict[str, str] = {}
    for line in text.lstrip('\ufeff').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(('#', ';')):
            continue
        if stripped.startswith('[') and stripped.endswith(']'):
            section = stripped[1:-1]
        elif '=' in line:
            key, value = line.split('=', 1)
            values[f'{section}.{key.strip()}' if section else key.strip()] = value
    return values


def snapshot(manifest: dict[str, Any], game_data: Path, mods: Path, log_text: str = '') -> dict[str, Any]:
    catalogs: dict[str, dict[str, str]] = {}
    sources: list[dict[str, str]] = []

    def add(mod: str, filename: str, content: bytes) -> None:
        match = re.search(r'(?:^|/)locale/([^/]+)/[^/]+\.cfg$', filename)
        # Core stores translations directly under locale/<language>.cfg.
        core = re.search(r'(?:^|/)locale/([^/]+)\.cfg$', filename)
        if not match and not core:
            return
        language = (match or core)[1]  # type: ignore[index]
        catalogs.setdefault(language, {}).update(parse_cfg(content.decode('utf-8-sig')))
        sources.append({'mod': mod, 'file': filename, 'sha256': hashlib.sha256(content).hexdigest()})

    versions = dict(manifest['loaded_mod_versions'])
    versions.update(re.findall(r'Loading mod (?:settings )?(.+?) ([0-9.]+) \(', log_text))
    # Some mods print the data-stage `mods` table, including translation-only packages.
    for bare, quoted, version in re.findall(r'^  (?:([\w-]+)|\["([^"\n]+)"\]) = "([0-9.]+)"[,\r]?$', log_text, re.M):
        versions[bare or quoted] = version
    order = re.findall(r'Checksum of (.+?): [0-9]+', log_text)
    order = ['core', *order] if order else list(versions)
    warnings = ['Static locale resolution; runtime script translations and player-specific controls are not evaluated.']
    for name in order:
        if name not in versions:
            warnings.append(
                f'Locale source omitted: exported version of {name} is not recorded. Re-export with its version pinned.'
            )
            continue
        version = versions[name]
        candidates = [game_data / name, mods / name, mods / f'{name}_{version}']
        directory = next(
            (
                p
                for p in candidates
                if (p / 'info.json').is_file()
                and json.loads((p / 'info.json').read_text(encoding='utf-8')).get('version') == version
            ),
            None,
        )
        if name == 'core' and (game_data / 'core/locale').is_dir():
            directory = game_data / 'core'
        if directory:
            for path in sorted((directory / 'locale').rglob('*.cfg')):
                add(name, path.relative_to(directory).as_posix(), path.read_bytes())
        else:
            archive = mods / f'{name}_{version}.zip'
            if not archive.exists():
                raise ValueError(f'Missing exported mod version: {name} {version}')
            with zipfile.ZipFile(archive) as z:
                infos = [f for f in z.namelist() if f.count('/') == 1 and f.endswith('/info.json')]
                if len(infos) != 1 or json.loads(z.read(infos[0]))['version'] != version:
                    raise ValueError(f'Mod archive version mismatch: {archive}')
                for filename in sorted(z.namelist()):
                    if '/locale/' in filename and filename.endswith('.cfg'):
                        add(name, filename, z.read(filename))
    return {
        'raw_sha256': manifest['raw_sha256'],
        'catalogs': catalogs,
        'sources': sources,
        'warnings': warnings,
        'loaded_mod_versions': {name: versions[name] for name in order if name in versions},
    }


@click.command(help=__doc__)
@click.option('--data', type=click.Path(path_type=Path), default=DATA_DIR)
@click.option('--game-data', type=click.Path(path_type=Path), default=FACTORIO_EXE.parents[2] / 'data')
def main(data: Path, game_data: Path) -> None:
    manifest = json.loads((data / 'manifest.json').read_text(encoding='utf-8'))
    raw_hash = hashlib.sha256((data / 'script-output/data-raw-dump.json').read_bytes()).hexdigest()
    if raw_hash != manifest['raw_sha256']:
        raise click.ClickException('Prototype dump does not match export manifest')
    try:
        result = snapshot(
            manifest,
            game_data,
            Path(manifest['mods_directory']),
            (data / 'export-console.log').read_text(encoding='utf-8', errors='replace')
            if (data / 'export-console.log').exists()
            else '',
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    path = data / 'locales.json'
    path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    click.echo(f'{path}: {len(result["catalogs"])} languages, {len(result["sources"])} files')


if __name__ == '__main__':
    main()
