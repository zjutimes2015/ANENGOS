"""真实模型演示：配好 ANENGOS_API_KEY 后跑一个「读-写-列」的受治理任务。

跑法（示例，豆包/DeepSeek/OpenAI 均可）：
  set ANENGOS_API_KEY=sk-xxx
  set ANENGOS_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
  set ANENGOS_MODEL=ep-xxxxxxxx
  python demo_real.py "在工作区里写一个 hello.txt，内容是 hello anengos，然后列出来"

没有 key 时程序直接提示，不会阻塞。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from governance.approval import ApprovalQueue
from governance.audit import AuditLog
from governance.capability import CapabilityRegistry
from governance.gatekeeper import Gatekeeper
from kernel.llm import OpenAICompatLLM
from kernel.loop import AgentOS
from kernel.tools import build_default_tools

ACTOR = "real-agent"
WORKSPACE = Path("demo_workspace")
AUDIT = Path("demo_real_audit.jsonl")


def main() -> None:
    if not os.environ.get("ANENGOS_API_KEY"):
        print("未配置 ANENGOS_API_KEY，请先设置环境变量（见 README「真实模型接入」）。")
        sys.exit(1)

    registry = CapabilityRegistry()
    registry.introduce(ACTOR, "file.read", "workspace", side_effect=False)
    registry.introduce(ACTOR, "file.write", "workspace", side_effect=True)
    registry.introduce(ACTOR, "file.list", "workspace", side_effect=False)

    approvals = ApprovalQueue()
    audit = AuditLog(AUDIT)
    tools = build_default_tools(WORKSPACE)
    llm = OpenAICompatLLM()

    os_ = AgentOS(ACTOR, tools, Gatekeeper(registry, "real"), approvals, audit, llm)
    query = sys.argv[1] if len(sys.argv) > 1 else "在工作区里写 hello.txt（内容 hello anengos），然后列出工作区文件"

    print(f"模型: {llm.model} | {llm.base_url}")
    print(f"任务: {query}\n")
    session = os_.run(query, max_steps=15)

    print("=== 最终输出 ===")
    print(session.output or f"（被拦截：{session.blocked_reason}）")
    print("\n=== 待审批 ===")
    for item in approvals.pending():
        print(f"  {item.request_id} {item.tool}")
    approvals.approve_all()
    print("\n=== 审计回放 ===")
    for entry in audit.replay():
        print(f"  {entry['ts'][:19]} {entry['tool']:12s} allowed={entry['allowed']} | {entry['decision']}")


if __name__ == "__main__":
    main()
