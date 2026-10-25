#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦当劳 MCP Server 最小客户端（Streamable HTTP / JSON-RPC 2.0）

只用标准库，无第三方依赖。
Token 从环境变量 MCD_MCP_TOKEN 读取，绝不写进代码或配置文件。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

MCP_URL = os.environ.get("MCD_MCP_URL", "https://mcp.mcd.cn")
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "mcd-group-split", "version": "1.0.0"}


class MCPError(RuntimeError):
    """MCP 调用异常（携带 HTTP 状态码 / JSON-RPC error）。"""


def _token() -> str:
    token = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if not token:
        raise MCPError(
            "未找到 MCD_MCP_TOKEN 环境变量。\n"
            "请先执行： export MCD_MCP_TOKEN='你的Token'  # https://open.mcd.cn/mcp"
        )
    return token


def _post(payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(MCP_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", "Bearer " + _token())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # 401 / 429 等
        hint = {401: "Token 无效或已过期", 429: "触发限流（600 次/分钟）"}.get(exc.code, "")
        raise MCPError(f"HTTP {exc.code} {hint}".strip()) from exc
    except urllib.error.URLError as exc:
        raise MCPError(f"网络不可达：{exc.reason}") from exc

    # 兼容 SSE 帧（部分网关会以 text/event-stream 返回）
    if not raw.lstrip().startswith("{"):
        chunks = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        raw = "\n".join(chunks) or raw
    data = json.loads(raw)
    if "error" in data:
        raise MCPError(f"JSON-RPC error: {data['error']}")
    return data


def initialize() -> dict[str, Any]:
    """握手。麦当劳 MCP 为无状态模式，握手后可直接 tools/call。"""
    return _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        }
    )


def list_tools() -> list[dict[str, Any]]:
    return _post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})["result"]["tools"]


def call_tool(name: str, arguments: dict[str, Any] | None = None, _id: int = 3) -> dict[str, Any]:
    """调用一个 MCP Tool，返回 structuredContent（无则回退解析 text）。"""
    data = _post(
        {
            "jsonrpc": "2.0",
            "id": _id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
    )
    result = data.get("result", {})
    if result.get("isError"):
        raise MCPError(f"{name} 返回错误：{result.get('content')}")
    if "structuredContent" in result:
        return result["structuredContent"]
    for part in result.get("content", []):
        if part.get("type") == "text":
            try:
                return json.loads(part["text"])
            except json.JSONDecodeError:
                return {"text": part["text"]}
    return {}


def call_with_retry(name: str, arguments: dict[str, Any] | None = None,
                    retries: int = 2, backoff: float = 1.2) -> dict[str, Any]:
    """轻量重试：只对 429 / 网络抖动重试，业务错误直接抛出。"""
    for attempt in range(retries + 1):
        try:
            return call_tool(name, arguments)
        except MCPError as exc:
            msg = str(exc)
            retriable = "429" in msg or "网络不可达" in msg or "timed out" in msg
            if attempt >= retries or not retriable:
                raise
            time.sleep(backoff * (attempt + 1))
    raise MCPError("unreachable")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python mcd_client.py <tool_name> ['{\"arg\":1}']")
        print("      python mcd_client.py --list")
        raise SystemExit(1)
    if sys.argv[1] == "--list":
        for t in list_tools():
            print(f"- {t['name']}: {t.get('description', '')[:70]}")
    else:
        args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        print(json.dumps(call_with_retry(sys.argv[1], args), ensure_ascii=False, indent=2))
