"""Call one MCP tool and print its JSON result; with no TOOL, list the tools."""

import asyncio
import io
import json
import sys
from typing import Any

import click
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(tool: str | None, arguments: dict[str, Any], force: str | None) -> bool:
    """Return whether the tool reported an error."""
    # Going through a real stdio session (same interpreter, so this checkout's server) keeps CLI output
    # identical to what an MCP client sees, including tool errors.
    params = StdioServerParameters(
        command=sys.executable, args=['-m', 'recipe_mcp'] + (['--force', force] if force else [])
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        if tool is None:
            click.echo((await session.list_tools()).model_dump_json(indent=2))
            return False
        result = await session.call_tool(tool, arguments)
        click.echo(result.model_dump_json(indent=2))
        return bool(result.isError)


def parse_arguments(ctx: click.Context, param: click.Parameter, value: str | None) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise click.BadParameter(str(e)) from e
    if not isinstance(parsed, dict):
        raise click.BadParameter('must be a JSON object')
    return parsed


@click.command(help=__doc__)
@click.argument('tool', required=False)
@click.argument('arguments', required=False, callback=parse_arguments)
@click.option('--force', help='Default force for stage-aware queries.')
def main(tool: str | None, arguments: dict[str, Any], force: str | None) -> None:
    # Tool results contain localized names; the Windows console default code page cannot encode them.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding='utf-8')
    if asyncio.run(run(tool, arguments, force)):
        raise click.ClickException('MCP tool returned an error')


if __name__ == '__main__':
    main()
