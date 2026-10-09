"""知识库 RAG（阶段2）测试：切块、块级检索、AI 问答（LLM 失败降级）、权限。"""
import base64
import json

from test_quota import _request
from test_auth import _setup
from test_knowledge import _mk_tenant


def _upload_doc(url, tid, token, filename, text, tags=None):
    return _request(url + f"/admin/api/tenants/{tid}/knowledge/upload",
                    {"filename": filename, "content": base64.b64encode(text.encode()).decode(),
                     "tags": tags or []}, method="POST", token=token)


def test_chunking_and_chunk_index(monkeypatch, tmp_path):
    import app
    from test_api_admin import _reset_state
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    monkeypatch.setattr(app, "_ACCOUNTS_FILE", tmp_path / "accounts.json")
    app._load_accounts()
    text = "。".join(f"第{i}段：关于产品定价与交付周期的说明内容" for i in range(1, 30))
    chunks = app._chunk_text(text)
    assert len(chunks) >= 2
    assert all(len(c) <= app.CHUNK_SIZE + app.CHUNK_OVERLAP + 20 for c in chunks)
    assert "".join(chunks).strip() == text.strip() or len("".join(chunks)) >= len(text) * 0.9  # 有重叠不应丢内容


def test_ask_returns_sources_when_llm_down(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        tid, token = _mk_tenant(url)
        text = ("本公司报价单：基础版每月 299 元，企业版每月 1500 元，按年付享 9 折。" * 5 +
                "交付周期 7 个工作日，含私有化部署与团队培训。售后支持 12 个月，响应时限 4 小时。" * 5)
        _upload_doc(url, tid, token, "报价与交付.txt", text, ["报价"])
        # ask：测试环境 LLM 指向 127.0.0.1:1（不可达）-> 降级返回检索来源
        code, d = _request(url + f"/admin/api/tenants/{tid}/knowledge/ask",
                           {"query": "企业版多少钱"}, method="POST", token=token)
        assert code == 200
        assert d["sources"], "至少应有检索来源"
        assert d["mode"] in ("bm25", "vector")
        # 命中内容与问题相关（块级检索把含"1500"的块带出来）
        joined = " ".join(s["snippet"] for s in d["sources"])
        assert "1500" in joined
        # 无命中
        code, d = _request(url + f"/admin/api/tenants/{tid}/knowledge/ask",
                           {"query": "火星移民计划"}, method="POST", token=token)
        assert code == 200 and d["sources"] == [] and d["error"]
    finally:
        srv.shutdown()


def test_ask_tenant_isolation(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        tid_a, tok_a = _mk_tenant(url)
        tid_b, tok_b = _mk_tenant(url)
        _upload_doc(url, tid_a, tok_a, "a.txt", "A 客户机密报价：年费 10000 元。")
        _upload_doc(url, tid_b, tok_b, "b.txt", "B 客户资料：团队 5 人。")
        code, d = _request(url + f"/admin/api/tenants/{tid_a}/knowledge/ask",
                           {"query": "年费多少"}, method="POST", token=tok_b)
        assert code == 403
        code, d = _request(url + f"/admin/api/tenants/{tid_b}/knowledge/ask",
                           {"query": "团队几人"}, method="POST", token=tok_b)
        assert code == 200 and d["sources"]
        # 管理员可问答任意租户
        code, d = _request(url + f"/admin/api/tenants/{tid_a}/knowledge/ask",
                           {"query": "年费"}, method="POST", token="admin123")
        assert code == 200 and d["sources"]
    finally:
        srv.shutdown()
