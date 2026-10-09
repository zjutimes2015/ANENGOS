"""总装线：互审监督智能体（Reviewer）单元测试 + 失败降级。"""

from __future__ import annotations

import json

from governance.audit import AuditLog
from governance.reviewer import Reviewer, _extract_json


class _PassLLM:
    def __call__(self, messages, schemas):
        return {"stop_reason": "end_turn", "text": '{"verdict":"pass","score":88,"reason":"产出合格"}'}


class _FixLLM:
    def __call__(self, messages, schemas):
        return {"stop_reason": "end_turn", "text": '{"verdict":"fix","score":40,"reason":"文件路径有安全问题"}'}


class _BrokenLLM:
    def __call__(self, messages, schemas):
        raise RuntimeError("模型 API 超时")


def test_extract_json_strict():
    assert _extract_json('{"verdict":"pass","score":1}') == {"verdict": "pass", "score": 1}
    assert _extract_json('前缀 {"verdict":"fix"} 后缀') == {"verdict": "fix"}
    assert _extract_json("没有 JSON") is None


def test_review_pass(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    r = Reviewer(_PassLLM(), audit)
    verdict = r.review("t1", "codex.submit", {"task": "写 hello"}, "[codex] 完成")
    assert verdict["verdict"] == "pass" and verdict["score"] == 88
    rows = audit.replay()
    assert rows and rows[0]["event"] == "review" and rows[0]["tool"] == "codex.submit"


def test_review_fix(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    r = Reviewer(_FixLLM(), audit)
    verdict = r.review("t1", "grok.submit", {"task": "文案"}, "[grok] 完成")
    assert verdict["verdict"] == "fix"


def test_review_skipped_on_llm_failure(tmp_path):
    """监督模型故障时降级为 skipped，不阻断业务，审计记录原因。"""
    audit = AuditLog(tmp_path / "audit.jsonl")
    r = Reviewer(_BrokenLLM(), audit)
    verdict = r.review("t1", "doubao.submit", {"task": "x"}, "[doubao] 完成")
    assert verdict["verdict"] == "skipped"
    assert "失败" in verdict["reason"]
    rows = audit.replay()
    assert rows and rows[0]["verdict"] == "skipped"
