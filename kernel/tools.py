"""工具分发：dispatch map + 路径沙箱（learn-claude-code s02）。

加一个工具 = 加一个 handler + 加一份 schema，循环体完全不动。
safe_path 防止工具逃逸工作区。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

Handler = Callable[[dict[str, Any]], str]

# OpenAI/DeepSeek 等厂商要求 function name 只含 [a-zA-Z0-9_-]。
# 内部用带命名空间的原始名（如 file.write），对外 schema 用规范化名（file_write）。
_FUNCTION_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _clean_name(name: str) -> str:
    return _FUNCTION_NAME_RE.sub("_", name)


class ToolNotFoundError(KeyError):
    pass


class PathEscapeError(PermissionError):
    pass


def safe_path(workspace: Path, raw: str) -> Path:
    """把相对路径解析到工作区内；试图逃逸则抛错。"""
    p = (workspace / raw).resolve()
    if not p.is_relative_to(workspace.resolve()):
        raise PathEscapeError(f"路径逃逸工作区: {raw}")
    return p


class ToolRegistry:
    """工具名 -> handler 的 dispatch map。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._params: dict[str, dict[str, Any]] = {}
        self._alias: dict[str, str] = {}

    def register(self, name: str, handler: Handler, parameters: dict[str, Any] | None = None) -> None:
        self._handlers[name] = handler
        self._params[name] = parameters or {"type": "object", "properties": {}}
        self._alias[_clean_name(name)] = name

    def has(self, name: str) -> bool:
        return name in self._handlers or _clean_name(name) in self._alias

    def run(self, name: str, args: dict[str, Any]) -> str:
        key = name if name in self._handlers else self._alias.get(_clean_name(name))
        if key is None:
            raise ToolNotFoundError(name)
        return self._handlers[key](args)

    def names(self) -> list[str]:
        return sorted(self._handlers)

    def canonical(self, name: str) -> str:
        """把模型返回的规范化工具名还原为内部名（file_write -> file.write）。"""
        if name in self._handlers:
            return name
        return self._alias.get(_clean_name(name), name)

    def schemas(self) -> list[dict[str, Any]]:
        """给 LLM 看的工具 schema 列表（含参数定义，供真实模型工具调用）。"""
        return [
            {
                "name": _clean_name(n),
                "description": f"工具 {n}（经 Gatekeeper 鉴权）",
                "parameters": self._params[n],
            }
            for n in self.names()
        ]


def build_default_tools(workspace: Path) -> ToolRegistry:
    """内置文件工具：read / write / list，作为最小演示集合。"""

    reg = ToolRegistry()

    def read(args: dict[str, Any]) -> str:
        p = safe_path(workspace, args["path"])
        return p.read_text(encoding="utf-8") if p.exists() else f"文件不存在: {args['path']}"

    def write(args: dict[str, Any]) -> str:
        p = safe_path(workspace, args["path"])
        p.write_text(args["content"], encoding="utf-8")
        return f"已写入 {args['path']}（{len(args['content'])} 字符）"

    def list_dir(args: dict[str, Any]) -> str:
        p = safe_path(workspace, args.get("path", "."))
        return "\n".join(sorted(x.name for x in p.iterdir()))

    reg.register("file.read", read, {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
    reg.register(
        "file.write",
        write,
        {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    )
    reg.register("file.list", list_dir, {"type": "object", "properties": {"path": {"type": "string"}}})
    return reg
