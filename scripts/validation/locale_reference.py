"""Compare names with Factorio's own locale dump in isolated write directories."""

import hashlib
import json
import re
import subprocess

import click

from recipe_mcp.database import Database
from recipe_mcp.paths import DATA_DIR, FACTORIO_EXE, FACTORIO_MODS


@click.command(help=__doc__)
@click.option('--language', multiple=True, default=('en', 'zh-CN'))
def main(language: tuple[str, ...]) -> None:
    db = Database()
    manifest = json.loads((DATA_DIR / 'manifest.json').read_text(encoding='utf-8'))
    for filename, key in [('mod-list.json', 'mod_list_sha256'), ('mod-settings.dat', 'startup_settings_sha256')]:
        if hashlib.sha256((FACTORIO_MODS / filename).read_bytes()).hexdigest() != manifest[key]:
            raise click.ClickException(f'{filename} differs from the prototype export')
    log = (DATA_DIR / 'export-console.log').read_text(encoding='utf-8')
    checksums = dict(re.findall(r'Checksum of (.+?): (\d+)', log))
    evidence: dict[str, dict[str, object]] = {}
    mismatches = 0
    for lang in language:
        db.names.get('fluid', 'nullius-water', lang)
        work = DATA_DIR / 'reports/helmod-gaps/native-locale' / lang
        work.mkdir(parents=True, exist_ok=True)
        config = work / 'config.ini'
        config.write_text(
            f'[path]\nread-data={(FACTORIO_EXE.parents[2] / "data").as_posix()}\n'
            f'write-data={work.as_posix()}\n[general]\nlocale={lang}\n',
            encoding='utf-8',
        )
        with (work / 'console.log').open('w', encoding='utf-8') as output:
            subprocess.run(
                [
                    str(FACTORIO_EXE),
                    '--config',
                    str(config),
                    '--mod-directory',
                    str(FACTORIO_MODS),
                    '--dump-prototype-locale',
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        log = (work / 'console.log').read_text(encoding='utf-8')
        if dict(re.findall(r'Checksum of (.+?): (\d+)', log)) != checksums:
            raise click.ClickException('Native reference uses different data-stage checksums')
        for kind in ('recipe', 'item', 'fluid', 'entity', 'technology'):
            reference = json.loads((work / f'script-output/{kind}-locale.json').read_text(encoding='utf-8'))['names']
            differences = {
                name: {'native': expected, 'actual': db.names.get(kind, name, lang).display_name}
                for name, expected in reference.items()
                if db.names.get(kind, name, lang).display_name != expected
            }
            mismatches += len(differences)
            evidence[f'{lang}/{kind}'] = {'compared': len(reference), 'differences': differences}
        click.echo(f'{lang}: native export and comparison finished')
    path = DATA_DIR / 'reports/helmod-gaps/locale-parity.json'
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    click.echo(f'{path}: {mismatches} mismatches')
    if mismatches:
        raise click.ClickException('Names differ from the native reference; inspect locale-parity.json')


if __name__ == '__main__':
    main()
