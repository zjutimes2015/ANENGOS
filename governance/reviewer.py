"""互审监督智能体：审核外部智能体（Codex/豆包/Grok）的产出。

多智能体总装线的「监督」环节——外部 agent 执行完任务后，由独立的监督
智能体（复用主模型配置）审核产出质量 / 安全 / 合规，输出结构化结论：
  {"verdict": "pass|fix", "score": 0-100, "reason": "..."}

- LLM 不可用 / 审核失败时降级为 skipped（不阻断业务，审计记录原因）
- 审核结论写审计（event=review），与 tool_call / approval_applied 同链可追溯
"""

from __future__ import annotations

import json
import re
from typing import Any

from governance.audit import AuditLog

_REVIEW_PROMPT = """你是 ANENGOS 总装线的监督智能体，负责审核外部智能体的执行产出。

外部智能体：{name}
原始任务：{task}
执行结果：
{result}

请从以下维度审核：①是否完成原始任务；②产出质量；③安全与合规风险。
只输出一个 JSON 对象（不要任何其他文字）：
{{"verdict": "pass" 或 "fix", "score": 0到100的整数, "reason": "不超过100字的中文理由"}}
- pass：产出合格、可交付；fix：存在质量/安全/合规问题，需修正后再交付。"""


def _extract_json(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


class Reviewer:
    """监督智能体：审核外部 agent 产出并留审计。"""

    def __init__(self, llm, audit: AuditLog) -> None:
        self.llm = llm
        self.audit = audit

    def review(
        self, actor: str, tool: str, args: dict[str, Any], result: str, workspace: str = ""
    ) -> dict[str, Any]:
        name = tool.split(".")[0]
        task = str(args.get("task", ""))
        try:
            resp = self.llm(
                [{"role": "user", "content": _REVIEW_PROMPT.format(name=name, task=task, result=result)}],
                [],
            )
            text = resp.get("text", "") if isinstance(resp, dict) else str(resp)
            verdict = _extract_json(text) or {"verdict": "fix", "score": 0, "reason": "监督智能体未返回结构化结论"}
            verdict.setdefault("verdict", "fix")
            verdict.setdefault("score", 0)
            verdict.setdefault("reason", "")
        except Exception as e:  # noqa: BLE001
            verdict = {"verdict": "skipped", "score": None, "reason": f"监督审核失败：{str(e)[:200]}"}

        self.audit.log(
            {
                "actor": actor,
                "event": "review",
                "tool": tool,
                "args": args,
                "verdict": verdict.get("verdict"),
                "score": verdict.get("score"),
                "reason": verdict.get("reason", ""),
            }
        )
        return verdict
