"""Command-line entry: call one MCP tool and print its JSON result; with no TOOL, list the tools."""

import argparse
import asyncio
import io
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(tool: str | None, arguments: str | None, force: str | None) -> None:
    # Going through a real stdio session (same interpreter, so this checkout's server) keeps CLI output
    # identical to what an MCP client sees, including tool errors.
    params = StdioServerParameters(
        command=sys.executable, args=['-m', 'recipe_mcp'] + (['--force', force] if force else [])
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if tool is None:
                print((await session.list_tools()).model_dump_json(indent=2))
            else:
                result = await session.call_tool(tool, json.loads(arguments) if arguments else {})
                print(result.model_dump_json(indent=2))
                if result.isError:
                    raise RuntimeError('MCP tool returned an error')


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog='recipe-mcp-cli', description=__doc__)
    ap.add_argument('tool', nargs='?', help='Tool name, e.g. solve_production')
    ap.add_argument('arguments', nargs='?', help='Tool arguments as a JSON object')
    ap.add_argument('--force', help='Default force for stage-aware queries')
    args = ap.parse_args(argv)
    # Tool results contain localized names; the Windows console default code page cannot encode them.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(run(args.tool, args.arguments, args.force))


if __name__ == '__main__':
    main()
