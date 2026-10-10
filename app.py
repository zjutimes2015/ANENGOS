"""ANENGOS 极简 HTTP 服务：真实模型 + 治理闭环 + Web 管理台 + 多租户，供 Docker 一键部署。

端点：
  GET  /health                  存活检查（Docker healthcheck 用）
  POST /run                     {"query": "..."} 跑一个受治理的 agent 任务（需鉴权）
  GET  /                        管理台页面（admin.html）
  POST /admin/api/run           管理台任务入口（同 /run，需鉴权）
  GET  /admin/api/me            当前身份（管理员 / 租户，需鉴权）
  GET  /admin/api/approvals     待审批队列（管理员看全局 / 租户看自己，需鉴权）
  POST /admin/api/approvals/{id}/approve|reject   批准（真实执行）/ 拒绝（需鉴权）
  POST /admin/api/approvals/approve-all           批量批准
  GET  /admin/api/audit         最近审计日志（需鉴权）
  GET  /admin/api/files         工作区文件列表（需鉴权）
  GET  /admin/api/tenants       租户列表（仅管理员）
  POST /admin/api/tenants       创建租户（仅管理员，返回一次性明文 token）
  POST /admin/api/tenants/{id}/revoke   停用租户（仅管理员）

多租户模型：
  ANENGOS_API_TOKEN 是管理员令牌；管理员可创建租户（客户），每个租户获得
  独立 token、独立工作区（workspace/tenants/{id}）、独立审批队列、独立审计
  文件（audit/tenants/{id}/audit.jsonl）。租户间完全隔离。

配置：
  ANENGOS_API_KEY / ANENGOS_BASE_URL / ANENGOS_MODEL / ANENGOS_PORT（默认 8080）
  ANENGOS_API_TOKEN  管理员令牌：未配置时全部接口拒绝服务；配置后请求必须带
                     Authorization: Bearer <token> 或 X-API-Token: <token>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, register_adapter
from connectors.codex import CodexAdapter
from connectors.doubao import DoubaoAdapter
from connectors.grok import GrokAdapter
from governance.approval import ApprovalItem, ApprovalQueue
from governance.audit import AuditLog
from governance.capability import CapabilityRegistry
from governance.gatekeeper import Gatekeeper
from governance.reviewer import Reviewer
from kernel.llm import OpenAICompatLLM
from kernel.loop import AgentOS
from kernel.tools import ToolRegistry, build_default_tools

ACTOR = "anengos-admin"
BASE = Path(os.environ.get("ANENGOS_HOME", "."))
WORKSPACE = BASE / "workspace"
AUDIT_FILE = BASE / "audit" / "audit.jsonl"
ADMIN_HTML = BASE / "admin.html"
TENANTS_FILE = BASE / "tenants.json"
TASKS_FILE = BASE / "tasks.json"  # 异步任务队列持久化（Failover：进程重启后断点续跑/重放）

# 全局单例：管理员审批队列、管理员工具注册表、租户表、租户审批队列。
_APPROVALS: ApprovalQueue | None = None
_TOOLS: ToolRegistry | None = None
_TENANTS: dict[str, dict[str, Any]] = {}
_TENANT_QUEUES: dict[str, ApprovalQueue] = {}

# 多智能体总装线：外部智能体注册表（Codex / 豆包 / Grok），统一 AgentAdapter 接口。
# 各适配器按自身 env 自动选择真实（http/cli）或演示（mock）模式。
_EXTERNAL_AGENTS: dict[str, AgentAdapter] = {
    "codex": CodexAdapter(),
    "doubao": DoubaoAdapter(),
    "grok": GrokAdapter(),
}

_REVIEWER: Reviewer | None = None

# 异步任务队列：后台线程执行长任务，/admin/api/tasks 可查状态，不阻塞 HTTP。
_TASKS: dict[str, dict[str, Any]] = {}
_TASKS_LOCK = __import__("threading").Lock()

# ---------- 租户用量配额与计费（信用额度池：所有能力共享月度分数，按调用计分） ----------
# 借鉴 AgentKey：一套信用额度池覆盖全部能力（search/ask/upload/任务），不同操作不同单价；
# 月度配额每月 1 日 UTC 重置，用尽后可购买"按量包"（extra credits，买断制不过期）。
# 租户记录：quota={tasks_per_month, credits_per_month, agents, storage_mb}，
#           usage={tasks, credits_used, extra_credits, month}。
DEFAULT_QUOTA: dict[str, int] = {"tasks_per_month": 100, "credits_per_month": 100, "agents": 1, "storage_mb": 100}
# 各操作单价（credits/次）：AI 问答最贵（检索+生成），任务执行次之，检索/上传轻量。
CREDIT_RATES: dict[str, int] = {
    "knowledge_ask": 5,
    "knowledge_search": 1,
    "knowledge_upload": 2,
    "task_run": 10,
}
# 按量包（超额购买）：结算后计入租户 extra_credits，不随月度重置，用完为止。
CREDIT_PACKS: dict[str, dict[str, Any]] = {
    "credits_1000": {"name": "按量包 1000 分", "price_cents": 900, "credits": 1000},
    "credits_5000": {"name": "按量包 5000 分", "price_cents": 4000, "credits": 5000},
}
# 自助开通防滥用：同一 IP 每天最多创建 5 个租户（内存限流）。
_SIGNUP_LIMIT = 5
_SIGNUP_HITS: dict[str, list[str]] = {}


def _current_month() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def _tenant_record(tid: str) -> dict[str, Any] | None:
    """租户记录；历史数据自动补齐 quota/usage 默认值（向前兼容）。"""
    t = _TENANTS.get(tid)
    if t is None:
        return None
    if "quota" not in t or not isinstance(t["quota"], dict):
        t["quota"] = dict(DEFAULT_QUOTA)
    q = t["quota"]
    q.setdefault("credits_per_month", q.get("tasks_per_month", 100))
    if "usage" not in t or not isinstance(t["usage"], dict):
        t["usage"] = {"tasks": 0, "credits_used": 0, "extra_credits": 0, "month": _current_month()}
    u = t["usage"]
    u.setdefault("credits_used", 0)
    u.setdefault("extra_credits", 0)
    if u.get("month") != _current_month():  # 月度滚动：归档上月用量为账单记录
        t.setdefault("billing", {})
        t["billing"][u["month"]] = {"tasks": u.get("tasks", 0),
                                    "credits_used": u.get("credits_used", 0)}
        u["tasks"] = 0
        u["credits_used"] = 0
        u["month"] = _current_month()
        _WEBHOOK_FIRED.pop(tid, None)  # 新账期重新允许告警
    return t


def _credit_rate(key: str) -> int:
    return CREDIT_RATES.get(key, 0)


def _tenant_credit_state(t: dict[str, Any]) -> dict[str, Any]:
    """信用额度池状态：月度池 + 按量包余额。"""
    u = t["usage"]
    limit = t["quota"].get("credits_per_month", 0)
    used = u.get("credits_used", 0)
    extra = u.get("extra_credits", 0)
    return {
        "monthly_limit": limit,
        "monthly_used": used,
        "monthly_remaining": max(limit - used, 0),
        "extra_remaining": extra,
        "total_remaining": max(limit - used, 0) + extra,
    }


def _charge_credits(principal: dict[str, Any], rate_key: str) -> str | None:
    """按单价扣减信用额度（管理员与免费操作不扣）。成功返回 None，额度不足返回错误文案。

    扣减顺序：先月度池，再按量包（extra_credits，买断制）。扣完持久化并触发告警检查。
    """
    if principal["role"] != "tenant":
        return None
    cost = _credit_rate(rate_key)
    if cost <= 0:
        return None
    t = _tenant_record(principal["tenant_id"])
    if t is None:
        return "租户不存在"
    u = t["usage"]
    limit = t["quota"].get("credits_per_month", 0)
    used = u.get("credits_used", 0)
    extra = u.get("extra_credits", 0)
    if used + cost <= limit:
        u["credits_used"] = used + cost
        _save_tenants()
        _maybe_webhook(principal)
        return None
    if extra >= cost:
        u["extra_credits"] = extra - cost
        _save_tenants()
        return None
    return (f"本月信用额度已用尽（{used}/{limit}），按量包余额 {extra} 分不足，"
            f"请联系管理员购买按量包或等待下月重置")


def _quota_error(principal: dict[str, Any]) -> str | None:
    """租户配额校验；管理员不受限。返回错误信息或 None。"""
    if principal["role"] != "tenant":
        return None
    t = _tenant_record(principal["tenant_id"])
    if t is None:
        return "租户不存在"
    used = t["usage"].get("tasks", 0)
    limit = t["quota"].get("tasks_per_month", 0)
    if used >= limit:
        return f"本月任务配额已用尽（{used}/{limit}），请联系管理员升级或等待下月重置"
    return None


def _bump_usage(principal: dict[str, Any]) -> None:
    """任务提交成功后租户任务计数 +1 并持久化，同时触发用量告警检查。"""
    if principal["role"] != "tenant":
        return
    t = _tenant_record(principal["tenant_id"])
    if t is None:
        return
    t["usage"]["tasks"] += 1
    _save_tenants()
    _maybe_webhook(principal)


# ---------- 支付与计费（Token 售卖） ----------
# 产品：客户购买租户订阅套餐（配额升级）。金额单位：分（整数，避免浮点误差）。
# 支付通道可插拔：BILLING_PROVIDER = "mock" 为内置演示通道（pay.html 模拟收银台 + notify 回调）；
# 接入真实通道（支付宝/微信）时切换为 "alipay" / "wechat"，密钥从环境变量读取（billing_providers/*.py）。
BILLING_PROVIDER = os.getenv("ANENGOS_BILLING_PROVIDER", "mock").strip().lower() or "mock"
BILLING_PLANS: dict[str, dict[str, Any]] = {
    "trial": {
        "name": "试用版",
        "price_cents": 0,
        "period": "一次性",
        "quota": {"tasks_per_month": 100, "credits_per_month": 100, "agents": 1, "storage_mb": 100},
    },
    "team": {
        "name": "团队版",
        "price_cents": 29900,
        "period": "月",
        "quota": {"tasks_per_month": 1000, "credits_per_month": 29900, "agents": 3, "storage_mb": 1000},
    },
    "enterprise": {
        "name": "企业版",
        "price_cents": 150000,
        "period": "月",
        "quota": {"tasks_per_month": 10000, "credits_per_month": 150000, "agents": 10, "storage_mb": 5000},
    },
}
_ORDERS_FILE = BASE / "orders.json"
_ORDERS: dict[str, dict[str, Any]] = {}


def _load_orders() -> None:
    global _ORDERS
    try:
        _ORDERS = json.loads(_ORDERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        _ORDERS = {}


def _save_orders() -> None:
    _ORDERS_FILE.write_text(json.dumps(_ORDERS, ensure_ascii=False, indent=1), encoding="utf-8")


def _provider(name: str = "") -> Any:
    """按通道名返回适配器模块（mock/alipay/wechat）。"""
    name = (name or BILLING_PROVIDER).strip().lower()
    try:
        if name == "mock":
            from billing_providers import mock
            return mock
        if name == "alipay":
            from billing_providers import alipay
            return alipay
        if name == "wechat":
            from billing_providers import wechat
            return wechat
    except Exception:
        pass
    from billing_providers import mock
    return mock


def _tenant_id_by_token(token: str) -> str | None:
    """按租户 token（或 tenant_id）解析租户 ID。"""
    if token in _TENANTS:
        return token
    h = _hash_token(token)
    for tid, t in _TENANTS.items():
        if t.get("token_hash") == h:
            return tid
    return None


def _create_order(plan_id: str, tenant_ref: str | None, tenant_name: str = "",
                  provider: str = "") -> tuple[int, dict[str, Any]]:
    """创建支付订单；校验套餐/按量包与租户（tenant_ref 可为租户 token 或 tenant_id）。

    非 mock 通道会调用适配器 create_payment 生成真实支付参数（qrcode/code_url）。
    """
    plan = BILLING_PLANS.get(plan_id)
    pack = CREDIT_PACKS.get(plan_id)
    if plan is None and pack is None:
        return 400, {"error": f"未知套餐/按量包：{plan_id}"}
    tid = _tenant_id_by_token(tenant_ref) if tenant_ref else None
    if tenant_ref and tid is None:
        return 404, {"error": "租户不存在，请先自助开通获取 token"}
    if plan_id == "trial":
        return 400, {"error": "试用版免费，请直接使用自助开通"}
    provider = (provider or BILLING_PROVIDER).strip().lower() or "mock"
    if pack is not None:
        kind, name, amount = "credits", pack["name"], pack["price_cents"]
    else:
        kind, name, amount = "subscription", plan["name"], plan["price_cents"]
    order = {
        "order_id": "od_" + secrets.token_hex(8),
        "plan": plan_id,
        "plan_name": name,
        "kind": kind,
        "amount_cents": amount,
        "currency": "CNY",
        "provider": provider,
        "tenant_id": tid,
        "tenant_name": tenant_name or (_TENANTS.get(tid, {}).get("name", "") if tid else ""),
        "status": "pending",  # pending -> paid / cancelled
        "created_at": _ts(),
        "paid_at": None,
    }
    if provider != "mock":
        try:
            pay = _provider(provider).create_payment(order)
            order.update({k: v for k, v in pay.items() if v})
        except Exception as exc:
            return 502, {"error": f"支付通道下单失败：{exc}"}
    else:
        order["mock"] = True
    _ORDERS[order["order_id"]] = order
    _save_orders()
    return 200, order


def _settle_order(order_id: str) -> tuple[int, dict[str, Any]]:
    """支付回调：标记订单已支付并发放权益（订阅升级配额 / 按量包加 extra credits，幂等）。"""
    order = _ORDERS.get(order_id)
    if order is None:
        return 404, {"error": "订单不存在"}
    if order["status"] == "paid":
        return 200, {"ok": True, "order_id": order_id, "status": "paid", "already": True}
    if order["status"] != "pending":
        return 400, {"error": f"订单状态异常：{order['status']}"}
    plan = BILLING_PLANS.get(order["plan"])
    pack = CREDIT_PACKS.get(order["plan"])
    if plan is None and pack is None:
        return 400, {"error": "套餐/按量包已失效"}
    tid = order.get("tenant_id")
    if tid:
        t = _tenant_record(tid)
        if t:
            if pack is not None:  # 按量包：加 extra credits，不升订阅配额
                t["usage"]["extra_credits"] = t["usage"].get("extra_credits", 0) + pack["credits"]
            elif plan is not None:
                for k, v in plan["quota"].items():
                    t["quota"][k] = max(t["quota"].get(k, 0), v)
                t["plan"] = order["plan"]
            t.setdefault("billing", {})
            t["billing"][_current_month()] = {
                "paid": t["billing"].get(_current_month(), {}).get("paid", 0) + order["amount_cents"],
                "orders": t["billing"].get(_current_month(), {}).get("orders", 0) + 1,
            }
            _save_tenants()
    order["status"] = "paid"
    order["paid_at"] = _ts()
    _save_orders()
    return 200, {"ok": True, "order_id": order_id, "status": "paid", "tenant_id": tid,
                 "plan": order["plan"], "kind": order.get("kind", "subscription"),
                 "amount_cents": order["amount_cents"]}


def _all_orders() -> list[dict[str, Any]]:
    return [dict(o) for o in sorted(_ORDERS.values(), key=lambda x: x.get("created_at", ""), reverse=True)]


# ---------- Web 登录与会话（Better Auth 风格：账号密码 + 会话 Cookie + 图形验证码） ----------
# 能力等价 Better Auth 核心：本地账号、会话 Cookie（HttpOnly）、登出、防爆破；
# OAuth 社交登录为扩展点（后续可加 GitHub/微信适配器，同一会话模型）。
_ACCOUNTS_FILE = BASE / "accounts.json"
_ACCOUNTS: dict[str, dict[str, Any]] = {}
_SESSIONS: dict[str, dict[str, Any]] = {}      # sid -> {username, role, tenant_id?, exp}
_CAPTCHAS: dict[str, dict[str, Any]] = {}      # cid -> {answer, exp}
_LOGIN_FAILS: dict[str, dict[str, Any]] = {}   # key -> {count, until}
SESSION_TTL = 12 * 3600
CAPTCHA_TTL = 300
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECS = 15 * 60


def _load_accounts() -> None:
    global _ACCOUNTS
    try:
        _ACCOUNTS = json.loads(_ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        _ACCOUNTS = {}


def _save_accounts() -> None:
    _ACCOUNTS_FILE.write_text(json.dumps(_ACCOUNTS, ensure_ascii=False, indent=2), encoding="utf-8")


def _hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 120_000)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def _verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        return hmac.compare_digest(_hash_password(pw, salt).split("$")[2], dk_b64)
    except Exception:
        return False


def _ensure_admin() -> None:
    """首次启动确保管理员账号存在（env ANENGOS_ADMIN_USER / ANENGOS_ADMIN_PASS）。"""
    user = os.getenv("ANENGOS_ADMIN_USER", "admin").strip()
    if user in _ACCOUNTS:
        return
    pw = os.getenv("ANENGOS_ADMIN_PASS", "").strip()
    if not pw:
        pw = "anengos-admin-" + secrets.token_hex(4)
    _ACCOUNTS[user] = {
        "password_hash": _hash_password(pw),
        "role": "admin",
        "created_at": _ts(),
    }
    _save_accounts()


def _new_captcha() -> tuple[str, str]:
    """生成算术验证码：返回 (id, dataURL PNG 图片)。答案存内存，5 分钟过期。"""
    from PIL import Image, ImageDraw, ImageFont
    import io
    import base64 as b64

    a, b = secrets.randbelow(8) + 2, secrets.randbelow(8) + 2
    op = secrets.choice("+-×")
    if op == "-" and a < b:
        a, b = b, a
    ans = {"+": a + b, "-": a - b, "×": a * b}[op]
    cid = secrets.token_hex(6)
    _CAPTCHAS[cid] = {"answer": str(ans), "exp": time.time() + CAPTCHA_TTL}
    img = Image.new("RGB", (240, 80), (240, 244, 248))
    d = ImageDraw.Draw(img)
    for _ in range(6):
        d.line([(secrets.randbelow(240), secrets.randbelow(80)),
                (secrets.randbelow(240), secrets.randbelow(80))],
               fill=(secrets.randbelow(200) + 30,) * 3, width=1)
    for _ in range(150):
        d.point((secrets.randbelow(240), secrets.randbelow(80)),
                fill=(secrets.randbelow(255), secrets.randbelow(255), secrets.randbelow(255)))
    try:
        font = ImageFont.load_default(size=46)
    except TypeError:
        font = ImageFont.load_default()
    d.text((22, 14), f"{a} {op} {b} = ?", font=font, fill=(31, 41, 55))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return cid, "data:image/png;base64," + b64.b64encode(buf.getvalue()).decode()


def _check_captcha(cid: str, answer: str) -> bool:
    c = _CAPTCHAS.pop(cid, None)
    if c is None or time.time() > c["exp"]:
        return False
    return hmac.compare_digest(c["answer"].strip(), str(answer).strip())


def _cookie_value(cookie_header: str, name: str) -> str:
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


def _new_session(username: str, role: str, tenant_id: str | None = None) -> str:
    sid = secrets.token_hex(24)
    _SESSIONS[sid] = {"username": username, "role": role, "tenant_id": tenant_id,
                      "exp": time.time() + SESSION_TTL}
    return sid


def _session_principal(sid: str) -> dict[str, Any] | None:
    s = _SESSIONS.get(sid)
    if s is None or time.time() > s["exp"]:
        _SESSIONS.pop(sid, None)
        return None
    if s["role"] == "admin":
        return {"role": "admin", "username": s["username"]}
    return {"role": "tenant", "tenant_id": s["tenant_id"], "username": s["username"]}


def _login_rate_key(username: str, ip: str) -> str:
    return f"{username}|{ip}"


def _check_login_lock(key: str) -> bool:
    f = _LOGIN_FAILS.get(key)
    if f and time.time() < f.get("until", 0):
        return True
    return False


def _register_fail(key: str) -> int:
    f = _LOGIN_FAILS.setdefault(key, {"count": 0, "until": 0})
    f["count"] += 1
    if f["count"] >= LOGIN_MAX_FAILS:
        f["until"] = time.time() + LOGIN_LOCK_SECS
    return f["count"]


# ---------- 客户站（租户自助门户）：token 登录 -> 会话 -> 问答 UI ----------
def _tenant_from_token(token: str) -> str | None:
    """校验租户明文 token：命中 active 租户返回 tenant_id，否则 None（管理员 token 不算）。"""
    if not token:
        return None
    admin_tok = _admin_token()
    if admin_tok and hmac.compare_digest(token, admin_tok):
        return None  # 管理员 token 不走客户站
    h = _hash_token(token)
    for tid, t in _TENANTS.items():
        if t.get("status") == "active" and hmac.compare_digest(t.get("token_hash", ""), h):
            return tid
    return None


def _client_session_principal(headers) -> dict[str, Any] | None:
    """仅认租户会话；管理员会话返回 None（管理员请走 /admin）。"""
    sid = _cookie_value(headers.get("Cookie", ""), "anengos_session")
    p = _session_principal(sid) if sid else None
    if p and p["role"] == "tenant":
        return p
    return None


# ---------- 租户知识库（阶段1：文件 + JSON 元数据 + 关键词倒排索引） ----------
# 目录：<租户工作区>/knowledge/{docs/, meta.json, index.json}
# 阶段1 不引入向量库：英文按词、中文按 2-gram 建倒排，检索返回命中文档+片段。
KNOWLEDGE_MAX_SIZE = 2 * 1024 * 1024  # 单文档上限 2MB
KNOWLEDGE_SEARCH_LIMIT = 10


def _knowledge_dir(tid: str) -> Path:
    return _tenant_workspace(tid) / "knowledge"


def _knowledge_meta(tid: str) -> dict[str, Any]:
    try:
        return json.loads((_knowledge_dir(tid) / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return {"docs": {}}


def _save_knowledge_meta(tid: str, meta: dict[str, Any]) -> None:
    (_knowledge_dir(tid) / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _tokenize(text: str) -> list[str]:
    """英文/数字按词，中文按 2-gram 切分（阶段1 关键词索引）。"""
    tokens: list[str] = []
    for m in re.finditer(r"[A-Za-z0-9_]+", text):
        tokens.append(m.group(0).lower())
    for h in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(h) == 1:
            tokens.append(h)
        else:
            tokens.extend(h[i:i + 2] for i in range(len(h) - 1))
    return tokens


def _rebuild_knowledge_index(tid: str, meta: dict[str, Any]) -> dict[str, list[str]]:
    """从 meta 全量重建倒排索引（上传/删除后调用，阶段1 数据量小，直接全量）。"""
    index: dict[str, list[str]] = {}
    for doc_id, info in meta["docs"].items():
        text = (_knowledge_dir(tid) / "docs" / f"{doc_id}.txt").read_text(encoding="utf-8", errors="replace")
        for tok in set(_tokenize(text)):
            index.setdefault(tok, []).append(doc_id)
    return index


def _save_knowledge_index(tid: str, index: dict[str, list[str]]) -> None:
    (_knowledge_dir(tid) / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")


def _upload_knowledge(tid: str, filename: str, content_b64: str, tags: list[str],
                      principal: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    filename = str(filename or "").strip()
    if not filename:
        return 400, {"error": "filename 不能为空"}
    try:
        raw = base64.b64decode(content_b64 or "")
    except Exception:
        return 400, {"error": "content 需为合法 base64"}
    if not raw:
        return 400, {"error": "文档内容为空"}
    if len(raw) > KNOWLEDGE_MAX_SIZE:
        return 413, {"error": f"单文档超过 {KNOWLEDGE_MAX_SIZE // 1024 // 1024}MB 上限（阶段1），大文件请走对象存储"}
    d = _knowledge_dir(tid)
    (d / "docs").mkdir(parents=True, exist_ok=True)
    doc_id = secrets.token_hex(6)
    (d / "docs" / f"{doc_id}.txt").write_bytes(raw)
    meta = _knowledge_meta(tid)
    meta["docs"][doc_id] = {
        "id": doc_id,
        "filename": filename,
        "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
        "size": len(raw),
        "created_at": _ts(),
        "source": "upload",
        "chunk_count": len(_chunk_text(raw.decode("utf-8", errors="replace"))),
    }
    _save_knowledge_meta(tid, meta)
    chunks = _chunk_text(raw.decode("utf-8", errors="replace"))
    _save_doc_chunks(tid, doc_id, chunks)
    vectorized = _vectorize_doc(tid, doc_id, chunks)
    if vectorized:
        meta["docs"][doc_id]["vectorized"] = True
        _save_knowledge_meta(tid, meta)
    _save_knowledge_index(tid, _rebuild_knowledge_index(tid, meta))
    _save_chunk_index(tid, _rebuild_chunk_index(tid, meta))
    _new_audit(principal).log({
        "actor": principal.get("role", "admin"),
        "event": "knowledge.upload",
        "tenant_id": tid,
        "doc_id": doc_id,
        "filename": filename,
        "size": len(raw),
        "allowed": True,
    })
    return 201, {"doc_id": doc_id, "filename": filename, "size": len(raw)}


def _delete_knowledge_doc(tid: str, doc_id: str, principal: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    d = _knowledge_dir(tid)
    meta = _knowledge_meta(tid)
    if doc_id not in meta["docs"]:
        return 404, {"error": "文档不存在"}
    (d / "docs" / f"{doc_id}.txt").unlink(missing_ok=True)
    (d / "chunks" / f"{doc_id}.json").unlink(missing_ok=True)
    (d / "vectors" / f"{doc_id}.json").unlink(missing_ok=True)
    info = meta["docs"].pop(doc_id)
    _save_knowledge_meta(tid, meta)
    _save_knowledge_index(tid, _rebuild_knowledge_index(tid, meta))
    _save_chunk_index(tid, _rebuild_chunk_index(tid, meta))
    _new_audit(principal).log({
        "actor": principal.get("role", "admin"),
        "event": "knowledge.delete",
        "tenant_id": tid,
        "doc_id": doc_id,
        "filename": info.get("filename", ""),
        "allowed": True,
    })
    return 200, {"ok": True, "doc_id": doc_id}


def _search_knowledge(tid: str, query: str) -> list[dict[str, Any]]:
    query = str(query or "").strip()
    if not query:
        return []
    meta = _knowledge_meta(tid)
    index = _load_knowledge_index(tid)
    q_tokens = _tokenize(query)
    hits: dict[str, int] = {}
    for tok in set(q_tokens):
        for doc_id in index.get(tok, []):
            hits[doc_id] = hits.get(doc_id, 0) + 1
    if not hits:
        # 宽松兜底：元数据（文件名/标签）子串匹配
        for doc_id, info in meta["docs"].items():
            hay = (info.get("filename", "") + " " + " ".join(info.get("tags", []))).lower()
            if query.lower() in hay:
                hits[doc_id] = 1
    ranked = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))[:KNOWLEDGE_SEARCH_LIMIT]
    results = []
    for doc_id, score in ranked:
        info = meta["docs"].get(doc_id, {})
        text = (_knowledge_dir(tid) / "docs" / f"{doc_id}.txt").read_text(encoding="utf-8", errors="replace")
        snippet = ""
        pos = text.lower().find(query.lower())
        if pos < 0 and q_tokens:
            pos = text.lower().find(q_tokens[0])
        if pos >= 0:
            start = max(0, pos - 60)
            snippet = ("…" if start > 0 else "") + text[start:pos + 120].replace("\n", " ") + ("…" if start + 180 < len(text) else "")
        results.append({
            "doc_id": doc_id,
            "filename": info.get("filename", ""),
            "tags": info.get("tags", []),
            "size": info.get("size", 0),
            "created_at": info.get("created_at", ""),
            "score": score,
            "snippet": snippet,
        })
    return results


def _load_knowledge_index(tid: str) -> dict[str, list[str]]:
    try:
        return json.loads((_knowledge_dir(tid) / "index.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------- 知识库 RAG（阶段2：chunk 级检索 + 生成问答；向量精排可选） ----------
# 上传时把文档切成 chunk 并建块级倒排（BM25 简化版，确定性、零依赖）；
# 设置 ANENGOS_EMBEDDING_MODEL 后启用语义精排（embedding API 失败自动降级 BM25）。
CHUNK_SIZE = 400          # 每块约 400 字符
CHUNK_OVERLAP = 60        # 相邻块重叠，避免切断语义
CHUNK_BM25_TOP = 30       # 粗筛数量
RAG_TOP = 6               # 精排后送入生成器的块数
_EMBED_CACHE: dict[str, list[float]] = {}   # text -> vec（LRU 上限 2000）
_EMBED_CACHE_MAX = 2000


def _chunk_text(text: str) -> list[str]:
    """按换行/句号优先切成 ~CHUNK_SIZE 的块，带 overlap。"""
    text = text.replace("\r\n", "\n")
    units = [u for u in re.split(r"(?<=[。！？!?\n])", text) if u.strip()]
    chunks: list[str] = []
    cur = ""
    for u in units:
        if len(cur) + len(u) > CHUNK_SIZE and cur:
            chunks.append(cur)
            cur = cur[-CHUNK_OVERLAP:] + u if CHUNK_OVERLAP else u
        else:
            cur += u
    if cur.strip():
        chunks.append(cur)
    if not chunks:
        chunks = [text]
    return chunks


def _rebuild_chunk_index(tid: str, meta: dict[str, Any]) -> dict[str, dict[str, int]]:
    """块级倒排：{token: {chunk_key: tf}}。"""
    index: dict[str, dict[str, int]] = {}
    for doc_id in meta["docs"]:
        chunks = _load_doc_chunks(tid, doc_id)
        for idx, text in enumerate(chunks):
            ck = f"{doc_id}:{idx}"
            for tok in set(_tokenize(text)):
                tf = index.setdefault(tok, {})
                tf[ck] = tf.get(ck, 0) + 1
    return index


def _save_chunk_index(tid: str, index: dict[str, dict[str, int]]) -> None:
    (_knowledge_dir(tid) / "chunk_index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")


def _load_chunk_index(tid: str) -> dict[str, dict[str, int]]:
    try:
        return json.loads((_knowledge_dir(tid) / "chunk_index.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_doc_chunks(tid: str, doc_id: str, chunks: list[str]) -> None:
    d = _knowledge_dir(tid) / "chunks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{doc_id}.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")


def _load_doc_chunks(tid: str, doc_id: str) -> list[str]:
    try:
        return json.loads((_knowledge_dir(tid) / "chunks" / f"{doc_id}.json").read_text(encoding="utf-8"))
    except Exception:
        return [_knowledge_doc_text(tid, doc_id)]


def _knowledge_doc_text(tid: str, doc_id: str) -> str:
    try:
        return (_knowledge_dir(tid) / "docs" / f"{doc_id}.txt").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _search_chunks(tid: str, query: str, k: int = CHUNK_BM25_TOP) -> list[dict[str, Any]]:
    """块级 BM25 简化检索：idf=log(1+N/(1+df))，score=Σtf·idf/√len。"""
    q_tokens = [t for t in _tokenize(query) if t]
    if not q_tokens:
        return []
    index = _load_chunk_index(tid)
    if not index:
        return []
    N = sum(len(v) for v in index.values())  # 近似 chunk 总数（重复计，可接受）
    scores: dict[str, float] = {}
    lens: dict[str, int] = {}
    for tok in set(q_tokens):
        post = index.get(tok, {})
        df = len(post)
        idf = 1.0 + __import__("math").log((N + 1) / (df + 1)) if df else 0.0
        for ck, tf in post.items():
            scores[ck] = scores.get(ck, 0.0) + tf * idf
            if ck not in lens:
                doc_id, idx = ck.rsplit(":", 1)
                lens[ck] = max(1, len(_load_doc_chunks(tid, doc_id)[int(idx)]))
    ranked = sorted(scores.items(), key=lambda kv: -kv[1] / lens.get(kv[0], 1))[:k]
    out = []
    for ck, score in ranked:
        doc_id, idx = ck.rsplit(":", 1)
        idx = int(idx)
        chunks = _load_doc_chunks(tid, doc_id)
        text = chunks[idx] if idx < len(chunks) else ""
        out.append({"doc_id": doc_id, "chunk_idx": idx, "text": text, "score": round(score, 3)})
    return out


def _embed_batch(texts: list[str]) -> list[list[float]] | None:
    """调 embedding API（OpenAI 兼容 /embeddings）。供应商可独立配置：
    ANENGOS_EMBEDDING_MODEL     必填（如 BAAI/bge-m3、text-embedding-3-small）
    ANENGOS_EMBEDDING_BASE_URL  默认取 ANENGOS_BASE_URL（硅基流动 https://api.siliconflow.cn/v1）
    ANENGOS_EMBEDDING_API_KEY   默认取 ANENGOS_API_KEY
    分批（≤32/次）并缓存；任何失败返回 None（调用方降级 BM25，不阻塞）。"""
    model = os.environ.get("ANENGOS_EMBEDDING_MODEL", "").strip()
    api_key = (os.environ.get("ANENGOS_EMBEDDING_API_KEY") or os.environ.get("ANENGOS_API_KEY", "")).strip()
    if not model or not api_key:
        return None
    base = (os.environ.get("ANENGOS_EMBEDDING_BASE_URL") or os.environ.get("ANENGOS_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    out: list[list[float]] = []
    missing: list[int] = []
    for i, t in enumerate(texts):
        if t in _EMBED_CACHE:
            out.append(_EMBED_CACHE[t])
        else:
            out.append([])
            missing.append(i)
    if missing:
        try:
            for start in range(0, len(missing), 32):
                batch_idx = missing[start:start + 32]
                batch = [texts[i] for i in batch_idx]
                req = urllib.request.Request(
                    f"{base}/embeddings",
                    data=json.dumps({"model": model, "input": batch}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                for item in data.get("data", []):
                    vec = item.get("embedding") or []
                    if vec:
                        _EMBED_CACHE[texts[item["index"]]] = vec if len(_EMBED_CACHE) < _EMBED_CACHE_MAX else vec
                for j, i in enumerate(batch_idx):
                    if i < len(out) and not out[i]:
                        out[i] = data["data"][j].get("embedding") or []
        except Exception:
            return None
    return out


def _save_doc_vectors(tid: str, doc_id: str, vectors: list[list[float]]) -> None:
    d = _knowledge_dir(tid) / "vectors"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{doc_id}.json").write_text(json.dumps(vectors), encoding="utf-8")


def _load_doc_vectors(tid: str, doc_id: str) -> list[list[float]]:
    try:
        return json.loads((_knowledge_dir(tid) / "vectors" / f"{doc_id}.json").read_text(encoding="utf-8"))
    except Exception:
        return []


def _vectorize_doc(tid: str, doc_id: str, chunks: list[str]) -> bool:
    """上传时预计算全部 chunk 向量；成功 True，失败 False（ask 自动降级 BM25）。"""
    if not os.environ.get("ANENGOS_EMBEDDING_MODEL", "").strip():
        return False
    vecs = _embed_batch(chunks)
    if not vecs or any(not v for v in vecs):
        return False
    _save_doc_vectors(tid, doc_id, vecs)
    return True


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(y * y for y in b)) or 1e-9
    return dot / (na * nb)


def _search_vectors(tid: str, query_vec: list[float], k: int = RAG_TOP) -> list[dict[str, Any]]:
    """语义检索：query 向量对租户全部 chunk 余弦排序。返回与 _search_chunks 同构。"""
    meta = _knowledge_meta(tid)
    scored: list[dict[str, Any]] = []
    for doc_id in meta["docs"]:
        vecs = _load_doc_vectors(tid, doc_id)
        if not vecs:
            continue
        chunks = _load_doc_chunks(tid, doc_id)
        for idx, v in enumerate(vecs):
            if idx >= len(chunks):
                break
            scored.append({"doc_id": doc_id, "chunk_idx": idx, "text": chunks[idx], "score": round(_cosine(query_vec, v), 4)})
    scored.sort(key=lambda h: -h["score"])
    return scored[:k]


def _ask_knowledge(tid: str, query: str, principal: dict[str, Any]) -> dict[str, Any]:
    """检索增强问答：语义向量优先 -> BM25 兜底 -> DeepSeek 生成，带来源。"""
    query = str(query or "").strip()
    if not query:
        return {"answer": None, "sources": [], "mode": "none", "error": "问题不能为空"}
    mode = "bm25"
    hits: list[dict[str, Any]] = []
    if os.environ.get("ANENGOS_EMBEDDING_MODEL", "").strip():
        qv = _embed_batch([query])
        if qv and qv[0]:
            hits = _search_vectors(tid, qv[0], RAG_TOP)
            if hits:
                mode = "vector"
    if not hits:
        hits = _search_chunks(tid, query, RAG_TOP)
    if not hits:
        _new_audit(principal).log({"actor": principal.get("role", "admin"), "event": "knowledge.ask",
                                   "tenant_id": tid, "query": query, "hits": 0, "mode": mode, "allowed": True})
        return {"answer": None, "sources": [], "mode": mode, "error": "知识库中无相关内容"}
    meta = _knowledge_meta(tid)
    sources = []
    parts = []
    for i, h in enumerate(hits, 1):
        info = meta["docs"].get(h["doc_id"], {})
        fname = info.get("filename", h["doc_id"])
        sources.append({"filename": fname, "chunk_idx": h["chunk_idx"], "snippet": h["text"][:180], "score": h["score"]})
        parts.append(f"[{i}] 来源《{fname}》片段{h['chunk_idx']}：\n{h['text']}")
    context = "\n\n".join(parts)
    answer, error = None, None
    try:
        llm = OpenAICompatLLM()
        resp = llm([
            {"role": "system", "content": "你是 ANENGOS 企业知识库问答助手。只能依据用户提供的资料回答，资料不足以回答时明确说明『资料中未提及』，不编造。回答用中文，简洁准确。"},
            {"role": "user", "content": f"以下是检索到的客户资料：\n\n{context}\n\n问题：{query}"},
        ], [])
        answer = (resp.get("text") or "").strip() or None
    except Exception as e:  # noqa: BLE001
        error = str(e)[:200]
    _new_audit(principal).log({"actor": principal.get("role", "admin"), "event": "knowledge.ask",
                               "tenant_id": tid, "query": query, "hits": len(hits), "mode": mode,
                               "answered": bool(answer), "allowed": True})
    return {"answer": answer, "sources": sources, "mode": mode, "error": error}


# ---------- 租户知识库 MCP（Streamable HTTP）：让客户的企业 agent 直接问自己的资料 ----------
# 端点 POST /mcp，Authorization: Bearer <租户token>；JSON-RPC 2.0：
#   initialize / notifications/initialized / ping / tools/list / tools/call
# 工具：knowledge_list / knowledge_search / knowledge_ask / knowledge_upload
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_SERVER_NAME = "anengos"
MCP_SERVER_VERSION = "0.3.0"
MCP_TOOLS: list[dict[str, Any]] = [
    {"name": "knowledge_list",
     "description": "列出当前租户知识库中的全部资料（文件名、大小、上传时间）",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "knowledge_search",
     "description": "在租户知识库中检索关键词，返回命中文档与上下文片段",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "检索关键词"}},
                     "required": ["query"]}},
    {"name": "knowledge_ask",
     "description": "基于租户知识库做 AI 问答（RAG）：检索最相关资料并用大模型回答，返回答案与来源。消耗 1 次租户任务配额",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string", "description": "要问的问题"}},
                     "required": ["query"]}},
    {"name": "knowledge_upload",
     "description": "上传一份文本资料（txt/md/csv 纯文本）到租户知识库，单份不超过 2MB，消耗租户配额",
     "inputSchema": {"type": "object",
                     "properties": {"filename": {"type": "string", "description": "文件名（含扩展名）"},
                                    "content": {"type": "string", "description": "文档纯文本内容"}},
                     "required": ["filename", "content"]}},
]


def _mcp_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _mcp_error(code: int, message: str, req_id: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
    if req_id is not None:
        d["id"] = req_id
    return d


def _mcp_text(req_id: Any, text: str, is_error: bool = False) -> dict[str, Any]:
    return _mcp_result(req_id, {"content": [{"type": "text", "text": text}], "isError": is_error})


def _mcp_tools_call(req_id: Any, params: dict[str, Any], tid: str) -> dict[str, Any]:
    name = str(params.get("name", ""))
    args: dict[str, Any] = params.get("arguments") or {}
    try:
        if name == "knowledge_list":
            meta = _knowledge_meta(tid)
            docs = sorted(meta["docs"].values(), key=lambda x: x.get("created_at", ""), reverse=True)
            if not docs:
                text = "当前租户知识库暂无资料，可用 knowledge_upload 上传。"
            else:
                total = sum(d.get("size", 0) for d in docs)
                lines = [f"当前租户知识库共 {len(docs)} 份资料（{total // 1024} KB）："]
                for d in docs:
                    lines.append(f"- {d.get('filename')}（{d.get('size', 0)} 字节，上传于 {str(d.get('created_at', ''))[:10]}）")
                text = "\n".join(lines)
            return _mcp_text(req_id, text)
        if name == "knowledge_search":
            query = str(args.get("query", "")).strip()
            if not query:
                return _mcp_text(req_id, "缺少 query 参数", is_error=True)
            quota_err = _charge_credits({"role": "tenant", "tenant_id": tid}, "knowledge_search")
            if quota_err:
                return _mcp_text(req_id, f"配额不足：{quota_err}", is_error=True)
            results = _search_knowledge(tid, query)
            if not results:
                text = f"未检索到与「{query}」相关的资料。"
            else:
                lines = [f"检索「{query}」命中 {len(results)} 份文档："]
                for i, r in enumerate(results, 1):
                    lines.append(f"{i}. 《{r['filename']}》（score={r['score']}）")
                    if r.get("snippet"):
                        lines.append(f"   片段：{r['snippet']}")
                text = "\n".join(lines)
            return _mcp_text(req_id, text)
        if name == "knowledge_ask":
            query = str(args.get("query", "")).strip()
            if not query:
                return _mcp_text(req_id, "缺少 query 参数", is_error=True)
            principal = {"role": "tenant", "tenant_id": tid}
            quota_err = _charge_credits(principal, "knowledge_ask")
            if quota_err:
                return _mcp_text(req_id, f"配额不足：{quota_err}", is_error=True)
            body = _ask_knowledge(tid, query, principal)
            _bump_usage(principal)  # AI 问答计 1 次任务用量
            if body.get("answer"):
                src = "、".join(f"《{s['filename']}》" for s in body.get("sources", [])) or "无"
                text = f"{body['answer']}\n\n[来源] {src}\n[检索模式] {body.get('mode', 'bm25')}"
            elif body.get("sources"):
                src = "、".join(f"《{s['filename']}》" for s in body.get("sources", []))
                text = f"{body.get('error') or '模型暂不可用'}\n[检索到资料但模型不可用，来源] {src}"
                return _mcp_text(req_id, text, is_error=True)
            else:
                text = body.get("error") or "知识库中无相关内容。"
                return _mcp_text(req_id, text, is_error=True)
            return _mcp_text(req_id, text)
        if name == "knowledge_upload":
            filename = str(args.get("filename", "")).strip()
            content = str(args.get("content", ""))
            if not filename or not content.strip():
                return _mcp_text(req_id, "filename 与 content 均不能为空", is_error=True)
            principal = {"role": "tenant", "tenant_id": tid}
            quota_err = _charge_credits(principal, "knowledge_upload")
            if quota_err:
                return _mcp_text(req_id, f"配额不足：{quota_err}", is_error=True)
            code, body = _upload_knowledge(tid, filename,
                                           base64.b64encode(content.encode("utf-8")).decode(), [], principal)
            if code in (200, 201):
                return _mcp_text(req_id,
                                 f"上传成功：{filename}（doc_id={body.get('doc_id')}，{body.get('size')} 字节）")
            return _mcp_text(req_id, f"上传失败：{body.get('error', '未知错误')}", is_error=True)
        return _mcp_text(req_id, f"Unknown tool: {name}", is_error=True)
    except Exception as exc:  # noqa: BLE001
        return _mcp_text(req_id, f"工具执行异常：{exc}", is_error=True)


def _mcp_handle(payload: dict[str, Any], tid: str) -> dict[str, Any] | None:
    """执行一次 MCP JSON-RPC 调用（tid 已通过租户 token 鉴权）。返回 None 表示通知（无需响应）。"""
    method = str(payload.get("method", ""))
    req_id = payload.get("id")
    if method == "initialize":
        return _mcp_result(req_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
        })
    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return _mcp_result(req_id, {})
    if method == "tools/list":
        return _mcp_result(req_id, {"tools": MCP_TOOLS})
    if method == "tools/call":
        return _mcp_tools_call(req_id, payload.get("params") or {}, tid)
    if method == "resources/list":
        return _mcp_result(req_id, {"resources": []})
    return _mcp_error(-32601, f"Method not found: {method}", req_id)
# 租户用量达到阈值（默认 80%）时推送告警；100% 档始终触发，同档位月度内只发一次。
_WEBHOOK: dict[str, Any] = {"url": None, "enabled": False, "threshold": 0.8}
_WEBHOOK_FIRED: dict[str, set[str]] = {}
_WEBHOOK_LOG: list[dict[str, Any]] = []


def _maybe_webhook(principal: dict[str, Any]) -> None:
    if principal["role"] != "tenant":
        return
    t = _tenant_record(principal["tenant_id"])
    if t is None or not _WEBHOOK.get("enabled") or not _WEBHOOK.get("url"):
        return
    used = t["usage"].get("tasks", 0)
    limit = t["quota"].get("tasks_per_month", 0)
    # 信用额度池使用率（精细计量优先；告警阈值取两者更高者）
    cstate = _tenant_credit_state(t)
    ratios: list[float] = []
    if limit > 0:
        ratios.append(used / limit)
    if cstate["monthly_limit"] > 0:
        ratios.append(cstate["monthly_used"] / cstate["monthly_limit"])
    if not ratios:
        return
    ratio = max(ratios)
    if ratio >= 1.0:
        level = "100"
    elif ratio >= float(_WEBHOOK.get("threshold", 0.8)):
        level = "80"
    else:
        return
    fired = _WEBHOOK_FIRED.setdefault(principal["tenant_id"], set())
    if level in fired:
        return
    import datetime

    payload: dict[str, Any] = {
        "event": "quota_alert",
        "tenant_id": principal["tenant_id"],
        "name": t.get("name", ""),
        "tasks_used": used,
        "tasks_quota": limit,
        "credits_used": cstate["monthly_used"],
        "credits_quota": cstate["monthly_limit"],
        "extra_credits": cstate["extra_remaining"],
        "ratio": round(ratio, 2),
        "level": level,
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        req = urllib.request.Request(
            _WEBHOOK["url"],
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=8).read()
        payload["status"] = "sent"
    except Exception as exc:  # 告警失败不影响业务
        payload["status"] = f"failed: {exc}"
    fired.add(level)
    _WEBHOOK_LOG.insert(0, payload)
    del _WEBHOOK_LOG[20:]


def _signup_rate_limited(ip: str) -> bool:
    """自助开通限流：同一 IP 当天超过 _SIGNUP_LIMIT 次则拒绝。"""
    import datetime

    day = datetime.date.today().isoformat()
    hits = _SIGNUP_HITS.setdefault(ip, [])
    hits = [h for h in hits if h == day]
    _SIGNUP_HITS[ip] = hits
    if len(hits) >= _SIGNUP_LIMIT:
        return True
    hits.append(day)
    return False


def _get_reviewer() -> Reviewer | None:
    """惰性创建监督智能体（复用主模型配置）；模型不可用时返回 None（跳过互审）。"""
    global _REVIEWER
    if _REVIEWER is None:
        try:
            _REVIEWER = Reviewer(OpenAICompatLLM(), _new_audit())
        except ValueError:
            return None
    return _REVIEWER


def _build_tools(ws: Path, actor: str) -> ToolRegistry:
    """统一构建工具集：内核默认工具 + 全部外部智能体适配器。"""
    reg = build_default_tools(ws)
    for adapter in _EXTERNAL_AGENTS.values():
        register_adapter(adapter, str(ws), reg)
    return reg


# ---------- 令牌与租户 ----------

def _admin_token() -> str:
    return os.environ.get("ANENGOS_API_TOKEN", "")


def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def _load_tenants() -> None:
    global _TENANTS
    if TENANTS_FILE.exists():
        try:
            _TENANTS = json.loads(TENANTS_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            _TENANTS = {}


def _load_tasks() -> None:
    """启动时恢复任务队列（Failover 断点续跑）。

    进程重启后：
    - queued（从未开始执行，无副作用）→ 自动重新入队执行；
    - running / retrying / waiting_approval（可能已产生外部副作用或挂起审批，
      内存中的 Session/Agent 已丢失）→ 标记 interrupted，交由管理员 retry 重放，
      不自动重放，避免重复外部调用。
    """
    global _TASKS
    if not TASKS_FILE.exists():
        return
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    interrupted: list[tuple[str, dict[str, Any]]] = []
    for tid, rec in data.items():
        if not isinstance(rec, dict):
            continue
        rec.pop("_session", None)
        rec.pop("_agent", None)
        status = rec.get("status")
        if status in ("running", "retrying", "waiting_approval"):
            rec["status"] = "interrupted"
            rec["error"] = rec.get("error") or "进程重启中断，请管理员重放"
            interrupted.append((tid, rec))
        elif status == "queued":
            rec["status"] = "queued"  # 从未执行：自动续跑
        else:
            continue
        _TASKS[tid] = rec
    if not interrupted:
        return
    for tid, rec in interrupted:  # 内存快照即可，线程只读 id
        pass
    import threading

    def _bootstrap() -> None:
        # 自动续跑 queued；interrupted 仅标记（等管理员 retry）
        for tid, rec in list(_TASKS.items()):
            if rec.get("status") == "queued":
                threading.Thread(
                    target=_run_task_with_failover,
                    args=(tid, rec),
                    daemon=True,
                ).start()

    threading.Thread(target=_bootstrap, daemon=True).start()


def _save_tasks() -> None:
    """任务队列落盘；隐藏内部 Session/Agent 对象（不可 JSON 序列化）。"""
    try:
        public = {
            tid: {k: v for k, v in rec.items() if not k.startswith("_")}
            for tid, rec in _TASKS.items()
        }
        TASKS_FILE.write_text(
            json.dumps(public, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        pass  # 落盘失败不阻塞业务


def _save_tenants() -> None:
    TENANTS_FILE.write_text(
        json.dumps(_TENANTS, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _tenant_workspace(tid: str) -> Path:
    return WORKSPACE / "tenants" / tid


def _auth_principal(headers) -> tuple[bool, str, dict[str, Any] | None]:
    """校验令牌并解析身份：{"role":"admin"} 或 {"role":"tenant","tenant_id":...}。

    鉴权顺序：Authorization: Bearer / X-API-Token（API 调用）→ Cookie 会话（Web 登录）。
    """
    admin_tok = _admin_token()
    if not admin_tok:
        return False, "服务未配置 ANENGOS_API_TOKEN（管理员令牌），接口已禁用；设置令牌后重启", None
    provided = headers.get("Authorization", "")
    if provided.startswith("Bearer "):
        provided = provided[7:]
    else:
        provided = headers.get("X-API-Token", "")
    if provided:
        if hmac.compare_digest(provided, admin_tok):
            return True, "", {"role": "admin"}
        h = _hash_token(provided)
        for tid, t in _TENANTS.items():
            if t.get("status") == "active" and hmac.compare_digest(t.get("token_hash", ""), h):
                return True, "", {"role": "tenant", "tenant_id": tid}
        return False, "访问令牌错误或租户已停用", None
    # Web 会话兜底
    sid = _cookie_value(headers.get("Cookie", ""), "anengos_session")
    if sid:
        principal = _session_principal(sid)
        if principal:
            return True, "", principal
        return False, "会话已过期，请重新登录", None
    return False, "缺少访问令牌：请带 Authorization: Bearer <token>，或先登录 Web 控制台", None


def _queue_for(principal: dict[str, Any]) -> ApprovalQueue:
    if principal["role"] == "tenant":
        tid = principal["tenant_id"]
        return _TENANT_QUEUES.setdefault(tid, ApprovalQueue())
    return _shared_context()[0]


def _workspace_for(principal: dict[str, Any]) -> Path:
    if principal["role"] == "tenant":
        return _tenant_workspace(principal["tenant_id"])
    return WORKSPACE


def _new_audit(principal: dict[str, Any] | None = None) -> AuditLog:
    if principal is not None and principal["role"] == "tenant":
        p = AUDIT_FILE.parent / "tenants" / principal["tenant_id"]
        p.mkdir(parents=True, exist_ok=True)
        return AuditLog(p / "audit.jsonl")
    return AuditLog(AUDIT_FILE)


def _shared_context() -> tuple[ApprovalQueue, ToolRegistry]:
    """管理员审批队列与工具注册表全局单例。"""
    global _APPROVALS, _TOOLS
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    if _APPROVALS is None:
        _APPROVALS = ApprovalQueue()
    if _TOOLS is None:
        _TOOLS = _build_tools(WORKSPACE, ACTOR)
    return _APPROVALS, _TOOLS


def _apply_approval(item: ApprovalItem, principal: dict[str, Any]) -> str:
    """批准后真实执行副作用动作，并留审计；外部智能体产出自动交由监督智能体互审。"""
    ws = _workspace_for(principal)
    ws.mkdir(parents=True, exist_ok=True)
    tools = _shared_context()[1] if principal["role"] == "admin" else _build_tools(ws, item.actor)
    result = tools.run(item.tool, item.args)

    entry: dict[str, Any] = {
        "actor": item.actor,
        "event": "approval_applied",
        "tool": item.tool,
        "args": item.args,
        "allowed": True,
        "decision": f"已批准并真实执行: {result}",
    }
    # 外部智能体（*.submit）产出 -> 互审
    if item.tool.endswith(".submit"):
        reviewer = _get_reviewer()
        if reviewer is not None:
            try:
                verdict = reviewer.review(item.actor, item.tool, item.args, result, str(ws))
                entry["review"] = verdict
                result = f"{result}\n\n[互审] 监督智能体：{verdict.get('verdict')}（{verdict.get('score')} 分）{verdict.get('reason', '')}"
            except Exception as e:  # noqa: BLE001
                entry["review_error"] = str(e)[:200]
    _new_audit(principal).log(entry)
    return result


def _reject_approval(item: ApprovalItem, principal: dict[str, Any]) -> None:
    _new_audit(principal).log(
        {
            "actor": item.actor,
            "event": "approval_rejected",
            "tool": item.tool,
            "args": item.args,
            "allowed": False,
            "decision": "管理员拒绝",
        }
    )


def build_agent(principal: dict[str, Any] | None = None) -> tuple[AgentOS | None, ApprovalQueue, str]:
    """按身份组装受治理的 agent；未配置 API key 时返回错误信息。"""
    principal = principal or {"role": "admin"}
    queue = _queue_for(principal)
    ws = _workspace_for(principal)
    ws.mkdir(parents=True, exist_ok=True)
    actor = ACTOR if principal["role"] == "admin" else f"tenant-{principal['tenant_id']}"

    registry = CapabilityRegistry()
    registry.introduce(actor, "file.read", "workspace", side_effect=False)
    registry.introduce(actor, "file.write", "workspace", side_effect=True)
    registry.introduce(actor, "file.list", "workspace", side_effect=False)
    # 多智能体总装线：外部智能体（提交任务=有副作用，走审批）
    for name in _EXTERNAL_AGENTS:
        registry.introduce(actor, f"{name}.submit", name, side_effect=True)
        registry.introduce(actor, f"{name}.health", name, side_effect=False)

    tools = _shared_context()[1] if principal["role"] == "admin" else _build_tools(ws, actor)
    audit = _new_audit(principal)
    try:
        llm = OpenAICompatLLM()
    except ValueError as e:
        return None, queue, str(e)
    os_ = AgentOS(actor, tools, Gatekeeper(registry, "api"), queue, audit, llm)
    return os_, queue, ""


def _llms_txt() -> str:
    """AI 可读的产品说明（llmstxt.org 规范）：让 LLM/agent 发现并理解本平台能力。

    分发闭环的入口：llms.txt（发现）→ 自助开通（/admin）→ 一行安装（/install）
    → MCP 验证（/mcp）→ 信用池计费（/admin/api/usage）。
    """
    host = (os.environ.get("ANENGOS_PUBLIC_URL") or f"http://localhost:{os.environ.get('ANENGOS_PORT', '8080')}").rstrip("/")
    tools = "、".join(t["name"] for t in MCP_TOOLS)
    return "\n".join([
        "# ANENGOS",
        "",
        f"> ANENGOS 是企业私有知识库的 AI 问答平台：把公司资料上传到云端知识库，",
        f"> 通过 MCP / API / 客户站三种方式，让 Claude、Cursor 等 AI 直接回答",
        f"> 你公司自己的问题。租户数据隔离、信用额度池计费、任务失败自动重放。",
        "",
        "## 入口",
        f"- {host}/: 产品主页与管理台（自助开通租户）",
        f"- {host}/llms.txt: 本文件（AI 可读的产品说明）",
        f"- {host}/install: 一行安装（生成 Claude/Cursor 的 MCP 配置，需登录）",
        "",
        "## MCP 端点（让 AI 直接问你的知识库）",
        f"- {host}/mcp: MCP streamable HTTP 端点，请求头 Authorization: Bearer <租户token>，JSON-RPC 2.0，协议版本 2025-06-18",
        f"- 工具：{tools}",
        "  - knowledge_list：列出当前租户知识库全部资料（免费）",
        "  - knowledge_search：关键词检索，返回命中文档与上下文（1 信用分/次）",
        "  - knowledge_ask：RAG 问答，检索 + 大模型回答 + 来源（5 信用分/次）",
        "  - knowledge_upload：上传资料（2 信用分/次）",
        "",
        "## 接入步骤（5 分钟）",
        "1. 管理台开通租户，获得租户 token（管理员在 /admin 创建后复制 token）",
        f"2. 访问 {host}/install 复制 MCP 配置（用租户 token 登录）",
        "3. 粘贴进 Claude Code（~/.config/claude/mcp.json）或 Cursor 的 MCP 配置",
        "4. 向 AI 提问『查询我的知识库』验证；用量与余额见管理台",
        "",
        "## 技术",
        "Python 标准库 HTTP 服务，无框架依赖；租户隔离 + 审批式智能体 + 异步任务队列（断点续跑/失败重放）；",
        "计费口径：试用免费 / 团队版 ¥299/月 / 企业版 ¥1,500/月。",
        "",
    ])


def _install_payload(principal: dict[str, Any], provided_token: str = "") -> dict[str, Any]:
    """一行安装：为当前身份生成粘贴即用的 MCP 配置（Claude Code / Cursor）。

    token 采用"回显"策略：服务端只存哈希，配置里的 token 即调用者本次
    提交的明文（用什么 token 登录，配置里就用什么），不额外存储明文。
    """
    host = (os.environ.get("ANENGOS_PUBLIC_URL") or f"http://localhost:{os.environ.get('ANENGOS_PORT', '8080')}").rstrip("/")
    mcp_url = f"{host}/mcp"
    tools = [t["name"] for t in MCP_TOOLS]
    if principal["role"] == "admin":
        # 管理员 token 不能调 MCP（MCP 只认租户 token），给出指引
        return {
            "ok": True,
            "role": "admin",
            "mcp_url": mcp_url,
            "tools": tools,
            "note": "MCP 端点只接受租户 token。请用租户 token 访问 /install（Authorization: Bearer <租户token>）以生成现成配置。",
            "claude_config": None,
        }
    if not provided_token:
        return {
            "ok": True,
            "role": "tenant",
            "tenant_id": principal["tenant_id"],
            "mcp_url": mcp_url,
            "tools": tools,
            "note": "请用租户 token 访问 /install（Authorization: Bearer <租户token>）以生成现成配置。",
            "claude_config": None,
        }
    config = {
        "type": "http",
        "url": mcp_url,
        "headers": {"Authorization": f"Bearer {provided_token}"},
        "tools": tools,
    }
    return {
        "ok": True,
        "role": "tenant",
        "tenant_id": principal["tenant_id"],
        "mcp_url": mcp_url,
        "tools": tools,
        "claude_config": {
            "mcpServers": {"anengos": {"type": "http", "url": mcp_url,
                                       "headers": {"Authorization": f"Bearer {provided_token}"}}},
        },
        "cursor_config": {
            "mcpServers": {"anengos": {"type": "http", "url": mcp_url,
                                       "headers": {"Authorization": f"Bearer {provided_token}"}}},
        },
        "quick_check": (
            f"curl -s -X POST {mcp_url} -H 'Authorization: Bearer {provided_token}' "
            "-H 'Content-Type: application/json' "
            "-d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"anengos-cli\",\"version\":\"0.1.0\"}}}'"
        ),
        "usage_hint": "knowledge_search 1 分/次，knowledge_ask 5 分/次，knowledge_upload 2 分/次，余额见管理台",
    }


def _health_body() -> dict[str, Any]:
    agents = {}
    for name, adapter in _EXTERNAL_AGENTS.items():
        try:
            h = adapter.health() if hasattr(adapter, "health") else {}
            agents[name] = {"ready": h.get("ready", False), "mode": h.get("mode", "?")}
        except Exception:  # noqa: BLE001
            agents[name] = {"ready": False, "mode": "error"}
    return {
        "status": "ok",
        "service": "anengos",
        "has_api_key": bool(os.environ.get("ANENGOS_API_KEY", "")),
        "auth_required": bool(_admin_token()),
        "model": os.environ.get("ANENGOS_MODEL", "gpt-4o-mini"),
        "workspace": str(WORKSPACE),
        "tenants": len([t for t in _TENANTS.values() if t.get("status") == "active"]),
        "agents": agents,
    }


def _run_task(query: str, principal: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    quota_err = _quota_error(principal)  # 任务次数配额（旧口径）
    if quota_err:
        return 429, {"error": quota_err}
    credit_err = _charge_credits(principal, "task_run")  # 信用额度池（新口径）
    if credit_err:
        return 429, {"error": credit_err}
    agent, approvals, err = build_agent(principal)
    if agent is None:
        return 503, {"error": err}
    session = agent.run(query, max_steps=20)
    _bump_usage(principal)
    return 200, {
        "output": session.output,
        "blocked": session.blocked_reason,
        "pending_approvals": [
            {"id": a.request_id, "tool": a.tool} for a in approvals.pending()
        ],
    }


def _task_visible(rec: dict[str, Any], principal: dict[str, Any]) -> bool:
    """管理员可见全部任务；租户仅见自己的任务。"""
    if principal["role"] == "admin":
        return True
    return rec.get("role") == "tenant" and rec.get("tenant_id") == principal.get("tenant_id")


def _principal_of(rec: dict[str, Any]) -> dict[str, Any]:
    """从任务记录恢复 principal（用于断点续跑/重放；不重复计费，费用在提交时已扣）。"""
    if rec.get("role") == "admin":
        return {"role": "admin"}
    return {"role": "tenant", "tenant_id": rec.get("tenant_id")}


def _execute_task(rec: dict[str, Any], principal: dict[str, Any]) -> None:
    """单次执行任务；遇审批挂起保存现场，异常上抛给 Failover 重放层。"""
    agent, _, err = build_agent(principal)
    if agent is None:
        raise RuntimeError(err or "agent 构建失败")
    session = agent.run(rec["query"], max_steps=20)
    if session.paused:
        rec["status"] = "waiting_approval"
        rec["output"] = None
        rec["blocked"] = None
        rec["pending_approvals"] = [
            {"id": i["request_id"], "tool": i["tool"]} for i in session.pending_items
        ]
        rec["waiting_on"] = [i["request_id"] for i in session.pending_items]
        rec["_session"] = session
        rec["_agent"] = agent
        rec["finished_at"] = None
    else:
        rec["output"] = session.output
        rec["blocked"] = session.blocked_reason
        rec["pending_approvals"] = []
        rec["status"] = "done"
        rec["finished_at"] = _ts()


def _run_task_with_failover(tid: str, rec: dict[str, Any], max_attempts: int = 3) -> None:
    """Failover 缓冲重放（借鉴 AgentKey buffer-and-replay）：客户端只见完整结果或完整失败。

    自动重试：执行抛异常（provider 抖动/构建失败等）时指数退避重试（1s/2s/4s…封顶 30s），
    期间状态 retrying + next_retry_at 全程可见；超过 max_attempts 给出最终失败，不丢任务。
    """
    principal = _principal_of(rec)
    attempts = 0
    while True:
        attempts += 1  # 1 基：第 N 次执行
        rec["status"] = "running"
        rec["attempts"] = attempts
        _save_tasks()
        try:
            _execute_task(rec, principal)
            _save_tasks()
            return
        except Exception as e:  # noqa: BLE001
            rec["last_error"] = str(e)[:500]
            if attempts >= max_attempts:
                rec["status"] = "error"
                rec["error"] = rec["last_error"]
                rec["finished_at"] = _ts()
                _save_tasks()
                return
            backoff = min(2 ** attempts, 30)
            import datetime

            rec["status"] = "retrying"
            rec["error"] = None
            rec["next_retry_at"] = (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(seconds=backoff)
            ).isoformat()
            _save_tasks()
            time.sleep(backoff)


def _submit_async(query: str, principal: dict[str, Any]) -> str:
    """提交异步任务：立即返回 task_id，后台线程执行；遇待审批挂起，审批后自动续跑。

    Failover：执行失败自动退避重试（max_attempts 次），进程重启后断点续跑/重放。
    """
    import threading

    tid = secrets.token_hex(6)
    rec: dict[str, Any] = {
        "id": tid,
        "status": "queued",  # queued -> running -> retrying* -> done|error|waiting_approval
        "created_at": _ts(),
        "finished_at": None,
        "role": principal["role"],
        "tenant_id": principal.get("tenant_id"),
        "query": query,
        "output": None,
        "blocked": None,
        "pending_approvals": [],
        "error": None,
        "attempts": 0,
        "max_attempts": 3,
        "last_error": None,
        "next_retry_at": None,
    }
    with _TASKS_LOCK:
        _TASKS[tid] = rec
    _save_tasks()
    threading.Thread(target=_run_task_with_failover, args=(tid, rec), daemon=True).start()
    _bump_usage(principal)  # 按提交次数计费（含挂起任务）
    return tid


def _maybe_resume(principal: dict[str, Any], req_id: str, result_text: str) -> None:
    """审批（批准/拒绝）后：若某挂起任务正等待该审批项，则自动续跑。"""
    import threading

    with _TASKS_LOCK:
        targets = [
            tid
            for tid, r in _TASKS.items()
            if r.get("status") == "waiting_approval" and req_id in r.get("waiting_on", [])
        ]
    if not targets:
        return

    def _resume_worker(tid: str) -> None:
        with _TASKS_LOCK:
            rec = _TASKS.get(tid)
        if rec is None:
            return
        session = rec.get("_session")
        agent = rec.get("_agent")
        if session is None or agent is None:
            return
        try:
            agent.resume(session, [{"id": req_id, "content": result_text}])
            if session.paused:
                rec["status"] = "waiting_approval"
                rec["output"] = None
                rec["pending_approvals"] = [
                    {"id": i["request_id"], "tool": i["tool"]} for i in session.pending_items
                ]
                rec["waiting_on"] = [i["request_id"] for i in session.pending_items]
                rec["finished_at"] = None
            else:
                rec["output"] = session.output
                rec["blocked"] = session.blocked_reason
                rec["pending_approvals"] = []
                rec["status"] = "done"
                rec["finished_at"] = _ts()
            _save_tasks()
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)[:500]
            rec["status"] = "error"
            rec["finished_at"] = _ts()
            _save_tasks()

    threading.Thread(target=_resume_worker, args=(targets[0],), daemon=True).start()


def _usage_csv(principal: dict[str, Any]) -> str:
    """账单 CSV：管理员导出全部租户（含历史月度归档），租户仅导出自己。

    每租户一行本月用量 + 每历史月一行账单；utf-8 BOM 保证 Excel 打开不乱码。
    """
    import csv
    import io

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["tenant_id", "name", "month", "tasks_used", "tasks_quota",
                "credits_used", "credits_quota", "extra_credits",
                "agents_quota", "storage_quota_mb", "status"])
    if principal["role"] == "admin":
        items = [(tid, t) for tid, t in _TENANTS.items()]
    else:
        tid = principal["tenant_id"]
        t = _TENANTS.get(tid)
        items = [(tid, t)] if t else []
    for tid, t in items:
        rec = _tenant_record(tid)
        c = _tenant_credit_state(t)
        w.writerow([tid, t.get("name", ""), rec["usage"]["month"],
                    rec["usage"]["tasks"], rec["quota"]["tasks_per_month"],
                    c["monthly_used"], c["monthly_limit"], c["extra_remaining"],
                    rec["quota"]["agents"], rec["quota"]["storage_mb"],
                    t.get("status", "active")])
        for m, u in sorted((t.get("billing") or {}).items()):
            w.writerow([tid, t.get("name", ""), m, u.get("tasks", 0),
                        rec["quota"]["tasks_per_month"], u.get("credits_used", 0),
                        c["monthly_limit"], c["extra_remaining"],
                        rec["quota"]["agents"], rec["quota"]["storage_mb"], "billed"])
    return "\ufeff" + out.getvalue()


def _all_audit_rows(principal: dict[str, Any]) -> list[dict[str, Any]]:
    """审计视图：管理员合并全部文件，租户只看自己。"""
    rows: list[dict[str, Any]] = []
    if principal["role"] == "admin":
        files = []
        if AUDIT_FILE.exists():
            files.append(AUDIT_FILE)
        tenant_dir = AUDIT_FILE.parent / "tenants"
        if tenant_dir.exists():
            files.extend(sorted(tenant_dir.glob("*/audit.jsonl")))
        for f in files:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
    else:
        p = AUDIT_FILE.parent / "tenants" / principal["tenant_id"] / "audit.jsonl"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows[:50]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # 静默访问日志
        pass

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _text(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _csv(self, code: int, body: str, filename: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auth(self) -> tuple[bool, dict[str, Any] | None]:
        ok, reason, principal = _auth_principal(self.headers)
        if not ok:
            code = 503 if not _admin_token() else 401
            self._json(code, {"error": reason})
            return False, None
        return True, principal

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "body 需为合法 JSON"})
            return None

    def _read_query(self) -> str | None:
        payload = self._read_json()
        if payload is None:
            return None
        query = str(payload.get("query", "")).strip()
        if not query:
            self._json(400, {"error": "query 不能为空"})
            return None
        return query

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/llms.txt":
            self._text(200, _llms_txt())
            return
        if path == "/install":
            ok, principal = self._auth()
            if not ok:
                return
            # 回显调用者自己提交的租户 token（服务端只存哈希，不落明文）：
            # "用什么 token 登录，配置里就用什么"
            raw = self.headers.get("Authorization", "")
            provided = raw[7:] if raw.startswith("Bearer ") else self.headers.get("X-API-Token", "")
            self._json(200, _install_payload(principal, provided))
            return
        if path == "/health":
            self._json(200, _health_body())
            return
        if path == "/":  # 产品主页（landing page）
            try:
                self._html(200, (BASE / "index.html").read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._html(200, "<h1>ANENGOS</h1><p>index.html 缺失</p>")
            return
        if path == "/admin":  # 管理台
            try:
                self._html(200, ADMIN_HTML.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._html(200, "<h1>ANENGOS</h1><p>admin.html 缺失</p>")
            return
        if path == "/signup":
            try:
                self._html(200, (BASE / "signup.html").read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._html(200, "<h1>ANENGOS</h1><p>signup.html 缺失</p>")
            return
        if path == "/login":  # Web 登录页
            try:
                self._html(200, (BASE / "login.html").read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._html(200, "<h1>ANENGOS</h1><p>login.html 缺失</p>")
            return
        # 客户站入口：未登录 -> 登录页；已登录租户 -> 问答 UI
        if path == "/client":
            p = _client_session_principal(self.headers)
            page = "client.html" if p else "client_login.html"
            try:
                self._html(200, (BASE / page).read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._html(200, f"<h1>ANENGOS</h1><p>{page} 缺失</p>")
            return
        # 客户站会话状态（登录态检测 + 信用额度余额）
        if path == "/api/client/session":
            p = _client_session_principal(self.headers)
            if not p:
                self._json(200, {"ok": False})
                return
            t = _TENANTS.get(p["tenant_id"])
            c = _tenant_credit_state(t) if t else {"monthly_limit": 0, "monthly_used": 0,
                                                   "monthly_remaining": 0, "extra_remaining": 0,
                                                   "total_remaining": 0}
            self._json(200, {"ok": True, "tenant_id": p["tenant_id"],
                             "credits": c, "rates": CREDIT_RATES})
            return
        if path == "/api/captcha":  # 公开：人机验证码
            cid, image = _new_captcha()
            self._json(200, {"captcha_id": cid, "image": image})
            return
        # 知识库列表：/admin/api/tenants/{tid}/knowledge
        m = re.match(r"^/admin/api/tenants/([0-9a-f]+)/knowledge$", path)
        if m:
            ok, principal = self._auth()
            if not ok:
                return
            tid = m.group(1)
            if principal["role"] == "tenant" and principal["tenant_id"] != tid:
                self._json(403, {"error": "只能访问自己的知识库"})
                return
            meta = _knowledge_meta(tid)
            docs = [dict(v) for v in sorted(meta["docs"].values(), key=lambda x: x.get("created_at", ""), reverse=True)]
            total = sum(d.get("size", 0) for d in docs)
            self._json(200, {"tenant_id": tid, "docs": docs, "doc_count": len(docs), "total_bytes": total})
            return
        # 租户知识库 API（客户自己管理/问答自己的资料，无需知道租户 id）
        if path == "/api/tenant/knowledge":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "tenant":
                self._json(403, {"error": "租户知识库 API 仅租户可用（管理员请用 /admin/api/tenants/{tid}/knowledge）"})
                return
            meta = _knowledge_meta(principal["tenant_id"])
            docs = [dict(v) for v in sorted(meta["docs"].values(), key=lambda x: x.get("created_at", ""), reverse=True)]
            total = sum(d.get("size", 0) for d in docs)
            self._json(200, {"tenant_id": principal["tenant_id"], "docs": docs, "doc_count": len(docs), "total_bytes": total})
            return
        if path == "/pay":  # 模拟收银台（mock 通道）
            try:
                self._html(200, (BASE / "pay.html").read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._html(200, "<h1>ANENGOS</h1><p>pay.html 缺失</p>")
            return
        if path == "/api/billing/plans":  # 公开：套餐 + 按量包 + 单价表
            self._json(200, {"provider": BILLING_PROVIDER, "plans": BILLING_PLANS,
                             "credit_packs": CREDIT_PACKS, "rates": CREDIT_RATES})
            return
        if path == "/api/billing/order":  # 公开：按 order_id 查订单状态
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            oid = (qs.get("id") or [""])[0]
            order = _ORDERS.get(oid)
            if order is None:
                self._json(404, {"error": "订单不存在"})
                return
            resp = {k: order[k] for k in ("order_id", "plan", "plan_name", "amount_cents",
                                          "currency", "provider", "tenant_id", "status",
                                          "created_at", "paid_at")}
            resp["qrcode"] = order.get("qrcode") or ""
            resp["code_url"] = order.get("code_url") or ""
            resp["mock"] = order.get("mock") or order.get("provider") == "mock"
            self._json(200, resp)
            return
        if path.startswith("/api/billing/order/") and path.endswith("/poll"):  # 公开：主动查单（真实通道轮询）
            oid = path[len("/api/billing/order/"):-len("/poll")]
            order = _ORDERS.get(oid)
            if order is None:
                self._json(404, {"error": "订单不存在"})
                return
            if order["status"] == "paid":
                self._json(200, {"status": "paid"})
                return
            provider = _provider(order.get("provider") or BILLING_PROVIDER)
            try:
                st = provider.query_payment(order)
            except Exception:
                st = "pending"
            if st == "paid":
                code, body = _settle_order(oid)
                self._json(code, {"status": "paid", **body})
                return
            self._json(200, {"status": "pending"})
            return
        if path == "/admin/api/orders":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "admin":
                self._json(403, {"error": "仅管理员可查看订单"})
                return
            self._json(200, {"orders": _all_orders()})
            return
        if path == "/admin/api/me":
            ok, principal = self._auth()
            if not ok:
                return
            body = {"role": principal["role"]}
            if principal["role"] == "tenant":
                body["tenant_id"] = principal["tenant_id"]
                body["name"] = _TENANTS.get(principal["tenant_id"], {}).get("name", "")
            self._json(200, body)
            return
        if path == "/admin/api/approvals":
            ok, principal = self._auth()
            if not ok:
                return
            queue = _queue_for(principal)
            self._json(
                200,
                {
                    "pending": [
                        {
                            "id": a.request_id,
                            "tool": a.tool,
                            "args": a.args,
                            "simulated_result": a.simulated_result,
                        }
                        for a in queue.pending()
                    ]
                },
            )
            return
        if path == "/admin/api/audit":
            ok, principal = self._auth()
            if not ok:
                return
            self._json(200, {"audit": _all_audit_rows(principal)})
            return
        if path == "/admin/api/reviews":
            ok, principal = self._auth()
            if not ok:
                return
            reviews = []
            for r in _all_audit_rows(principal):
                # 外部智能体审批应用事件携带 review 结果，展平为 verdict/score/reason
                if r.get("event") == "approval_applied" and isinstance(r.get("review"), dict):
                    v = dict(r["review"])
                    v["tool"] = r.get("tool")
                    v["ts"] = r.get("ts")
                    reviews.append(v)
            self._json(200, {"reviews": reviews[:20]})
            return
        if path == "/admin/api/agents":  # 多智能体总装线：各外部智能体就绪状态（可观测性）
            ok, principal = self._auth()
            if not ok:
                return
            agents = []
            for name, adapter in _EXTERNAL_AGENTS.items():
                try:
                    h = adapter.health() if hasattr(adapter, "health") else {"name": name, "ready": True}
                except Exception as e:  # noqa: BLE001
                    h = {"name": name, "ready": False, "hint": str(e)[:200]}
                agents.append({"name": name, **h})
            self._json(200, {"agents": agents})
            return
        if path == "/admin/api/usage":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] == "admin":
                rows = []
                for tid, t in _TENANTS.items():
                    rec = _tenant_record(tid)
                    c = _tenant_credit_state(t)
                    rows.append(
                        {
                            "tenant_id": tid,
                            "name": t.get("name", ""),
                            "status": t.get("status", "active"),
                            "tasks_used": rec["usage"]["tasks"],
                            "tasks_quota": rec["quota"]["tasks_per_month"],
                            "credits_used": c["monthly_used"],
                            "credits_quota": c["monthly_limit"],
                            "credits_remaining": c["monthly_remaining"],
                            "extra_credits": c["extra_remaining"],
                            "agents_quota": rec["quota"]["agents"],
                            "storage_quota_mb": rec["quota"]["storage_mb"],
                            "month": rec["usage"]["month"],
                        }
                    )
                self._json(200, {"usage": rows, "month": _current_month(),
                                 "rates": CREDIT_RATES, "packs": CREDIT_PACKS})
            else:
                rec = _tenant_record(principal["tenant_id"])
                if rec is None:
                    self._json(404, {"error": "租户不存在"})
                    return
                c = _tenant_credit_state(_TENANTS[principal["tenant_id"]])
                self._json(
                    200,
                    {
                        "usage": {
                            "tenant_id": principal["tenant_id"],
                            "tasks_used": rec["usage"]["tasks"],
                            "tasks_quota": rec["quota"]["tasks_per_month"],
                            "credits_used": c["monthly_used"],
                            "credits_quota": c["monthly_limit"],
                            "credits_remaining": c["monthly_remaining"],
                            "extra_credits": c["extra_remaining"],
                            "agents_quota": rec["quota"]["agents"],
                            "storage_quota_mb": rec["quota"]["storage_mb"],
                            "month": rec["usage"]["month"],
                        },
                        "rates": CREDIT_RATES,
                        "packs": CREDIT_PACKS,
                    },
                )
            return
        if path == "/admin/api/usage/export.csv":
            ok, principal = self._auth()
            if not ok:
                return
            self._csv(200, _usage_csv(principal), f"anengos_usage_{_current_month()}.csv")
            return
        if path == "/admin/api/settings/webhook":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "admin":
                self._json(403, {"error": "仅管理员可配置告警"})
                return
            if self.command == "GET":
                self._json(200, {"settings": {k: v for k, v in _WEBHOOK.items()},
                                 "recent": _WEBHOOK_LOG[:10]})
            else:
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
                except Exception:
                    self._json(400, {"error": "无效的 JSON"})
                    return
                url = str(body.get("url") or "").strip()
                if url and not (url.startswith("http://") or url.startswith("https://")):
                    self._json(400, {"error": "webhook URL 必须以 http(s):// 开头"})
                    return
                thr = float(body.get("threshold", 0.8))
                if not (0.1 <= thr <= 1.0):
                    self._json(400, {"error": "阈值必须在 0.1 ~ 1.0 之间"})
                    return
                _WEBHOOK["url"] = url or None
                _WEBHOOK["enabled"] = bool(body.get("enabled", False)) and bool(url)
                _WEBHOOK["threshold"] = thr
                self._json(200, {"ok": True, "settings": dict(_WEBHOOK)})
            return
        if path == "/admin/api/files":
            ok, principal = self._auth()
            if not ok:
                return
            ws = _workspace_for(principal)
            files = []
            if ws.exists():
                for p in sorted(ws.iterdir()):
                    if p.is_file():
                        files.append({"name": p.name, "size": _human_size(p.stat().st_size)})
                    elif p.is_dir():
                        n = sum(1 for _ in p.rglob("*") if _.is_file())
                        files.append({"name": p.name + "/", "size": f"{n} 个文件"})
            self._json(200, {"files": files})
            return
        if path == "/admin/api/tasks":
            ok, principal = self._auth()
            if not ok:
                return
            with _TASKS_LOCK:
                mine = [r for r in _TASKS.values() if _task_visible(r, principal)]
            mine.sort(key=lambda r: r.get("created_at", ""), reverse=True)
            self._json(
                200,
                {
                    "tasks": [
                        {k: r.get(k) for k in ("id", "status", "created_at", "finished_at",
                                                "query", "error", "attempts", "next_retry_at")}
                        for r in mine[:50]
                    ]
                },
            )
            return
        if path.startswith("/admin/api/tasks/"):
            ok, principal = self._auth()
            if not ok:
                return
            sub = path[len("/admin/api/tasks/"):].split("/")
            tid = sub[0]
            action = sub[1] if len(sub) > 1 else None
            if action in ("retry", "cancel"):  # 动作走 POST，GET 拒绝
                self._json(405, {"error": "请使用 POST 调用该动作"})
                return
            with _TASKS_LOCK:
                rec = _TASKS.get(tid)
            if rec is None or not _task_visible(rec, principal):
                self._json(404, {"error": "任务不存在"})
                return
            # 只返回可 JSON 序列化的公开字段，隐藏内部 Session/Agent 对象
            public = {k: v for k, v in rec.items() if not k.startswith("_")}
            self._json(200, public)
            return
        if path == "/admin/api/tenants":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "admin":
                self._json(403, {"error": "仅管理员可管理租户"})
                return
            self._json(
                200,
                {
                    "tenants": [
                        {
                            "id": tid,
                            "name": t.get("name", ""),
                            "status": t.get("status", "active"),
                            "created_at": t.get("created_at", ""),
                        }
                        for tid, t in _TENANTS.items()
                    ]
                },
            )
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path

        if path in ("/run", "/admin/api/run"):
            ok, principal = self._auth()
            if not ok:
                return
            query = self._read_query()
            if query is None:
                return
            self._json(*_run_task(query, principal))
            return

        if path == "/admin/api/run-async":
            ok, principal = self._auth()
            if not ok:
                return
            quota_err = _quota_error(principal)  # 任务次数配额（旧口径）
            if quota_err:
                self._json(429, {"error": quota_err})
                return
            credit_err = _charge_credits(principal, "task_run")  # 信用额度池（新口径）
            if credit_err:
                self._json(429, {"error": credit_err})
                return
            query = self._read_query()
            if query is None:
                return
            tid = _submit_async(query, principal)
            self._json(202, {"task_id": tid, "status": "running"})
            return

        # Failover 手动重放/取消（POST /admin/api/tasks/{id}/retry|cancel）
        if path.startswith("/admin/api/tasks/"):
            sub = path[len("/admin/api/tasks/"):].split("/")
            if len(sub) == 2 and sub[1] in ("retry", "cancel"):
                ok, principal = self._auth()
                if not ok:
                    return
                tid, action = sub[0], sub[1]
                with _TASKS_LOCK:
                    rec = _TASKS.get(tid)
                if rec is None or not _task_visible(rec, principal):
                    self._json(404, {"error": "任务不存在"})
                    return
                if action == "retry":
                    if rec.get("status") not in ("error", "interrupted", "cancelled"):
                        self._json(400, {"error": f"仅 error/interrupted/cancelled 任务可重放（当前 {rec.get('status')}）"})
                        return
                    rec["status"] = "queued"
                    rec["attempts"] = 0
                    rec["last_error"] = None
                    rec["next_retry_at"] = None
                    rec["error"] = None
                    rec["finished_at"] = None
                    _save_tasks()
                    import threading

                    threading.Thread(target=_run_task_with_failover, args=(tid, rec), daemon=True).start()
                    self._json(200, {"ok": True, "task_id": tid, "status": "queued"})
                    return
                if action == "cancel":
                    if rec.get("status") not in ("queued", "running", "retrying"):
                        self._json(400, {"error": f"仅 queued/running/retrying 任务可取消（当前 {rec.get('status')}）"})
                        return
                    rec["status"] = "cancelled"
                    rec["finished_at"] = _ts()
                    rec["error"] = "任务已取消"
                    _save_tasks()
                    self._json(200, {"ok": True, "task_id": tid, "status": "cancelled"})
                    return

        if path == "/admin/api/tenants":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "admin":
                self._json(403, {"error": "仅管理员可管理租户"})
                return
            payload = self._read_json()
            if payload is None:
                return
            name = str(payload.get("name", "")).strip()
            if not name:
                self._json(400, {"error": "name 不能为空"})
                return
            tid = secrets.token_hex(4)
            token = _new_token()
            _TENANTS[tid] = {
                "name": name,
                "token_hash": _hash_token(token),
                "status": "active",
                "created_at": _ts(),
                "quota": dict(DEFAULT_QUOTA),
                "usage": {"tasks": 0, "month": _current_month()},
            }
            _save_tenants()
            _tenant_workspace(tid).mkdir(parents=True, exist_ok=True)
            self._json(200, {
                "tenant_id": tid,
                "name": name,
                "token": token,  # 仅此一次明文返回，请立即转交客户并妥善保管
                "quota": dict(DEFAULT_QUOTA),
            })
            return

        # 自助开通：公开注册，免鉴权（试用配额），同一 IP 每日限 5 次
        if path == "/api/signup":
            if _signup_rate_limited(self.client_address[0]):
                self._json(429, {"error": f"同一 IP 每天最多开通 {_SIGNUP_LIMIT} 个租户"})
                return
            payload = self._read_json()
            if payload is None:
                return
            name = str(payload.get("name", "")).strip()
            if not name:
                self._json(400, {"error": "name 不能为空"})
                return
            # 人机验证（选填，Web 注册页必填；纯 API 兼容旧调用）
            if payload.get("captcha_id") or payload.get("captcha_answer"):
                if not _check_captcha(str(payload.get("captcha_id", "")), str(payload.get("captcha_answer", ""))):
                    self._json(400, {"error": "验证码错误或已过期"})
                    return
            tid = secrets.token_hex(4)
            token = _new_token()
            _TENANTS[tid] = {
                "name": name,
                "token_hash": _hash_token(token),
                "status": "active",
                "created_at": _ts(),
                "quota": dict(DEFAULT_QUOTA),
                "usage": {"tasks": 0, "month": _current_month()},
            }
            _save_tenants()
            _tenant_workspace(tid).mkdir(parents=True, exist_ok=True)
            # 可选：同时创建 Web 登录账号（username/password）
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", "")).strip()
            if username and password:
                if len(password) < 8:
                    self._json(400, {"error": "密码至少 8 位"})
                    return
                if username in _ACCOUNTS:
                    self._json(400, {"error": "用户名已存在"})
                    return
                _ACCOUNTS[username] = {
                    "password_hash": _hash_password(password),
                    "role": "tenant",
                    "tenant_id": tid,
                    "created_at": _ts(),
                }
                _save_accounts()
            self._json(201, {
                "tenant_id": tid,
                "name": name,
                "token": token,  # 仅此一次明文返回，请立即保存
                "plan": "trial",
                "quota": dict(DEFAULT_QUOTA),
                "account_created": bool(username and password),
            })
            return

        # Web 登录（公开）：验证码 + 账号密码 -> 会话 Cookie
        if path == "/api/login":
            payload = self._read_json()
            if payload is None:
                return
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", "")).strip()
            cid = str(payload.get("captcha_id", "")).strip()
            answer = str(payload.get("captcha_answer", "")).strip()
            if not username or not password:
                self._json(400, {"error": "用户名和密码不能为空"})
                return
            ip = self.client_address[0]
            key = _login_rate_key(username, ip)
            if _check_login_lock(key):
                self._json(429, {"error": "失败次数过多，请 15 分钟后再试"})
                return
            if not _check_captcha(cid, answer):
                self._json(400, {"error": "验证码错误或已过期"})
                return
            acc = _ACCOUNTS.get(username)
            if acc is None or not _verify_password(password, acc.get("password_hash", "")):
                left = LOGIN_MAX_FAILS - _register_fail(key)
                self._json(401, {"error": f"用户名或密码错误（剩余 {max(left, 0)} 次尝试）"})
                return
            _LOGIN_FAILS.pop(key, None)
            sid = _new_session(username, acc["role"], acc.get("tenant_id"))
            body = json.dumps({"ok": True, "username": username, "role": acc["role"]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie",
                             f"anengos_session={sid}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TTL}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Web 登出：销毁会话
        if path == "/api/logout":
            sid = _cookie_value(self.headers.get("Cookie", ""), "anengos_session")
            _SESSIONS.pop(sid, None)
            self._json(200, {"ok": True})
            return

        # 客户站登录（公开）：验证码 + 租户 token -> 租户会话 Cookie
        if path == "/api/client/login":
            payload = self._read_json()
            if payload is None:
                return
            token = str(payload.get("token", "")).strip()
            cid = str(payload.get("captcha_id", "")).strip()
            answer = str(payload.get("captcha_answer", "")).strip()
            if not token:
                self._json(400, {"error": "请输入租户访问令牌（Token）"})
                return
            ip = self.client_address[0]
            key = _login_rate_key("client|" + ip, "")
            if _check_login_lock(key):
                self._json(429, {"error": "失败次数过多，请 15 分钟后再试"})
                return
            if not _check_captcha(cid, answer):
                self._json(400, {"error": "验证码错误或已过期"})
                return
            tid = _tenant_from_token(token)
            if tid is None:
                left = LOGIN_MAX_FAILS - _register_fail(key)
                self._json(401, {"error": f"令牌无效或租户已停用（剩余 {max(left, 0)} 次尝试）"})
                return
            _LOGIN_FAILS.pop(key, None)
            sid = _new_session(f"tenant-{tid}", "tenant", tid)
            body = json.dumps({"ok": True, "tenant_id": tid}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie",
                             f"anengos_session={sid}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TTL}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 客户站登出：销毁会话
        if path == "/api/client/logout":
            sid = _cookie_value(self.headers.get("Cookie", ""), "anengos_session")
            _SESSIONS.pop(sid, None)
            self._json(200, {"ok": True})
            return

        # 知识库：上传 /admin/api/tenants/{tid}/knowledge/upload
        m = re.match(r"^/admin/api/tenants/([0-9a-f]+)/knowledge/upload$", path)
        if m:
            ok, principal = self._auth()
            if not ok:
                return
            tid = m.group(1)
            if principal["role"] == "tenant" and principal["tenant_id"] != tid:
                self._json(403, {"error": "只能操作自己的知识库"})
                return
            payload = self._read_json()
            if payload is None:
                return
            code, body = _upload_knowledge(tid, str(payload.get("filename", "")),
                                           str(payload.get("content", "")),
                                           payload.get("tags") or [], principal)
            self._json(code, body)
            return
        # 知识库：检索 /admin/api/tenants/{tid}/knowledge/search
        m = re.match(r"^/admin/api/tenants/([0-9a-f]+)/knowledge/search$", path)
        if m:
            ok, principal = self._auth()
            if not ok:
                return
            tid = m.group(1)
            if principal["role"] == "tenant" and principal["tenant_id"] != tid:
                self._json(403, {"error": "只能检索自己的知识库"})
                return
            payload = self._read_json()
            if payload is None:
                return
            self._json(200, {"query": str(payload.get("query", "")), "results": _search_knowledge(tid, str(payload.get("query", "")))})
            return
        # 知识库：AI 问答（RAG：检索 + DeepSeek 生成） /admin/api/tenants/{tid}/knowledge/ask
        m = re.match(r"^/admin/api/tenants/([0-9a-f]+)/knowledge/ask$", path)
        if m:
            ok, principal = self._auth()
            if not ok:
                return
            tid = m.group(1)
            if principal["role"] == "tenant" and principal["tenant_id"] != tid:
                self._json(403, {"error": "只能问答自己的知识库"})
                return
            payload = self._read_json()
            if payload is None:
                return
            body = _ask_knowledge(tid, str(payload.get("query", "")), principal)
            code = 200 if body.get("answer") is not None else 200
            self._json(code, body)
            return
        # 知识库：删除 /admin/api/tenants/{tid}/knowledge/{doc_id}/delete
        m = re.match(r"^/admin/api/tenants/([0-9a-f]+)/knowledge/([0-9a-f]+)/delete$", path)
        if m:
            ok, principal = self._auth()
            if not ok:
                return
            tid, doc_id = m.group(1), m.group(2)
            if principal["role"] == "tenant" and principal["tenant_id"] != tid:
                self._json(403, {"error": "只能操作自己的知识库"})
                return
            code, body = _delete_knowledge_doc(tid, doc_id, principal)
            self._json(code, body)
            return

        # 租户知识库 API（客户自己问自己的资料；租户 token 鉴权 + 信用额度池计费）
        if path == "/api/tenant/knowledge/upload":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "tenant":
                self._json(403, {"error": "租户知识库 API 仅租户可用"})
                return
            quota_err = _charge_credits(principal, "knowledge_upload")
            if quota_err:
                self._json(429, {"error": quota_err})
                return
            payload = self._read_json()
            if payload is None:
                return
            code, body = _upload_knowledge(principal["tenant_id"], str(payload.get("filename", "")),
                                           str(payload.get("content", "")), payload.get("tags") or [], principal)
            self._json(code, body)
            return
        if path == "/api/tenant/knowledge/search":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "tenant":
                self._json(403, {"error": "租户知识库 API 仅租户可用"})
                return
            quota_err = _charge_credits(principal, "knowledge_search")
            if quota_err:
                self._json(429, {"error": quota_err})
                return
            payload = self._read_json()
            if payload is None:
                return
            self._json(200, {"query": str(payload.get("query", "")),
                             "results": _search_knowledge(principal["tenant_id"], str(payload.get("query", "")))})
            return
        if path == "/api/tenant/knowledge/ask":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "tenant":
                self._json(403, {"error": "租户知识库 API 仅租户可用"})
                return
            quota_err = _charge_credits(principal, "knowledge_ask")
            if quota_err:
                self._json(429, {"error": quota_err})
                return
            payload = self._read_json()
            if payload is None:
                return
            body = _ask_knowledge(principal["tenant_id"], str(payload.get("query", "")), principal)
            _bump_usage(principal)  # AI 问答计 1 次任务用量（与信用额度并存，兼容旧报表）
            self._json(200, body)
            return
        m = re.match(r"^/api/tenant/knowledge/([0-9a-f]+)/delete$", path)
        if m:
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "tenant":
                self._json(403, {"error": "租户知识库 API 仅租户可用"})
                return
            code, body = _delete_knowledge_doc(principal["tenant_id"], m.group(1), principal)
            self._json(code, body)
            return

        # MCP 端点（Streamable HTTP）：客户的企业 agent 用租户 token 直接问自己的资料
        if path == "/mcp":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}})
                return
            token = ""
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            tid = _tenant_from_token(token)
            if tid is None:
                self._json(401, {"error": "无效的租户访问令牌（MCP 仅接受租户 token，管理员请走 /admin）"})
                return
            resp = _mcp_handle(payload, tid)
            if resp is None:  # 通知（notifications/initialized 等）无需响应
                self._json(202, {})
                return
            self._json(200, resp)
            return

        # 支付下单（公开）：创建订单，返回 order_id 供收银台支付
        if path == "/api/billing/order":
            payload = self._read_json()
            if payload is None:
                return
            plan_id = str(payload.get("plan", "")).strip()
            tid = str(payload.get("tenant_id") or "").strip() or None
            name = str(payload.get("name", "")).strip()
            provider = str(payload.get("provider") or BILLING_PROVIDER).strip()
            code, body = _create_order(plan_id, tid, name, provider)
            self._json(code, body)
            return

        # 支付回调（公开）：按订单通道分发验签；mock 直接结算
        if path == "/api/billing/notify":
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            oid = ""
            if path.startswith("/api/billing/order/"):
                oid = path[len("/api/billing/order/"):]
            order = _ORDERS.get(oid) if oid else None
            provider = _provider(order.get("provider") if order else BILLING_PROVIDER)
            try:
                code, info = provider.handle_notify(raw, {k.lower(): v for k, v in self.headers.items()})
            except Exception as exc:
                self._json(500, {"error": f"回调处理异常：{exc}"})
                return
            if info.get("settle") and info.get("order_id"):
                code, body = _settle_order(info["order_id"])
                self._json(code, body)
                return
            self._json(code, info)
            return

        # 管理员手动补单（线下收款 / 通道故障时）
        parts = path.strip("/").split("/")
        if len(parts) == 5 and parts[0] == "admin" and parts[1] == "api" and parts[2] == "orders" and parts[4] == "mark-paid":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "admin":
                self._json(403, {"error": "仅管理员可补单"})
                return
            code, body = _settle_order(parts[3])
            self._json(code, body)
            return

        # 告警 Webhook 设置（POST；GET 见 do_GET）
        if path == "/admin/api/settings/webhook":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "admin":
                self._json(403, {"error": "仅管理员可配置告警"})
                return
            try:
                body = self._read_json()
                if body is None:
                    return
            except Exception:
                self._json(400, {"error": "无效的 JSON"})
                return
            url = str(body.get("url") or "").strip()
            if url and not (url.startswith("http://") or url.startswith("https://")):
                self._json(400, {"error": "webhook URL 必须以 http(s):// 开头"})
                return
            thr = float(body.get("threshold", 0.8))
            if not (0.1 <= thr <= 1.0):
                self._json(400, {"error": "阈值必须在 0.1 ~ 1.0 之间"})
                return
            _WEBHOOK["url"] = url or None
            _WEBHOOK["enabled"] = bool(body.get("enabled", False)) and bool(url)
            _WEBHOOK["threshold"] = thr
            self._json(200, {"ok": True, "settings": dict(_WEBHOOK)})
            return

        if path == "/admin/api/approvals/approve-all":
            ok, principal = self._auth()
            if not ok:
                return
            queue = _queue_for(principal)
            items = [i for i in queue.pending()]
            for item in items:
                queue.approve(item.request_id)
                _apply_approval(item, principal)
            self._json(200, {"approved": len(items)})
            return

        parts = path.strip("/").split("/")
        # /admin/api/tenants/{id}/quota  管理员设置租户配额
        if len(parts) == 5 and parts[0] == "admin" and parts[1] == "api" and parts[2] == "tenants" and parts[4] == "quota":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "admin":
                self._json(403, {"error": "仅管理员可管理租户"})
                return
            tid = parts[3]
            t = _TENANTS.get(tid)
            if t is None:
                self._json(404, {"error": f"租户不存在: {tid}"})
                return
            payload = self._read_json()
            if payload is None:
                return
            quota = t.setdefault("quota", dict(DEFAULT_QUOTA))
            for key in ("tasks_per_month", "credits_per_month", "agents", "storage_mb"):
                if key in payload:
                    try:
                        quota[key] = max(0, int(payload[key]))
                    except (TypeError, ValueError):
                        self._json(400, {"error": f"{key} 需为非负整数"})
                        return
            if "extra_credits" in payload:  # 管理员可直接补给按量包余额
                try:
                    t.setdefault("usage", {"tasks": 0, "month": _current_month()})
                    t["usage"]["extra_credits"] = max(0, int(payload["extra_credits"]))
                except (TypeError, ValueError):
                    self._json(400, {"error": "extra_credits 需为非负整数"})
                    return
            t.setdefault("usage", {"tasks": 0, "month": _current_month()})
            _save_tenants()
            self._json(200, {"message": f"租户 {tid} 配额已更新", "quota": quota})
            return
        # /admin/api/tenants/{id}/revoke
        if len(parts) == 5 and parts[0] == "admin" and parts[1] == "api" and parts[2] == "tenants" and parts[4] == "revoke":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] != "admin":
                self._json(403, {"error": "仅管理员可管理租户"})
                return
            tid = parts[3]
            if tid not in _TENANTS:
                self._json(404, {"error": f"租户不存在: {tid}"})
                return
            _TENANTS[tid]["status"] = "inactive"
            _save_tenants()
            _TENANT_QUEUES.pop(tid, None)
            self._json(200, {"message": f"租户 {tid} 已停用"})
            return

        # /admin/api/approvals/{id}/approve | reject
        if len(parts) == 5 and parts[0] == "admin" and parts[1] == "api" and parts[2] == "approvals":
            ok, principal = self._auth()
            if not ok:
                return
            req_id, action = parts[3], parts[4]
            queue = _queue_for(principal)
            item = queue.get(req_id)
            if item is None:
                self._json(404, {"error": f"审批项不存在: {req_id}"})
                return
            if action == "approve":
                if not queue.approve(req_id):
                    self._json(409, {"error": "该审批项已处理"})
                    return
                result = _apply_approval(item, principal)
                _maybe_resume(principal, req_id, result)
                self._json(200, {"message": "已批准并真实执行", "result": result})
                return
            if action == "reject":
                if not queue.reject(req_id):
                    self._json(409, {"error": "该审批项已处理"})
                    return
                _reject_approval(item, principal)
                _maybe_resume(principal, req_id, "[已拒绝] 该操作未执行")
                self._json(200, {"message": "已拒绝"})
                return
            self._json(404, {"error": f"未知操作: {action}"})
            return
        self._json(404, {"error": "not found"})


def _ts() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def main() -> None:
    port = int(os.environ.get("ANENGOS_PORT", "8080"))
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    (BASE / "audit").mkdir(parents=True, exist_ok=True)
    _load_tenants()
    _load_orders()
    _load_accounts()
    _load_tasks()  # Failover：重启后断点续跑（queued 自动续跑，running 等标记 interrupted）
    _ensure_admin()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(
        f"ANENGOS listening on :{port}（health: /health, run: POST /run, 管理台: /）",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
