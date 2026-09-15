"""One-shot stdio MCP client: client.py TOOL JSON_ARGUMENTS."""
import asyncio
import json
import sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name('server.py'))])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if len(sys.argv)==1:
                print((await session.list_tools()).model_dump_json(indent=2))
            else:
                result = await session.call_tool(sys.argv[1],json.loads(sys.argv[2]) if len(sys.argv)>2 else {})
                print(result.model_dump_json(indent=2))
                if result.isError: raise RuntimeError('MCP tool returned an error')

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
