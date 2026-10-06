"""Gadget 基类：私有沙箱应用（Cloudflare OS 概念）。

默认不联网、只能访问自己的工作区；暴露给 agent 的接口即为 Cap'n Web RPC 的简化版。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class SandboxError(PermissionError):
    pass


class Gadget:
    """每个用户跑自己的私有实例；默认零外部权限。"""

    def __init__(self, owner: str, workspace: Path) -> None:
        self.owner = owner
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._rpc: dict[str, Callable[..., Any]] = {}

    def expose(self, name: str, fn: Callable[..., Any]) -> None:
        """注册一个可被 agent 直接调用的方法。"""
        self._rpc[name] = fn

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name not in self._rpc:
            raise KeyError(f"Gadget 未暴露方法: {name}")
        return self._rpc[name](*args, **kwargs)

    def api_schema(self) -> list[str]:
        return list(self._rpc)

    def write_local(self, rel: str, content: str) -> str:
        """只允许写自己的工作区。"""
        p = (self.workspace / rel).resolve()
        if not p.is_relative_to(self.workspace.resolve()):
            raise SandboxError(f"越界写入: {rel}")
        p.write_text(content, encoding="utf-8")
        return f"已写入 {rel}"
