"""最小内核：THE AGENT PATTERN 循环 + 治理层接线（learn-claude-code s01 + Cloudflare OS）。

每次工具调用先过 Gatekeeper：
- 未介绍 → 拒绝，循环终止并说明；
- 无副作用 → 真实执行 + 审计；
- 有副作用 → 模拟放行 + 排队审批 + 审计（simulate-first）。

llm 参数是可注入的调用函数，便于测试与真实模型接入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from governance.approval import ApprovalQueue
from governance.audit import AuditLog
from governance.gatekeeper import GateDecision, Gatekeeper
from kernel.tools import ToolRegistry

LLM = Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]]
# pre_execute: 工具级能力通过后、执行前的细粒度校验（如域名白名单），返回 (允许, 原因)
PreExecute = Callable[[str, dict[str, Any]], tuple[bool, str]]


@dataclass
class Session:
    actor: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    output: str | None = None
    blocked_reason: str | None = None
    paused: bool = False
    pending_items: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    max_steps: int = 12


class AgentOS:
    """内核 + 治理的一体入口：跑一个任务，全程受 Gatekeeper 管辖。"""

    def __init__(
        self,
        actor: str,
        tools: ToolRegistry,
        gatekeeper: Gatekeeper,
        approvals: ApprovalQueue,
        audit: AuditLog,
        llm: LLM,
        pre_execute: PreExecute | None = None,
    ) -> None:
        self.actor = actor
        self.tools = tools
        self.gatekeeper = gatekeeper
        self.approvals = approvals
        self.audit = audit
        self.llm = llm
        self.pre_execute = pre_execute

    def run(self, query: str, max_steps: int = 12) -> Session:
        session = Session(actor=self.actor, max_steps=max_steps)
        session.messages.append({"role": "user", "content": query})
        self._loop(session)
        return session

    def resume(self, session: Session, results: list[dict[str, Any]]) -> Session:
        """审批后恢复执行：把已批准/拒绝的执行结果作为 tool_result 注入，继续循环。

        results: [{"id": request_id, "content": 真实执行结果或拒绝说明}]
        tool_use_id 必须与 assistant 消息里的 tool_use 一致，否则 API 会 400。
        """
        by_req = {p["request_id"]: p for p in session.pending_items}
        for r in results:
            item = by_req.get(r["id"], {})
            session.messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": item.get("tool_use_id", r["id"]),
                            "content": r["content"],
                        }
                    ],
                }
            )
        self._loop(session)
        return session

    def _loop(self, session: Session) -> None:
        """循环：LLM -> 工具 -> 治理；遇待审批即挂起（paused），由 resume 续跑。"""
        session.paused = False  # 每轮进入循环都重置挂起标记
        for _ in range(session.steps, session.max_steps):
            session.steps += 1
            response = self.llm(session.messages, self.tools.schemas())
            session.messages.append({"role": "assistant", "content": response})

            if response.get("stop_reason") != "tool_use":
                session.output = str(response.get("text", ""))
                return

            for block in response.get("content", []):
                if block.get("type") != "tool_use":
                    continue
                name, args = block["name"], block["input"]
                name = self.tools.canonical(name)  # 模型返回规范化名，还原为内部名再走治理
                decision = self.gatekeeper.check(self.actor, name)
                self.audit.log(
                    {
                        "actor": self.actor,
                        "event": "tool_call",
                        "tool": name,
                        "args": args,
                        "allowed": decision.allowed,
                        "decision": decision.reason,
                    }
                )
                if not decision.allowed:
                    session.blocked_reason = decision.reason
                    return
                if self.pre_execute is not None:
                    ok, reason = self.pre_execute(name, args)
                    if not ok:
                        self.audit.log(
                            {
                                "actor": self.actor,
                                "event": "tool_blocked",
                                "tool": name,
                                "args": args,
                                "allowed": False,
                                "decision": reason,
                            }
                        )
                        session.blocked_reason = reason
                        return
                if decision.simulate and decision.queue_approval:
                    item = self.approvals.submit(
                        self.actor, name, args, simulator=self._simulate
                    )
                    # 挂起：等待审批，批准后由 resume 注入真实结果续跑
                    session.paused = True
                    session.pending_items.append(
                        {
                            "request_id": item.request_id,
                            # 与 llm.to_openai 的 tool_calls id 生成规则保持一致，保证续跑时能对上
                            "tool_use_id": block.get("id", "call_" + block.get("name", "x")),
                            "tool": name,
                            "args": args,
                        }
                    )
                    return
                if not self.tools.has(name):
                    raise KeyError(f"未注册工具: {name}")
                result = self.tools.run(name, args)
                session.messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": block.get("id", name),
                                "content": result,
                            }
                        ],
                    }
                )
        raise RuntimeError(f"超过 {session.max_steps} 步未结束")

    def _simulate(self, tool: str, args: dict[str, Any]) -> str:
        return f"[模拟] 已按请求执行 {tool}({args})——等待用户批准后生效"
