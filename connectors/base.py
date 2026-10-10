"""外部智能体统一接口 + 注册器（多智能体总装线的「适配器层」，借鉴 AgentKey 的跨 provider 统一 Schema）。

总装线目标：把 Grokbot / 豆包工作 / Codex 等外部智能体纳入 ANENGOS 统一指挥。
每个外部智能体实现 AgentAdapter（统一入参 task/workspace，统一出参 AgentResult）：

- AgentResult：所有 provider 返回同一结构 {ok, provider, status, text, artifact_paths, error, meta}，
  管理台/互审/审计不再解析各家字符串——这就是「统一 Schema + Adapter」模式；
- health()：探测该适配器当前就绪状态（mode / 是否配 key），供管理台与客户展示可观测性；
- describe()：能力描述，注册器据此生成工具 schema，新增 provider 无需改主程序。

治理：register_adapter 以 `{name}.submit` 注册进 ToolRegistry（副作用，需审批），并注册
`{name}.health` 只读探测工具（无副作用）；批准后真实调用 submit，产出交由监督智能体互审；审计全程留痕。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass
class AgentResult:
    """外部智能体统一结果 Schema（跨 provider 同构，JSON 可序列化）。"""

    ok: bool
    provider: str
    status: str = "done"  # done | error | timeout | skipped | unconfigured
    text: str = ""  # 人类可读结果摘要（含 [provider/模式] 前缀，兼容旧调用方）
    artifact_paths: list[str] = field(default_factory=list)  # 交付物路径（相对 workspace）
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # mode / model / 耗时 / ts

    def to_text(self) -> str:
        return self.text or f"[{self.provider}/{self.status}] {'成功' if self.ok else '失败'}" + (
            f"：{self.error}" if self.error else ""
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __str__(self) -> str:
        return self.to_text()

    def __contains__(self, item: str) -> bool:
        """保持旧断言 `"codex" in result` 可用（历史测试兼容）。"""
        return item in self.to_text() or item in self.provider


def result_summary(provider: str, ok: bool, status: str, text: str,
                   artifacts: list[str] | None = None, error: str | None = None,
                   meta: dict[str, Any] | None = None) -> AgentResult:
    """便捷构造统一结果。"""
    return AgentResult(
        ok=ok, provider=provider, status=status, text=text,
        artifact_paths=list(artifacts or []), error=error, meta=meta or {},
    )


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class AgentAdapter(Protocol):
    """外部智能体适配器统一协议（统一入参 -> 统一 Schema 出参）。"""

    name: str

    def submit(self, task: str, workspace: str) -> AgentResult:
        """把任务交给外部智能体执行，返回统一 AgentResult。"""
        ...

    def health(self) -> dict[str, Any]:
        """就绪状态：{name, mode, ready, hint}（管理台可观测性）。"""
        ...

    def describe(self) -> dict[str, Any]:
        """能力描述：{summary, params}，注册器据此生成工具 schema。"""
        ...


def register_adapter(adapter: AgentAdapter, workspace: str, reg) -> None:
    """把任意外部智能体注册为受治理工具 `{name}.submit` + 只读探测 `{name}.health`。

    统一 Schema：submit 的返回值由 register_adapter 规范为可序列化字符串
    （AgentResult 的 text 摘要），审批/审计/管理台无需解析各家格式。
    """
    desc = adapter.describe() if hasattr(adapter, "describe") else {}
    summary = desc.get("summary", f"把任务交给 {adapter.name} 执行并返回结果摘要")
    params = desc.get("params", {"task": {"type": "string", "required": True}})

    def _submit(args: dict[str, Any]) -> str:
        task = str(args.get("task", "")).strip()
        if not task:
            return f"[{adapter.name}] 任务为空，未执行"
        result = adapter.submit(task, workspace)
        # 统一为字符串摘要（to_text 已含 provider 前缀）；结构化字段仍可从 result 取
        return result.to_text() if isinstance(result, AgentResult) else str(result)

    reg.register(
        f"{adapter.name}.submit",
        _submit,
        {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": f"交给 {adapter.name} 的任务"},
                "repo": {"type": "string", "description": "目标仓库路径（可选）"},
            },
            "required": ["task"],
        },
    )

    def _health(args: dict[str, Any]) -> str:
        try:
            h = adapter.health() if hasattr(adapter, "health") else {"name": adapter.name, "ready": True}
            return json.dumps(h, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"name": adapter.name, "ready": False, "hint": str(e)[:200]}, ensure_ascii=False)

    reg.register(
        f"{adapter.name}.health",
        _health,
        {
            "type": "object",
            "properties": {},
            "required": [],
        },
    )
