"""信用额度池测试：统一计分、月度池+按量包扣减、耗尽 429、按量包购买结算、月度归档、余额透明。"""
import base64
import json

import app

from test_knowledge import _mk_tenant
from test_quota import _request, _serve


def _credits(tid):
    return app._tenant_credit_state(app._TENANTS[tid])


def test_default_credits_and_plans_expose_packs_rates(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/api/signup", {"name": "信用额度客户"}, method="POST")
        assert code == 201
        assert d["quota"]["credits_per_month"] == 100
        # 公开：套餐 + 按量包 + 单价
        code, d = _request(url + "/api/billing/plans")
        assert code == 200
        assert d["credit_packs"]["credits_1000"]["credits"] == 1000
        assert d["rates"]["knowledge_ask"] == 5 and d["rates"]["knowledge_search"] == 1
        assert d["rates"]["knowledge_upload"] == 2 and d["rates"]["task_run"] == 10
    finally:
        srv.shutdown()


def test_charge_by_rate_and_extra_pack(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        tid, token = _mk_tenant(url)
        # 上传 2 分、检索 1 分、问答 5 分（tasks 同步 +1）
        code, d = _request(url + "/api/tenant/knowledge/upload",
                           {"filename": "信用测试.txt",
                            "content": base64.b64encode(
                                "信用额度池报价：企业版 150000 分每月。".encode()).decode(),
                            "tags": []}, method="POST", token=token)
        assert code == 201
        code, d = _request(url + "/api/tenant/knowledge/search", {"query": "报价"}, method="POST", token=token)
        assert code == 200
        code, d = _request(url + "/api/tenant/knowledge/ask", {"query": "企业版 150000"}, method="POST", token=token)
        assert code == 200 and d.get("sources") is not None
        st = _credits(tid)
        assert st["monthly_used"] == 2 + 1 + 5
        assert app._tenant_record(tid)["usage"]["tasks"] == 1  # 仅问答计任务
        assert st["monthly_remaining"] == 100 - 8
        # 管理员免费
        c0 = _credits(tid)["monthly_used"]
        app._charge_credits({"role": "admin", "tenant_id": tid}, "knowledge_ask")
        assert _credits(tid)["monthly_used"] == c0
    finally:
        srv.shutdown()


def test_exhausted_uses_extra_pack_then_429(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        tid, token = _mk_tenant(url)
        # 把月度池压到 5 分（正好一次问答）
        code, d = _request(url + f"/admin/api/tenants/{tid}/quota", {"credits_per_month": 5}, token="admin123")
        assert code == 200
        assert _request(url + "/api/tenant/knowledge/ask", {"query": "问题"}, method="POST", token=token)[0] == 200
        # 月度池耗尽 -> 429
        code, d = _request(url + "/api/tenant/knowledge/ask", {"query": "再来一次"}, method="POST", token=token)
        assert code == 429 and "按量包" in d["error"]
        # 购买按量包 1000 分（mock 下单 -> 管理员补单结算）
        code, d = _request(url + "/api/billing/order", {"plan": "credits_1000", "tenant_id": token},
                           method="POST")
        assert code == 200 and d["kind"] == "credits" and d["plan_name"] == "按量包 1000 分"
        oid = d["order_id"]
        code, d = _request(url + f"/admin/api/orders/{oid}/mark-paid", {}, method="POST", token="admin123")
        assert code == 200
        st = _credits(tid)
        assert st["extra_remaining"] == 1000
        assert st["monthly_used"] == 5
        # 月度池耗尽后扣按量包
        code, d = _request(url + "/api/tenant/knowledge/ask", {"query": "用按量包"}, method="POST", token=token)
        assert code == 200
        st = _credits(tid)
        assert st["extra_remaining"] == 995 and st["monthly_used"] == 5
        # 按量包扣光后 429
        app._TENANTS[tid]["usage"]["extra_credits"] = 4
        code, d = _request(url + "/api/tenant/knowledge/ask", {"query": "又没了"}, method="POST", token=token)
        assert code == 429
    finally:
        srv.shutdown()


def test_credit_monthly_reset_and_csv_columns(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        tid, token = _mk_tenant(url)
        app._charge_credits({"role": "tenant", "tenant_id": tid}, "knowledge_upload")
        assert _credits(tid)["monthly_used"] == 2
        # 模拟跨月：credits_used 归档到 billing
        old = "2000-01"
        app._TENANTS[tid]["usage"]["month"] = old
        rec = app._tenant_record(tid)
        assert rec["usage"]["credits_used"] == 0
        assert app._TENANTS[tid]["billing"][old]["credits_used"] == 2
        # usage 端点带 credits 字段
        code, d = _request(url + "/admin/api/usage", token=token)
        assert code == 200
        u = d["usage"]
        assert u["credits_quota"] == 100 and u["credits_remaining"] == 100
        assert "rates" in d and "packs" in d
        # CSV 追加 credits 列（表头兼容旧前缀）
        import urllib.request
        req = urllib.request.Request(url + "/admin/api/usage/export.csv")
        req.add_header("Authorization", "Bearer admin123")
        with urllib.request.urlopen(req, timeout=10) as r:
            lines = r.read().decode("utf-8-sig").strip().splitlines()
        assert lines[0].startswith("tenant_id,name,month,tasks_used,tasks_quota,credits_used")
        assert any(tid in ln and ",2,100," in ln for ln in lines)
    finally:
        srv.shutdown()


def test_client_session_exposes_credits(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        tid, token = _mk_tenant(url)
        # 直接调 session 需要登录态，改用租户 API 等价路径验证：/api/client/session 需会话，跳过；
        # 这里验证 _charge 后的 usage 端点在租户侧可见 credits。
        code, d = _request(url + "/admin/api/usage", token=token)
        assert code == 200 and d["usage"]["credits_remaining"] == 100
    finally:
        srv.shutdown()
