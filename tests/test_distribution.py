"""借鉴清单第5步：llms.txt + 一行安装（分发闭环）。

验证：llms.txt 匿名可读且符合 llmstxt 规范（标题/摘要/入口/工具/接入步骤）；
/install 需鉴权，租户身份返回粘贴即用的 Claude/Cursor MCP 配置与快速验证命令，
管理员身份给出指引（管理员 token 不能调 MCP）；ANENGOS_PUBLIC_URL 参与生成。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import app
from test_quota import _serve, _request


def _raw(url: str, token=None):
    r = urllib.request.Request(url)
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_llms_txt_anonymous_and_complete(monkeypatch, tmp_path):
    """llms.txt 匿名可读：标题/摘要/MCP 端点/工具清单/接入步骤齐全。"""
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        monkeypatch.setenv("ANENGOS_PUBLIC_URL", "https://siyu-ai.com")
        code, body = _raw(url + "/llms.txt")
        assert code == 200
        assert body.startswith("# ANENGOS")
        assert "私有知识库" in body
        assert "https://siyu-ai.com/mcp" in body
        for t in ("knowledge_list", "knowledge_search", "knowledge_ask", "knowledge_upload"):
            assert t in body
        assert "/install" in body and "5 分钟" in body
    finally:
        srv.shutdown()


def test_install_requires_auth(monkeypatch, tmp_path):
    """/install 必须鉴权（匿名 401）。"""
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, _ = _request(url + "/install", token=None)
        assert code == 401
    finally:
        srv.shutdown()


def test_install_tenant_payload(monkeypatch, tmp_path):
    """租户身份返回一行安装配置：claude/cursor 配置 + quick_check + 工具清单。"""
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        monkeypatch.setenv("ANENGOS_PUBLIC_URL", "https://siyu-ai.com")
        # 建租户并取 token
        code, d = _request(url + "/admin/api/tenants", {"name": "客户甲", "plan": "team"},
                           method="POST", token="admin123")
        assert code == 200, d
        tenant_id, token = d["tenant_id"], d["token"]
        code, d = _request(url + "/install", token=token)
        assert code == 200
        assert d["role"] == "tenant" and d["tenant_id"] == tenant_id
        assert d["mcp_url"] == "https://siyu-ai.com/mcp"
        assert d["claude_config"]["mcpServers"]["anengos"]["url"] == "https://siyu-ai.com/mcp"
        assert d["claude_config"]["mcpServers"]["anengos"]["headers"]["Authorization"] == f"Bearer {token}"
        assert "knowledge_ask" in d["tools"]
        assert "curl" in d["quick_check"] and "initialize" in d["quick_check"]
        assert "5 分/次" in d["usage_hint"]
    finally:
        srv.shutdown()


def test_install_admin_guidance(monkeypatch, tmp_path):
    """管理员身份：给出指引（MCP 只认租户 token），不泄露管理员 token。"""
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/install", token="admin123")
        assert code == 200
        assert d["role"] == "admin" and d["claude_config"] is None
        assert "租户 token" in d["note"]
    finally:
        srv.shutdown()
