#!/usr/bin/env python3
"""Reference player agent: puts an LLM in the loop against mc-remote's MCP endpoint.

Feeds the mod's MCP tool list to an OpenRouter model as OpenAI-style function
tools, then executes each tool call against the mod until the task is done.

Usage:
    MCREMOTE_TOKEN=<token> OPENROUTER_API_KEY=<key> \
        python3 agent/player_agent.py "chop one bamboo and pick it up"

Env:
    MCREMOTE_URL      default http://127.0.0.1:25587
    MCREMOTE_TOKEN    bearer token (falls back to <mc>/run/config/mcremote.json)
    OPENROUTER_API_KEY  required
    MCREMOTE_MODEL    default anthropic/claude-haiku-4.5
    MAX_STEPS         default 80
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

MCREMOTE_URL = os.environ.get("MCREMOTE_URL", "http://127.0.0.1:25587").rstrip("/")
MODEL = os.environ.get("MCREMOTE_MODEL", "anthropic/claude-haiku-4.5")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "80"))
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"


def token() -> str:
    if os.environ.get("MCREMOTE_TOKEN"):
        return os.environ["MCREMOTE_TOKEN"]
    for p in ("run/config/mcremote.json", os.path.expanduser("~/.minecraft/config/mcremote.json")):
        try:
            with open(p) as f:
                return json.load(f)["token"]
        except OSError:
            continue
    return ""


TOKEN = token()


def post(url: str, payload: dict, bearer: str = "") -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read()[:400].decode(errors='replace')}"}
    except OSError as e:
        return {"error": str(e)}


def mcp_call(method: str, params: dict, req_id: int = 0) -> dict:
    return post(f"{MCREMOTE_URL}/mcp", {
        "jsonrpc": "2.0", "id": req_id, "method": method, "params": params,
    }, TOKEN)


def get_tools() -> list[dict]:
    resp = mcp_call("tools/list", {}, 1)
    tools = resp.get("result", {}).get("tools", [])
    return [{
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
        },
    } for t in tools]


def call_tool(name: str, args: dict) -> str:
    resp = mcp_call("tools/call", {"name": name, "arguments": args}, int(time.time() * 1000) % 2**31)
    try:
        text = resp["result"]["content"][0]["text"]
        return text
    except (KeyError, IndexError, TypeError):
        return json.dumps(resp)[:2000]


SYSTEM = """You are playing Minecraft Java Edition by calling tools that control a real client.
State reflects exactly what the game client received. Rules of thumb:
- Coordinates are block coords (yaw 0=+Z south, 90=-X west, 180=-Z north, -90=+X east; pitch -90=up, 90=down).
- Start by calling get_state with blocks_radius ~6-8 to see surroundings; re-check after moving.
- walk_to is greedy steering, not pathfinding: if it reports "stuck" or "timeout", pick a different route (go around, jump out, or mine through).
- mine needs line of sight within ~4.5 blocks: use looking_at/get_blocks to find a reachable face, look at it first.
- You see entities under 32 blocks; items on the ground show as entities with an "item" field.
- Keep steps cheap: prefer one walk_to over many small moves; batch nothing, the API serializes anyway.
- When the task is done, reply with a plain-text completion summary and no tool calls.
"""


def run(task: str) -> None:
    tools = get_tools()
    if not tools:
        print("FATAL: no tools from mod — is the client running?", file=sys.stderr)
        sys.exit(1)
    print(f"[agent] {len(tools)} tools, model={MODEL}", file=sys.stderr)

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Task: {task}"},
    ]

    for step in range(1, MAX_STEPS + 1):
        resp = post(OPENROUTER, {
            "model": MODEL,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }, os.environ.get("OPENROUTER_API_KEY", ""))
        if "error" in resp:
            print(f"[llm] error: {resp['error']}", file=sys.stderr)
            time.sleep(5)
            continue
        try:
            choice = resp["choices"][0]["message"]
        except (KeyError, IndexError):
            print(f"[llm] bad response: {json.dumps(resp)[:400]}", file=sys.stderr)
            time.sleep(5)
            continue

        messages.append(choice)
        calls = choice.get("tool_calls") or []
        if not calls:
            print(f"[agent] done at step {step}: {choice.get('content','')!r}")
            return

        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            print(f"[{step}] {name}({json.dumps(args)[:160]})", flush=True)
            result = call_tool(name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result[:8000],
            })
    print(f"[agent] hit MAX_STEPS={MAX_STEPS}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    run(" ".join(sys.argv[1:]))
