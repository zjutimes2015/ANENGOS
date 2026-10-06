"""最小闭环演示：能力检查 → 异步审批 → 审计回放。

跑法：python demo.py
用一个「脚本化 LLM」代替真实模型，演示三类 Gatekeeper 决策。
"""

from __future__ import annotations

from pathlib import Path

from governance.approval import ApprovalQueue
from governance.audit import AuditLog
from governance.capability import CapabilityRegistry
from governance.gatekeeper import Gatekeeper
from kernel.loop import AgentOS
from kernel.tools import build_default_tools

WORKSPACE = Path("demo_workspace")
AUDIT = Path("demo_audit.jsonl")


def scripted_llm(plan: list[dict]):
    """按计划逐次返回工具调用，最后给出文本回答。"""

    def llm(messages, schemas):
        step = len([m for m in messages if m["role"] == "assistant"])
        if step < len(plan):
            return plan[step]
        return {"stop_reason": "end_turn", "text": "任务完成，全部动作已记录。"}

    return llm


def main() -> None:
    registry = CapabilityRegistry()
    # 用户把两个资源「介绍」给 agent：只读的仓库 + 有副作用的写操作
    registry.introduce("dev-agent", "file.read", "workspace:demo", side_effect=False)
    registry.introduce("dev-agent", "file.write", "workspace:demo", side_effect=True)

    approvals = ApprovalQueue()
    audit = AuditLog(AUDIT)
    gate = Gatekeeper(registry, "demo")
    tools = build_default_tools(WORKSPACE)

    plan = [
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t1", "name": "file.read",
             "input": {"path": "hello.txt"}}]},
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t2", "name": "file.write",
             "input": {"path": "hello.txt", "content": "hello agentos"}}]},
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t3", "name": "slack.post",
             "input": {"channel": "general", "text": "hi"}}]},
    ]

    os = AgentOS("dev-agent", tools, gate, approvals, audit, scripted_llm(plan))
    session = os.run("帮我读文件、写文件、发消息")

    print("=== 会话结果 ===")
    print("output:", session.output)
    print("blocked:", session.blocked_reason)
    print()
    print("=== 待审批队列（副作用动作已模拟放行，agent 未卡住） ===")
    for item in approvals.pending():
        print(f"  {item.request_id} {item.tool} -> {item.simulated_result}")

    print()
    print("=== 用户稍后批量批准 ===")
    n = approvals.approve_all()
    print(f"  已批准 {n} 项")
    print()
    print("=== 审计回放 ===")
    for entry in audit.replay():
        print(f"  {entry['ts'][:19]} {entry['actor']} {entry['tool']:10s} "
              f"allowed={entry['allowed']} | {entry['decision']}")


if __name__ == "__main__":
    main()
