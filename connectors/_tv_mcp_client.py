"""TradingView MCP HTTP 共享客户端（Streamable JSON-RPC）。

被 quant_technical / quant_backtest 复用，封装：
  - initialize 握手 + session 管理
  - tools/call 调用 + SSE 响应解析
  - 超时/错误兜底（失败时返回 None，由 connector 降级到 mock）

配置：ANENGOS_TV_MCP_URL（默认 http://127.0.0.1:9100/mcp）
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any


class TVMCPClient:
    def __init__(self, url: str, timeout: int = 60) -> None:
        self.url = url.rstrip("/")
        if not self.url.endswith("/mcp"):
            self.url = self.url + "/mcp"
        self.timeout = timeout
        self._session_id: str | None = None
        self._req_id = 0

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                body = r.read().decode(errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return None

        # 解析 SSE: "event: message\ndata: {...}\n\n"
        m = re.search(r"data:\s*(\{.*\})", body, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def connect(self) -> bool:
        """initialize 握手。成功返回 True。"""
        result = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "anengos", "version": "0.3"}},
        })
        if not result or "result" not in result:
            return False
        # initialized notification（不需要响应）
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return True

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str | None:
        """调用 MCP 工具，返回 content text（失败返回 None）。"""
        if not self._session_id and not self.connect():
            return None
        result = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if not result or "result" not in result:
            # session 可能过期，重连一次
            self._session_id = None
            if not self.connect():
                return None
            result = self._post({
                "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })
            if not result or "result" not in result:
                return None
        content = result.get("result", {}).get("content", [])
        if not content:
            return None
        return content[0].get("text", "")
