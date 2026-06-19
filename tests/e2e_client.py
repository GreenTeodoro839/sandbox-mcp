"""End-to-end test: drive the running MCP server like a real client would.

Run on the Pi:
    cd ~/sandbox-mcp && set -a && . .env && set +a && .venv/bin/python tests/e2e_client.py
"""

import asyncio
import json
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("SMCP_TEST_URL", "http://127.0.0.1:8000/mcp")
TOKEN = os.environ["SMCP_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def text_of(result) -> str:
    return "\n".join(getattr(c, "text", str(c)) for c in result.content)


def data_of(result) -> dict:
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        # FastMCP wraps non-dict returns as {"result": ...}
        return sc.get("result", sc) if set(sc.keys()) == {"result"} else sc
    try:
        return json.loads(text_of(result))
    except Exception:
        return {"_text": text_of(result)}


async def main() -> None:
    print(f"connecting to {MCP_URL}")
    async with streamablehttp_client(MCP_URL, headers=HEADERS) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("\n[1] TOOLS:", sorted(t.name for t in tools.tools))

            print("\n[2] exec (auto-creates sandbox 'demo')")
            res = await s.call_tool(
                "exec",
                {"sandbox": "demo", "command": "echo hello-from-sandbox; python3 -c 'print(6*7)'"},
            )
            print("   ->", data_of(res))

            print("\n[3] upload a file via signed URL (the large-file side-channel)")
            up = data_of(await s.call_tool("upload_url", {"sandbox": "demo", "dest": "input/notes.txt"}))
            print("   upload_url:", up.get("upload_url"))
            async with httpx.AsyncClient() as hc:
                pr = await hc.put(up["upload_url"], content=b"line one\nline two\nhello pipeline\n")
            print("   PUT status:", pr.status_code, pr.text)

            print("\n[4] process the uploaded file inside the sandbox")
            res = await s.call_tool(
                "exec",
                {"sandbox": "demo", "command": "wc -l input/notes.txt; tr a-z A-Z < input/notes.txt > output.txt; cat output.txt"},
            )
            print("   ->", data_of(res))

            print("\n[5] run_background + poll get_job")
            bg = data_of(await s.call_tool(
                "run_background",
                {"sandbox": "demo", "command": "for i in 1 2 3; do echo step $i; sleep 1; done; echo JOBDONE"},
            ))
            job_id = bg["job_id"]
            print("   job_id:", job_id)
            await asyncio.sleep(5)
            jr = data_of(await s.call_tool("get_job", {"job_id": job_id}))
            print("   status:", jr.get("status"), "exit:", jr.get("exit_code"))
            print("   log:\n     " + (jr.get("log_tail", "").replace("\n", "\n     ")))

            print("\n[6] download the produced file via signed URL")
            dl = data_of(await s.call_tool("download_url", {"sandbox": "demo", "src": "output.txt"}))
            async with httpx.AsyncClient() as hc:
                gr = await hc.get(dl["download_url"])
            print("   GET status:", gr.status_code, "body:", repr(gr.text))

            print("\n[7] list_sandboxes")
            print("   ->", data_of(await s.call_tool("list_sandboxes", {})))

    print("\nALL STEPS COMPLETED")


if __name__ == "__main__":
    asyncio.run(main())
