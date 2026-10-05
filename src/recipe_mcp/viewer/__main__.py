"""Export a planner JSON result (including MCP CLI envelopes) as an offline HTML report."""

import json
from pathlib import Path

import click

from . import render


@click.command(help=__doc__)
@click.argument('input_file', type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument('output_file', type=click.Path(dir_okay=False, path_type=Path))
@click.option('--title', default='生产规划观察器', show_default=True)
def main(input_file: Path, output_file: Path, title: str) -> None:
    try:
        result = json.loads(input_file.read_text(encoding='utf-8-sig'))
        html = render(result, title)
    except (ValueError, TypeError) as exc:
        raise click.ClickException(str(exc)) from exc
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(html, encoding='utf-8', newline='\n')
    click.echo(str(output_file.resolve()))


if __name__ == '__main__':
    main()
