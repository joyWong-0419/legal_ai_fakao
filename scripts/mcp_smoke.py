"""MCP stdio 冒烟测试：不经过 Claude，直接按协议对话。

用法：python scripts/mcp_smoke.py
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def send(proc: subprocess.Popen, payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def recv(proc: subprocess.Popen) -> dict:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("服务器无响应（可能启动失败）")
    return json.loads(line)


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-m", "legal_ai.mcp_server"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    try:
        send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "0"},
                },
            },
        )
        init = recv(proc)
        info = init.get("result", {}).get("serverInfo", {})
        print(f"initialize: {info.get('name')} {info.get('version', '')}".strip())

        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = recv(proc)
        names = [tool["name"] for tool in tools["result"]["tools"]]
        print(f"tools/list: {names}")

        send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "law_article",
                    "arguments": {"law": "刑法", "article": "20"},
                },
            },
        )
        call = recv(proc)
        payload = json.loads(call["result"]["content"][0]["text"])
        print(f"law_article: {payload.get('status')} | {payload.get('citation')}")

        send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "books_catalog",
                    "arguments": {},
                },
            },
        )
        call = recv(proc)
        payload = json.loads(call["result"]["content"][0]["text"])
        print(f"books_catalog: {payload.get('status')} | {payload.get('book_count')} 本")

        send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "evidence_search",
                    "arguments": {"query": "正当防卫 必要限度", "limit": 1},
                },
            },
        )
        call = recv(proc)
        payload = json.loads(call["result"]["content"][0]["text"])
        items = payload.get("items", [])
        print(f"evidence_search: {payload.get('status')} | {len(items)} 条")
        if items:
            print(f"  {items[0]['citation']}")
            leaked = [
                key
                for key in ("run_id", "sentence_id", "chunk_id", "sha256")
                if key in json.dumps(payload, ensure_ascii=False)
            ]
            print(f"  内部字段泄漏：{leaked or '无'}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
