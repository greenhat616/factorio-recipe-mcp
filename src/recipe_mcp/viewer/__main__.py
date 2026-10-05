"""Export a planner JSON result (including MCP CLI envelopes) as an offline HTML report."""

import json
from pathlib import Path

import click

from ..database import Database
from . import render, unwrap


@click.command(help=__doc__)
@click.argument('input_file', type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument('output_file', type=click.Path(dir_okay=False, path_type=Path))
@click.option('--title', default='生产规划观察器', show_default=True)
@click.option(
    '--language',
    multiple=True,
    help='Embed object names from the matching locale snapshot; repeat for offline switching.',
)
def main(input_file: Path, output_file: Path, title: str, language: tuple[str, ...]) -> None:
    try:
        result = json.loads(input_file.read_text(encoding='utf-8-sig'))
        if language:
            result = dict(unwrap(result))
            db = Database()
            previous_hash = (result.get('names') or {}).get('raw_sha256')
            if previous_hash and previous_hash != db.raw_sha256:
                raise ValueError('Report prototypes differ from the current database; keep its embedded names')
            result['names'] = db.names.catalog(result, list(language)).model_dump(mode='json')
        html = render(result, title)
    except (ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(html, encoding='utf-8', newline='\n')
    click.echo(str(output_file.resolve()))


if __name__ == '__main__':
    main()
