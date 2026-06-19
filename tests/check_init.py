"""Quick check: connect, print server instructions and tool count."""

import asyncio
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main() -> None:
    url = os.environ.get("SMCP_TEST_URL", "http://127.0.0.1:8000/mcp")
    headers = {"Authorization": "Bearer " + os.environ["SMCP_TOKEN"]}
    async with streamablehttp_client(url, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print("server:", init.serverInfo.name, init.serverInfo.version)
            print("instructions_present:", bool(init.instructions))
            print("---- instructions ----")
            print(init.instructions or "(none)")
            tools = await s.list_tools()
            print("---- tools:", len(tools.tools))


if __name__ == "__main__":
    asyncio.run(main())
