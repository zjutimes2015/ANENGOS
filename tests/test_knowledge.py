"""知识库（阶段1）测试：上传/列表/检索(中文2-gram)/租户隔离/删除/大小上限。"""
import base64
import json

from test_quota import _request

from test_auth import _setup


def _mk_tenant(url):
    code, d = _request(url + "/admin/api/tenants", {"name": "知识库客户"}, method="POST", token="admin123")
    assert code == 200
    return d["tenant_id"], d["token"]


def _upload(url, tid, token, filename="报价单.txt", text="本公司报价单如下：基础版 299 元/月，企业版 1500 元/月。", tags=None):
    return _request(url + f"/admin/api/tenants/{tid}/knowledge/upload",
                    {"filename": filename, "content": base64.b64encode(text.encode()).decode(),
                     "tags": tags or ["报价"]}, method="POST", token=token)


def test_upload_list_search_delete(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        tid, token = _mk_tenant(url)
        code, d = _upload(url, tid, token, tags=["报价", "套餐"])
        assert code == 201 and d["doc_id"]
        doc_id = d["doc_id"]
        # 列表
        code, d = _request(url + f"/admin/api/tenants/{tid}/knowledge", token=token)
        assert code == 200 and d["doc_count"] == 1 and d["docs"][0]["filename"] == "报价单.txt"
        assert d["docs"][0]["tags"] == ["报价", "套餐"]
        # 检索：中文 2-gram（"报价"、"报价单"均命中）
        code, d = _request(url + f"/admin/api/tenants/{tid}/knowledge/search",
                           {"query": "报价"}, method="POST", token=token)
        assert code == 200 and len(d["results"]) == 1
        assert d["results"][0]["doc_id"] == doc_id and "报价单" in d["results"][0]["snippet"]
        code, d = _request(url + f"/admin/api/tenants/{tid}/knowledge/search",
                           {"query": "企业版 1500"}, method="POST", token=token)
        assert code == 200 and d["results"] and d["results"][0]["doc_id"] == doc_id
        # 英文/数字词命中
        code, d = _request(url + f"/admin/api/tenants/{tid}/knowledge/search",
                           {"query": "299"}, method="POST", token=token)
        assert code == 200 and d["results"]
        # 无命中
        code, d = _request(url + f"/admin/api/tenants/{tid}/knowledge/search",
                           {"query": "不存在的词xyz"}, method="POST", token=token)
        assert code == 200 and d["results"] == []
        # 删除
        code, d = _request(url + f"/admin/api/tenants/{tid}/knowledge/{doc_id}/delete", {}, method="POST", token=token)
        assert code == 200
        code, d = _request(url + f"/admin/api/tenants/{tid}/knowledge", token=token)
        assert d["doc_count"] == 0
        # 重复删除 404
        code, d = _request(url + f"/admin/api/tenants/{tid}/knowledge/{doc_id}/delete", {}, method="POST", token=token)
        assert code == 404
    finally:
        srv.shutdown()


def test_tenant_isolation_and_admin_scope(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        tid_a, tok_a = _mk_tenant(url)
        tid_b, tok_b = _mk_tenant(url)
        _upload(url, tid_a, tok_a, "a.txt", "这是 A 客户的机密报价")
        _upload(url, tid_b, tok_b, "b.txt", "这是 B 客户的年报数据")
        # 租户 B 访问 A 的知识库 -> 403
        code, d = _request(url + f"/admin/api/tenants/{tid_a}/knowledge", token=tok_b)
        assert code == 403
        code, d = _request(url + f"/admin/api/tenants/{tid_a}/knowledge/search", {"query": "机密"}, method="POST", token=tok_b)
        assert code == 403
        # 管理员可管理任意租户
        code, d = _request(url + f"/admin/api/tenants/{tid_a}/knowledge", token="admin123")
        assert code == 200 and d["doc_count"] == 1 and "A 客户" in d["docs"][0]["filename"] or d["docs"][0]["filename"] == "a.txt"
        # 各自检索互不可见（B 搜 A 的词无命中——403 已挡；B 搜自己的词命中）
        code, d = _request(url + f"/admin/api/tenants/{tid_b}/knowledge/search", {"query": "年报"}, method="POST", token=tok_b)
        assert code == 200 and d["results"]
    finally:
        srv.shutdown()


def test_upload_rejects_bad_payload(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        tid, token = _mk_tenant(url)
        # 空文件名
        code, d = _upload(url, tid, token, filename="   ")
        assert code == 400
        # 空内容
        code, d = _request(url + f"/admin/api/tenants/{tid}/knowledge/upload",
                           {"filename": "x.txt", "content": "", "tags": []}, method="POST", token=token)
        assert code == 400
        # 超 2MB
        big = base64.b64encode(b"x" * (2 * 1024 * 1024 + 1)).decode()
        code, d = _request(url + f"/admin/api/tenants/{tid}/knowledge/upload",
                           {"filename": "big.txt", "content": big, "tags": []}, method="POST", token=token)
        assert code == 413
    finally:
        srv.shutdown()
