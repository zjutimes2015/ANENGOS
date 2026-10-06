"""真实模型接入：OpenAI 兼容 Chat Completions（豆包 / DeepSeek / Qwen / OpenAI 通用）。

配置（环境变量）：
  ANENGOS_API_KEY   必填；未配置时回退到脚本化 LLM（demo 模式，仓库开箱可跑）
  ANENGOS_BASE_URL  默认 https://api.openai.com/v1
                    （豆包 https://ark.cn-beijing.volces.com/api/v3、DeepSeek https://api.deepseek.com/v1 等）
  ANENGOS_MODEL     默认 gpt-4o-mini

只依赖标准库 urllib，无需安装 SDK；输出格式与 kernel/loop.py 的 LLM 协议一致。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 ANENGOS 会话消息转成 OpenAI Chat 消息格式。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        role, content = m["role"], m["content"]
        if role == "assistant":
            text = content.get("text", "") if isinstance(content, dict) else str(content)
            msg: dict[str, Any] = {"role": "assistant", "content": text}
            tool_calls = []
            blocks = content.get("content", []) if isinstance(content, dict) else []
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": b.get("id", "call_" + b.get("name", "x")),
                            "type": "function",
                            "function": {
                                "name": b["name"],
                                "arguments": json.dumps(b.get("input", {}), ensure_ascii=False),
                            },
                        }
                    )
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
        elif role == "user":
            if (
                isinstance(content, list)
                and content
                and isinstance(content[0], dict)
                and content[0].get("type") == "tool_result"
            ):
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": content[0].get("tool_use_id", ""),
                        "content": content[0].get("content", ""),
                    }
                )
            else:
                out.append(
                    {"role": "user", "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)}
                )
        else:
            out.append({"role": role, "content": str(content)})
    return out


def build_chat_request(
    messages: list[dict[str, Any]], schemas: list[dict[str, Any]], model: str
) -> dict[str, Any]:
    """构造 Chat Completions 请求体（含 tools）。"""
    tools = [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s.get("description", ""),
                "parameters": s.get("parameters", {"type": "object", "properties": {}}),
            },
        }
        for s in schemas
    ]
    return {
        "model": model,
        "messages": to_openai_messages(messages),
        "tools": tools,
        "tool_choice": "auto",
    }


def parse_openai_message(msg: dict[str, Any]) -> dict[str, Any]:
    """把 OpenAI 的 assistant message 转成 ANENGOS 的 LLM 协议响应。"""
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        blocks = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"raw": fn.get("arguments", "")}
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": args,
                }
            )
        return {"stop_reason": "tool_use", "content": blocks, "text": content}
    return {"stop_reason": "end_turn", "text": content, "content": []}


class OpenAICompatLLM:
    """真实模型客户端：__call__ 签名与 kernel/loop.py 的 LLM 协议一致。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("ANENGOS_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "未配置 ANENGOS_API_KEY。设置环境变量后使用真实模型，或用 ScriptedLLM 跑演示。"
            )
        self.base_url = (
            base_url or os.environ.get("ANENGOS_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("ANENGOS_MODEL") or "gpt-4o-mini"

    def __call__(
        self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]]
    ) -> dict[str, Any]:
        body = build_chat_request(messages, schemas, self.model)
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"模型 API 返回 {e.code}: {detail}") from e
        return parse_openai_message(data["choices"][0]["message"])
