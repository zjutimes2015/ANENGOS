"""借鉴清单第3步：Adapter + 统一 Schema 模式（AgentKey 借鉴）。

验证：AgentResult 统一结构（JSON 可序列化）、adapter.health()/describe()、
注册器自动注册 {name}.submit + {name}.health 工具、/admin/api/agents 可观测性端点、
健康探测工具经 Gatekeeper 只读放行（不产生审批）、错误场景统一 status。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import app
from connectors.base import AgentResult, register_adapter
from connectors.codex import CodexAdapter
from connectors.doubao import DoubaoAdapter
from connectors.grok import GrokAdapter


def test_agent_result_schema_json_roundtrip():
    """统一 Schema：AgentResult 可 JSON 序列化，字段同构。"""
    r = AgentResult(True, "codex", "done", "[codex/演示] 已产出 x.md",
                    artifact_paths=["codex_output/x.md"],
                    meta={"mode": "mock", "ts": "2026-01-01T00:00:00+00:00"})
    d = r.to_dict()
    assert d["ok"] is True and d["provider"] == "codex" and d["status"] == "done"
    assert d["artifact_paths"] == ["codex_output/x.md"] and d["error"] is None
    assert json.loads(json.dumps(d)) == d  # 可序列化
    assert str(r) == r.text and "codex" in r  # 旧断言兼容


def test_adapters_share_unified_schema_and_health(tmp_path):
    """三个适配器返回同一 AgentResult 结构 + health/describe。"""
    for adapter in (CodexAdapter(mode="mock"), DoubaoAdapter(api_key=""),
                    GrokAdapter(api_key="")):
        r = adapter.submit("测试统一 Schema", str(tmp_path))
        assert isinstance(r, AgentResult)
        assert r.provider == adapter.name and r.ok and r.status == "done"
        assert isinstance(r.to_dict(), dict)
        h = adapter.health()
        assert h["name"] == adapter.name and "mode" in h and "ready" in h
        assert "summary" in adapter.describe()


def test_register_health_tool_and_admin_agents_endpoint(monkeypatch, tmp_path):
    """注册器自动注册 health 工具（只读、不产生审批）；/admin/api/agents 返回就绪状态。"""
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
    monkeypatch.setattr(app, "_EXTERNAL_AGENTS", {"codex": CodexAdapter(mode="mock")})
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        def _req(u, method="GET", token=None):
            req = urllib.request.Request(u, method=method)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode())

        # /admin/api/agents：只读可观测性
        code, d = _req(url + "/admin/api/agents", token="admin123")
        assert code == 200
        assert d["agents"][0]["name"] == "codex" and d["agents"][0]["mode"] == "mock"
        # 未鉴权 401
        code, _ = _req(url + "/admin/api/agents")
        assert code == 401
        # 触发工具表惰性构建后，health/submit 工具已注册
        app._shared_context()
        assert app._TOOLS is not None and app._TOOLS.has("codex.health")
        health_out = app._TOOLS.run("codex.health", {})
        h = json.loads(health_out)
        assert h["name"] == "codex" and h["ready"] is True
        # submit 工具注册且返回值统一为字符串摘要
        submit_out = app._TOOLS.run("codex.submit", {"task": "写一个 x.py"})
        assert isinstance(submit_out, str) and "codex" in submit_out
    finally:
        srv.shutdown()


def test_error_scenarios_unified_status(tmp_path):
    """错误场景统一 status：空任务 skipped、未配 key unconfigured。"""
    d = DoubaoAdapter(api_key="", mode="http")
    r = d.submit("   ", str(tmp_path))
    assert r.status == "skipped" and r.ok is False
    r2 = d.submit("真实任务", str(tmp_path))
    assert r2.status == "unconfigured" and r2.error and "API_KEY" in r2.text
