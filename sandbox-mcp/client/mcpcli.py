"""A tiny real MCP client used by verify.sh (and handy for humans).

  python mcpcli.py --token T tools
  python mcpcli.py --token T call <tool> '<json args>'
  python mcpcli.py --token T resource sandbox://<id>/events

Speaks Streamable HTTP with the official python SDK: initialize, list
tools, call a tool, read a resource. Output is JSON on stdout.
"""
import argparse
import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def run(args):
    url = os.environ.get("MCP_URL", "http://localhost:9420/mcp")
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    try:
        async with streamablehttp_client(url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                if args.cmd == "tools":
                    tools = await s.list_tools()
                    print(json.dumps(sorted(t.name for t in tools.tools)))
                elif args.cmd == "call":
                    res = await s.call_tool(args.tool, json.loads(args.args or "{}"))
                    out = []
                    for c in res.content:
                        if getattr(c, "type", "") == "text":
                            try:
                                out.append(json.loads(c.text))
                            except ValueError:
                                out.append(c.text)
                        elif getattr(c, "type", "") == "image":
                            out.append({"image_bytes": len(c.data) * 3 // 4, "mime": c.mimeType})
                    print(json.dumps({"is_error": res.isError, "content": out}))
                elif args.cmd == "resource":
                    res = await s.read_resource(args.tool)
                    print(json.dumps([getattr(c, "text", "")[:2000] for c in res.contents]))
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", default=os.environ.get("MCP_TOKEN", ""))
    ap.add_argument("cmd", choices=["tools", "call", "resource"])
    ap.add_argument("tool", nargs="?")
    ap.add_argument("args", nargs="?")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
