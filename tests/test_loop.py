"""核心测试：循环正常退出、未授权被拒、审计可回放。"""

from __future__ import annotations

from pathlib import Path

from governance.approval import ApprovalQueue
from governance.audit import AuditLog
from governance.capability import CapabilityRegistry
from governance.gatekeeper import Gatekeeper
from kernel.loop import AgentOS
from kernel.tools import build_default_tools


def _make_os(tmp_path: Path, llm, actor: str = "tester"):
    registry = CapabilityRegistry()
    registry.introduce(actor, "file.read", "workspace:demo", side_effect=False)
    registry.introduce(actor, "file.write", "workspace:demo", side_effect=True)
    approvals = ApprovalQueue()
    audit = AuditLog(tmp_path / "audit.jsonl")
    tools = build_default_tools(tmp_path / "ws")
    return AgentOS(actor, tools, Gatekeeper(registry, "demo"), approvals, audit, llm), audit


def _llm_with(plan):
    def llm(messages, schemas):
        step = len([m for m in messages if m["role"] == "assistant"])
        if step < len(plan):
            return plan[step]
        return {"stop_reason": "end_turn", "text": "done"}

    return llm


def test_loop_exits_normally(tmp_path):
    """循环正常退出：工具调用后 end_turn，拿到最终输出。"""
    plan = [
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "r1", "name": "file.read", "input": {"path": "a.txt"}}]},
    ]
    os_, audit = _make_os(tmp_path, _llm_with(plan))
    session = os_.run("读 a.txt")
    assert session.output == "done"
    assert session.blocked_reason is None
    assert len(audit.replay()) == 1


def test_unauthorized_tool_is_blocked(tmp_path):
    """未介绍的工具被 Gatekeeper 拒绝，循环终止并说明原因。"""
    plan = [
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "s1", "name": "slack.post",
             "input": {"text": "hi"}}]},
    ]
    os_, audit = _make_os(tmp_path, _llm_with(plan))
    session = os_.run("发消息")
    assert session.output is None
    assert "未被介绍" in (session.blocked_reason or "")
    entries = audit.replay()
    assert entries and entries[-1]["allowed"] is False


def test_audit_replay_and_side_effect_queued(tmp_path):
    """副作用动作被排队审批（simulate-first），审计可回放。"""
    plan = [
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "w1", "name": "file.write",
             "input": {"path": "b.txt", "content": "x"}}]},
    ]
    os_, audit = _make_os(tmp_path, _llm_with(plan))
    session = os_.run("写文件")
    assert session.output == "done"  # 未被卡住
    pending = os_.approvals.pending()
    assert len(pending) == 1 and pending[0].tool == "file.write"
    assert os_.approvals.approve_all() == 1
    replay = audit.replay()
    assert len(replay) == 1
    assert replay[0]["tool"] == "file.write"
    assert replay[0]["allowed"] is True
