"""总装线：豆包 / Grok 适配器 + 统一注册（mock 全链路）。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app
from connectors.base import AgentResult
from connectors.doubao import DoubaoAdapter
from connectors.grok import GrokAdapter


def test_adapters_mock_produce_files(tmp_path):
    """豆包与 Grok 的 mock 模式在工作区产出各自交付物目录（统一 AgentResult Schema）。"""
    db = DoubaoAdapter(api_key="")
    assert db.mode == "mock"
    r1 = db.submit("写一份产品摘要", str(tmp_path))
    assert isinstance(r1, AgentResult)
    assert r1.ok and r1.provider == "doubao" and r1.status == "done"
    assert "doubao" in str(r1) and "doubao" in r1  # 统一 text 前缀 + 旧断言兼容
    assert r1.artifact_paths and r1.artifact_paths[0].startswith("doubao_output/")
    assert (tmp_path / "doubao_output").exists()

    gk = GrokAdapter(api_key="")
    assert gk.mode == "mock"
    r2 = gk.submit("写一段营销文案", str(tmp_path))
    assert r2.ok and r2.provider == "grok" and "grok" in r2
    assert r2.artifact_paths and r2.artifact_paths[0].startswith("grok_output/")
    assert (tmp_path / "grok_output").exists()

    # 健康探测（未配 key -> mock，ready=True 表示演示可用）
    h = db.health()
    assert h["name"] == "doubao" and h["mode"] == "mock" and h["ready"] is True


class _FakeLLM:
    """主循环模型：先调 doubao.submit，再结束回合；监督智能体返回 pass 结论。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages, schemas):
        content = str(messages[-1].get("content", ""))
        if "监督智能体" in content:
            return {"stop_reason": "end_turn", "text": '{"verdict":"pass","score":92,"reason":"产出符合任务要求"}'}
        self.calls += 1
        if self.calls == 1:
            return {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "c1", "name": "doubao_submit",
                     "input": {"task": "写一篇 100 字的产品介绍"}}
                ],
            }
        return {"stop_reason": "end_turn", "text": "已提交给豆包"}


def _request(url: str, method: str = "GET", token: str | None = None, payload=None):
    req = urllib.request.Request(url, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode("utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode("utf-8")
            return r.status, json.loads(raw) if raw.startswith("{") else raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, json.loads(raw) if raw.startswith("{") else raw


def test_doubao_tool_governance_and_review(monkeypatch, tmp_path):
    """豆包适配器：注册 -> 审批 -> 真实产出 -> 互审 -> 审计全链路。"""
    monkeypatch.setattr(app, "WORKSPACE", tmp_path / "ws")
    monkeypatch.setattr(app, "AUDIT_FILE", tmp_path / "audit.jsonl")
    monkeypatch.setattr(app, "TENANTS_FILE", tmp_path / "tenants.json")
    html = tmp_path / "admin.html"
    html.write_text("<title>t</title>", encoding="utf-8")
    monkeypatch.setattr(app, "ADMIN_HTML", html)
    monkeypatch.setattr(app, "_APPROVALS", None)
    monkeypatch.setattr(app, "_TOOLS", None)
    monkeypatch.setattr(app, "_TENANTS", {})
    monkeypatch.setattr(app, "_TENANT_QUEUES", {})
    monkeypatch.setattr(app, "_TASKS", {})
    monkeypatch.setattr(app, "_REVIEWER", None)
    monkeypatch.setattr(app, "OpenAICompatLLM", _FakeLLM)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        code, body = _request(url + "/run", "POST", "admin123", {"query": "让豆包写产品介绍"})
        assert code == 200
        assert body["pending_approvals"] == [{"id": "req-1", "tool": "doubao.submit"}]

        code, body = _request(url + "/admin/api/approvals/req-1/approve", "POST", "admin123")
        assert code == 200
        # 批准后真实执行 + 监督智能体互审
        assert "doubao" in body["result"]
        assert "[互审]" in body["result"] and "pass" in body["result"]

        assert (tmp_path / "ws" / "doubao_output").exists()

        code, body = _request(url + "/admin/api/audit", token="admin123")
        events = [r["event"] for r in body["audit"]]
        assert "review" in events
        review_rows = [r for r in body["audit"] if r["event"] == "review"]
        assert review_rows and review_rows[0]["verdict"] == "pass"
        assert review_rows[0]["score"] == 92
    finally:
        srv.shutdown()
