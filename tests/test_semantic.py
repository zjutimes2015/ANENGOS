"""知识库语义检索（真向量）+ 租户知识库 API 测试。"""
import base64
import json

from test_quota import _request
from test_auth import _setup
from test_knowledge import _mk_tenant


def _upload_doc(url, tid, token, filename, text, tags=None, path=None):
    p = path or f"/admin/api/tenants/{tid}/knowledge/upload"
    return _request(url + p,
                    {"filename": filename, "content": base64.b64encode(text.encode()).decode(),
                     "tags": tags or []}, method="POST", token=token)


def _fake_embed(texts):
    """确定性伪向量：词 token 的 one-hot 累加（同一进程内 hash 稳定，可验证向量链路）。"""
    out = []
    for t in texts:
        vec = [0.0] * 64
        for tok in _tokenize_all(t):
            vec[abs(hash(tok)) % 64] += 1.0
        out.append(vec)
    return out


def _tokenize_all(text):
    import app
    toks = set(app._tokenize(text))
    for ch in text:
        if ch.strip():
            toks.add("c:" + ch)
    return toks


def test_upload_precomputes_vectors_and_vector_mode(monkeypatch, tmp_path):
    import app
    from test_api_admin import _reset_state
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    monkeypatch.setenv("ANENGOS_EMBEDDING_MODEL", "fake-embed")
    monkeypatch.setattr(app, "_embed_batch", _fake_embed)
    app._load_accounts()
    # 直接调用上传函数（免起 HTTP 服务）
    tid = "abc123"
    t = app._TENANTS.setdefault(tid, {"name": "t", "status": "active", "quota": dict(app.DEFAULT_QUOTA),
                                      "usage": {"tasks": 0, "month": app._current_month()}, "token_hash": "x"})
    text = ("企业版每月 1500 元，含 1000 次任务。私有化部署周期 7 个工作日。" * 4)
    code, body = app._upload_knowledge(tid, "报价.txt", base64.b64encode(text.encode()).decode(), [], {"role": "admin"})
    assert code in (200, 201)
    doc_id = body["doc_id"]
    vecs = app._load_doc_vectors(tid, doc_id)
    assert vecs and len(vecs) == len(app._load_doc_chunks(tid, doc_id)), "上传后应预计算全部 chunk 向量"
    # ask 应为 vector 模式（LLM 不可达，返回检索来源）
    out = app._ask_knowledge(tid, "企业版多少钱", {"role": "admin"})
    assert out["mode"] == "vector", f"期望 vector，实际 {out['mode']}"
    assert out["sources"]
    joined = " ".join(s["snippet"] for s in out["sources"])
    assert "1500" in joined
    # 语义检索价值：改写问法（同义表达）也能命中语义相关块
    vec_out = app._ask_knowledge(tid, "私有化要搞多久", {"role": "admin"})
    assert vec_out["mode"] == "vector" and vec_out["sources"] and "7 个工作日" in " ".join(s["snippet"] for s in vec_out["sources"])


def test_vector_failure_falls_back_to_bm25(monkeypatch, tmp_path):
    import app
    from test_api_admin import _reset_state
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    monkeypatch.setenv("ANENGOS_EMBEDDING_MODEL", "fake-embed")
    monkeypatch.setattr(app, "_embed_batch", lambda texts: None)  # embedding 全失败
    app._load_accounts()
    tid = "def456"
    app._TENANTS.setdefault(tid, {"name": "t", "status": "active", "quota": dict(app.DEFAULT_QUOTA),
                                  "usage": {"tasks": 0, "month": app._current_month()}, "token_hash": "x"})
    code, body = app._upload_knowledge(tid, "报价.txt",
                                       base64.b64encode("企业版每月 1500 元，交付 7 个工作日。".encode()).decode(),
                                       [], {"role": "admin"})
    assert code in (200, 201)
    assert app._load_doc_vectors(tid, body["doc_id"]) == [], "embedding 失败不应落盘向量"
    out = app._ask_knowledge(tid, "交付周期", {"role": "admin"})
    assert out["mode"] == "bm25" and out["sources"]


def test_no_embedding_env_keeps_bm25(monkeypatch, tmp_path):
    import app
    from test_api_admin import _reset_state
    _reset_state(monkeypatch, tmp_path)
    monkeypatch.setenv("ANENGOS_API_TOKEN", "admin123")
    monkeypatch.delenv("ANENGOS_EMBEDDING_MODEL", raising=False)
    app._load_accounts()
    tid = "ghi789"
    app._TENANTS.setdefault(tid, {"name": "t", "status": "active", "quota": dict(app.DEFAULT_QUOTA),
                                  "usage": {"tasks": 0, "month": app._current_month()}, "token_hash": "x"})
    code, body = app._upload_knowledge(tid, "报价.txt",
                                       base64.b64encode("企业版每月 1500 元，交付 7 个工作日。".encode()).decode(),
                                       [], {"role": "admin"})
    assert code in (200, 201)
    out = app._ask_knowledge(tid, "交付周期", {"role": "admin"})
    assert out["mode"] == "bm25" and out["sources"]


def test_tenant_knowledge_api_full_flow(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        tid, token = _mk_tenant(url)
        # 无 token -> 401
        code, d = _request(url + "/api/tenant/knowledge", method="GET", token=None)
        assert code == 401
        # 管理员 -> 403
        code, d = _request(url + "/api/tenant/knowledge", method="GET", token="admin123")
        assert code == 403
        # 租户上传自己的资料
        code, d = _upload_doc(url, tid, token, "我的资料.txt",
                              "我们公司 2026 年计划招聘 20 人，预算 300 万。", path="/api/tenant/knowledge/upload")
        assert code in (200, 201), d
        doc_id = d["doc_id"]
        # 列表
        code, d = _request(url + "/api/tenant/knowledge", method="GET", token=token)
        assert code == 200 and d["doc_count"] == 1
        # AI 问答（无 embedding env：bm25；LLM 不可达：返回来源）并计一次用量
        code, d = _request(url + "/api/tenant/knowledge/ask", {"query": "招聘多少人"}, method="POST", token=token)
        assert code == 200 and d["sources"] and d["mode"] == "bm25"
        import app
        assert app._TENANTS[tid]["usage"]["tasks"] == 1, "AI 问答应计入租户任务用量"
        # 检索 + 删除
        code, d = _request(url + "/api/tenant/knowledge/search", {"query": "招聘"}, method="POST", token=token)
        assert code == 200 and d["results"]
        code, d = _request(url + f"/api/tenant/knowledge/{doc_id}/delete", method="POST", token=token)
        assert code == 200
        code, d = _request(url + "/api/tenant/knowledge", method="GET", token=token)
        assert code == 200 and d["doc_count"] == 0
    finally:
        srv.shutdown()


def test_tenant_knowledge_quota_429(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        tid, token = _mk_tenant(url)
        # 信用额度清零 -> ask 应 429（知识问答按信用池计费：1 次 5 分）
        code, d = _request(url + f"/admin/api/tenants/{tid}/quota",
                           {"credits_per_month": 0}, method="POST", token="admin123")
        assert code == 200
        code, d = _request(url + "/api/tenant/knowledge/ask", {"query": "随便问问"}, method="POST", token=token)
        assert code == 429 and "信用额度" in str(d.get("error", ""))
        # search 也计入信用池（1 分）：额度为 0 时同样 429
        code, d = _request(url + "/api/tenant/knowledge/search", {"query": "随便"}, method="POST", token=token)
        assert code == 429
    finally:
        srv.shutdown()

