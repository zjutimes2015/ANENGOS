"""浏览器「手」演示：域名介绍制 → 放行/拒绝 → 副作用审批 → 审计回放。

跑法：python demo_browser.py
"""

from __future__ import annotations

from pathlib import Path

from connectors.browser import BrowserTools, MockBrowserDriver
from governance.approval import ApprovalQueue
from governance.audit import AuditLog
from governance.capability import CapabilityRegistry
from governance.gatekeeper import Gatekeeper
from kernel.loop import AgentOS
from kernel.tools import build_default_tools

WORKSPACE = Path("demo_workspace")
AUDIT = Path("demo_browser_audit.jsonl")


def scripted_llm(plan):
    def llm(messages, schemas):
        step = len([m for m in messages if m["role"] == "assistant"])
        if step < len(plan):
            return plan[step]
        return {"stop_reason": "end_turn", "text": "浏览任务完成，全部动作已记录。"}

    return llm


def main() -> None:
    actor = "research-agent"
    registry = CapabilityRegistry()
    # 用户把 example.com「介绍」给 agent：只读可开、点击有副作用
    registry.introduce(actor, "browser.open", "example.com", side_effect=False)
    registry.introduce(actor, "browser.extract", "example.com", side_effect=False)
    registry.introduce(actor, "browser.click", "example.com", side_effect=True)

    approvals = ApprovalQueue()
    audit = AuditLog(AUDIT)
    tools = build_default_tools(WORKSPACE)
    driver = MockBrowserDriver()
    browser = BrowserTools(driver, registry)
    browser.register(tools, lambda: actor)

    plan = [
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "b1", "name": "browser.open",
             "input": {"url": "https://example.com"}}]},
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "b3", "name": "browser.click",
             "input": {"selector": "#buy"}}]},
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "b2", "name": "browser.open",
             "input": {"url": "https://evil.example.org"}}]},
    ]

    os_ = AgentOS(
        actor, tools, Gatekeeper(registry, "browser"), approvals, audit, scripted_llm(plan),
        pre_execute=browser.make_scope_checker(lambda: actor),
    )
    session = os_.run("调研 example.com，然后点购买，再开一个 evil 站")

    print("=== 会话结果 ===")
    print("output:", session.output)
    print("blocked:", session.blocked_reason)
    print()
    print("=== 待审批（点击被模拟放行，agent 未卡住） ===")
    for item in approvals.pending():
        print(f"  {item.request_id} {item.tool} -> {item.simulated_result}")
    os_.approvals.approve_all()
    print("  已批量批准")
    print()
    print("=== 审计回放（浏览器动作全程留痕） ===")
    for entry in audit.replay():
        print(f"  {entry['ts'][:19]} {entry['tool']:14s} allowed={entry['allowed']} | {entry['decision']}")


if __name__ == "__main__":
    main()
