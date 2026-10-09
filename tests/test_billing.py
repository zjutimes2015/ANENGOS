"""支付计费测试：下单、mock 回调结算、幂等、配额升级、管理员补单、权限。"""
import urllib.request

import app

from test_quota import _serve, _request


def test_order_create_settle_upgrade_quota(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/api/signup", {"name": "付费客户"}, method="POST")
        tid, token = d["tenant_id"], d["token"]
        # 创建订单（用租户 token 作为 tenant_id）
        code, o = _request(url + "/api/billing/order",
                           {"plan": "team", "tenant_id": token, "name": "付费客户"}, method="POST")
        assert code == 200 and o["plan"] == "team" and o["amount_cents"] == 29900
        assert o["status"] == "pending"
        oid = o["order_id"]
        # 未支付前配额不变
        assert app._tenant_record(tid)["quota"]["tasks_per_month"] == 100
        # mock 回调结算
        code, r = _request(url + "/api/billing/notify", {"order_id": oid}, method="POST")
        assert code == 200 and r["status"] == "paid" and r["tenant_id"] == tid
        # 配额升级
        rec = app._tenant_record(tid)
        assert rec["quota"]["tasks_per_month"] == 1000
        assert rec["quota"]["agents"] == 3
        assert rec["plan"] == "team"
        # 幂等：重复回调不重复计费
        code, r = _request(url + "/api/billing/notify", {"order_id": oid}, method="POST")
        assert code == 200 and r.get("already") is True
    finally:
        srv.shutdown()


def test_order_invalid_and_unknown(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        # 未知套餐
        code, r = _request(url + "/api/billing/order", {"plan": "platinum"}, method="POST")
        assert code == 400
        # 试用版禁止下单（走自助开通）
        code, r = _request(url + "/api/billing/order", {"plan": "trial"}, method="POST")
        assert code == 400
        # 不存在的租户
        code, r = _request(url + "/api/billing/order", {"plan": "team", "tenant_id": "no_such_token"}, method="POST")
        assert code == 404
        # 查不存在的订单
        code, r = _request(url + "/api/billing/order?id=od_nope")
        assert code == 404
    finally:
        srv.shutdown()


def test_admin_mark_paid_and_permission(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, d = _request(url + "/api/signup", {"name": "补单客户"}, method="POST")
        tid, token = d["tenant_id"], d["token"]
        code, o = _request(url + "/api/billing/order",
                           {"plan": "enterprise", "tenant_id": token}, method="POST")
        oid = o["order_id"]
        # 非管理员不能补单
        code, r = _request(url + f"/admin/api/orders/{oid}/mark-paid", {}, method="POST", token=token)
        assert code == 403
        # 管理员补单
        code, r = _request(url + f"/admin/api/orders/{oid}/mark-paid", {}, method="POST", token="admin123")
        assert code == 200 and r["status"] == "paid"
        rec = app._tenant_record(tid)
        assert rec["quota"]["tasks_per_month"] == 10000 and rec["plan"] == "enterprise"
        # 订单列表管理员可见
        code, r = _request(url + "/admin/api/orders", token="admin123")
        assert code == 200 and any(x["order_id"] == oid for x in r["orders"])
        # 非管理员不可见订单列表
        code, r = _request(url + "/admin/api/orders", token=token)
        assert code == 403
    finally:
        srv.shutdown()


def test_billing_plans_public(monkeypatch, tmp_path):
    srv, url = _serve(monkeypatch, tmp_path)
    try:
        code, r = _request(url + "/api/billing/plans")
        assert code == 200 and "team" in r["plans"] and "enterprise" in r["plans"]
        assert r["plans"]["team"]["price_cents"] == 29900
    finally:
        srv.shutdown()
