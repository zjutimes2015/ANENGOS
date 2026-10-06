"""工具分发：dispatch map + 路径沙箱（learn-claude-code s02）。

加一个工具 = 加一个 handler + 加一份 schema，循环体完全不动。
safe_path 防止工具逃逸工作区。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

Handler = Callable[[dict[str, Any]], str]


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

    def register(self, name: str, handler: Handler) -> None:
        self._handlers[name] = handler

    def has(self, name: str) -> bool:
        return name in self._handlers

    def run(self, name: str, args: dict[str, Any]) -> str:
        if name not in self._handlers:
            raise ToolNotFoundError(name)
        return self._handlers[name](args)

    def names(self) -> list[str]:
        return sorted(self._handlers)

    def schemas(self) -> list[dict[str, Any]]:
        """给 LLM 看的工具 schema 列表。"""
        return [{"name": n, "description": f"工具 {n}（经 Gatekeeper 鉴权）"} for n in self.names()]


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

    reg.register("file.read", read)
    reg.register("file.write", write)
    reg.register("file.list", list_dir)
    return reg
