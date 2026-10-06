"""能力注册表：默认零权限，按需「介绍」（Cloudflare OS 能力模型）。

每个 actor（agent 或 Gadget）的能力表默认为空。想访问某资源，
必须由用户显式 introduce()。与传统 MCP 一次性全量挂载不同，
这里保证 actor 只有完成任务所需的最小权限。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Capability:
    """一项已授予的能力。side_effect=True 表示该动作会产生副作用，需要审批。"""

    tool: str
    scope: str  # 例如 repo:owner/name、table:users（只读）
    side_effect: bool = False


class CapabilityRegistry:
    """actor -> {tool: Capability}"""

    def __init__(self) -> None:
        self._table: dict[str, dict[str, Capability]] = {}

    def introduce(self, actor: str, tool: str, scope: str, side_effect: bool = False) -> None:
        """用户把某个资源「介绍」给 actor，授予窄权限。"""
        self._table.setdefault(actor, {})[tool] = Capability(
            tool=tool, scope=scope, side_effect=side_effect
        )

    def resolve(self, actor: str, tool: str) -> Capability | None:
        """查能力；未介绍则返回 None（默认零权限）。"""
        return self._table.get(actor, {}).get(tool)

    def revoke(self, actor: str, tool: str) -> None:
        self._table.get(actor, {}).pop(tool, None)

    def __repr__(self) -> str:
        return f"CapabilityRegistry({len(self._table)} actors)"
