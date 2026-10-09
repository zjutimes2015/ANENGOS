"""浏览器工具测试：域名介绍制、审计、副作用审批。"""

from __future__ import annotations

from pathlib import Path

from connectors.browser import BrowserTools, MockBrowserDriver
from governance.approval import ApprovalQueue
from governance.audit import AuditLog
from governance.capability import CapabilityRegistry
from governance.gatekeeper import Gatekeeper
from kernel.loop import AgentOS
from kernel.tools import build_default_tools


def _make_browser_os(tmp_path: Path, plan: list[dict], actor: str = "browser-agent"):
    registry = CapabilityRegistry()
    # 只介绍 example.com：agent 可浏览该域名，其他域名一律拒绝
    registry.introduce(actor, "browser.open", "example.com", side_effect=False)
    registry.introduce(actor, "browser.extract", "example.com", side_effect=False)
    registry.introduce(actor, "browser.click", "example.com", side_effect=True)

    approvals = ApprovalQueue()
    audit = AuditLog(tmp_path / "audit.jsonl")
    tools = build_default_tools(tmp_path / "ws")
    driver = MockBrowserDriver()
    browser = BrowserTools(driver, registry)
    browser.register(tools, lambda: actor)

    def llm(messages, schemas):
        step = len([m for m in messages if m["role"] == "assistant"])
        if step < len(plan):
            return plan[step]
        return {"stop_reason": "end_turn", "text": "browser done"}

    os_ = AgentOS(
        actor, tools, Gatekeeper(registry, "browser"), approvals, audit, llm,
        pre_execute=browser.make_scope_checker(lambda: actor),
    )
    return os_, audit, approvals, driver


def _tool_call(tid: str, name: str, input_: dict) -> dict:
    return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": tid, "name": name, "input": input_}]}


def test_browser_open_allowed_and_audited(tmp_path):
    """被介绍域名：放行、返回页面内容、写审计。"""
    plan = [_tool_call("b1", "browser.open", {"url": "https://example.com"})]
    os_, audit, _, _ = _make_browser_os(tmp_path, plan)
    session = os_.run("打开 example.com")
    assert session.output == "browser done"
    assert "Example Domain" in str(session.messages)
    entries = audit.replay()
    assert entries[-1]["tool"] == "browser.open"
    assert entries[-1]["allowed"] is True


def test_browser_unknown_domain_blocked(tmp_path):
    """未介绍域名：pre_execute 域名白名单拒绝，循环终止并留审计。"""
    plan = [_tool_call("b2", "browser.open", {"url": "https://evil.example.org"})]
    os_, audit, _, _ = _make_browser_os(tmp_path, plan)
    session = os_.run("打开 evil 站")
    assert session.output is None
    assert "不在介绍白名单" in (session.blocked_reason or "")
    entries = audit.replay()
    assert entries[-1]["event"] == "tool_blocked"
    assert entries[-1]["allowed"] is False


def test_browser_click_goes_through_approval(tmp_path):
    """有副作用的点击：模拟放行、挂起排队审批、批准后续跑。"""
    plan = [_tool_call("b3", "browser.click", {"selector": "#buy"})]
    os_, audit, approvals, driver = _make_browser_os(tmp_path, plan)
    session = os_.run("点购买按钮")
    assert session.paused is True  # 挂起等待审批
    assert session.output is None
    pending = approvals.pending()
    assert len(pending) == 1 and pending[0].tool == "browser.click"
    # 批准并注入执行结果后续跑
    approvals.approve(pending[0].request_id)
    result = os_.tools.run(pending[0].tool, pending[0].args)
    session = os_.resume(session, [{"id": pending[0].request_id, "content": result}])
    assert session.paused is False
    assert session.output == "browser done"
    assert driver.clicks == ["#buy"]  # 批准后真实执行了点击
    assert audit.replay()[-1]["tool"] == "browser.click"
