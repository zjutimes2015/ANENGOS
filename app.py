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

import hashlib
import hmac
import json
import os
import secrets
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

# ---------- 租户用量配额与计费 ----------
# 计费模型：按租户月度任务次数计费（BYOK：模型成本客户自理，平台只收治理层）。
# 租户记录扩展：quota={tasks_per_month, agents, storage_mb}，usage={tasks, month}。
DEFAULT_QUOTA: dict[str, int] = {"tasks_per_month": 100, "agents": 1, "storage_mb": 100}
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
    if "usage" not in t or not isinstance(t["usage"], dict):
        t["usage"] = {"tasks": 0, "month": _current_month()}
    if t["usage"].get("month") != _current_month():  # 月度滚动：归档上月用量为账单记录
        t.setdefault("billing", {})
        t["billing"][t["usage"]["month"]] = {"tasks": t["usage"].get("tasks", 0)}
        t["usage"] = {"tasks": 0, "month": _current_month()}
        _WEBHOOK_FIRED.pop(tid, None)  # 新账期重新允许告警
    return t


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
        "quota": {"tasks_per_month": 100, "agents": 1, "storage_mb": 100},
    },
    "team": {
        "name": "团队版",
        "price_cents": 29900,
        "period": "月",
        "quota": {"tasks_per_month": 1000, "agents": 3, "storage_mb": 1000},
    },
    "enterprise": {
        "name": "企业版",
        "price_cents": 150000,
        "period": "月",
        "quota": {"tasks_per_month": 10000, "agents": 10, "storage_mb": 5000},
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
    """创建支付订单；校验套餐与租户（tenant_ref 可为租户 token 或 tenant_id）。

    非 mock 通道会调用适配器 create_payment 生成真实支付参数（qrcode/code_url）。
    """
    plan = BILLING_PLANS.get(plan_id)
    if plan is None:
        return 400, {"error": f"未知套餐：{plan_id}"}
    tid = _tenant_id_by_token(tenant_ref) if tenant_ref else None
    if tenant_ref and tid is None:
        return 404, {"error": "租户不存在，请先自助开通获取 token"}
    if plan_id == "trial":
        return 400, {"error": "试用版免费，请直接使用自助开通"}
    provider = (provider or BILLING_PROVIDER).strip().lower() or "mock"
    order = {
        "order_id": "od_" + secrets.token_hex(8),
        "plan": plan_id,
        "plan_name": plan["name"],
        "amount_cents": plan["price_cents"],
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
    """支付回调：标记订单已支付并升级租户配额（幂等）。"""
    order = _ORDERS.get(order_id)
    if order is None:
        return 404, {"error": "订单不存在"}
    if order["status"] == "paid":
        return 200, {"ok": True, "order_id": order_id, "status": "paid", "already": True}
    if order["status"] != "pending":
        return 400, {"error": f"订单状态异常：{order['status']}"}
    plan = BILLING_PLANS.get(order["plan"])
    if plan is None:
        return 400, {"error": "套餐已失效"}
    tid = order.get("tenant_id")
    if tid:
        t = _tenant_record(tid)
        if t:
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
                 "plan": order["plan"], "amount_cents": order["amount_cents"]}


def _all_orders() -> list[dict[str, Any]]:
    return [dict(o) for o in sorted(_ORDERS.values(), key=lambda x: x.get("created_at", ""), reverse=True)]
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
    if limit <= 0:
        return
    ratio = used / limit
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


def _save_tenants() -> None:
    TENANTS_FILE.write_text(
        json.dumps(_TENANTS, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _tenant_workspace(tid: str) -> Path:
    return WORKSPACE / "tenants" / tid


def _auth_principal(headers) -> tuple[bool, str, dict[str, Any] | None]:
    """校验令牌并解析身份：{"role":"admin"} 或 {"role":"tenant","tenant_id":...}。"""
    admin_tok = _admin_token()
    if not admin_tok:
        return False, "服务未配置 ANENGOS_API_TOKEN（管理员令牌），接口已禁用；设置令牌后重启", None
    provided = headers.get("Authorization", "")
    if provided.startswith("Bearer "):
        provided = provided[7:]
    else:
        provided = headers.get("X-API-Token", "")
    if not provided:
        return False, "缺少访问令牌：请带 Authorization: Bearer <token>", None
    if hmac.compare_digest(provided, admin_tok):
        return True, "", {"role": "admin"}
    h = _hash_token(provided)
    for tid, t in _TENANTS.items():
        if t.get("status") == "active" and hmac.compare_digest(t.get("token_hash", ""), h):
            return True, "", {"role": "tenant", "tenant_id": tid}
    return False, "访问令牌错误或租户已停用", None


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

    tools = _shared_context()[1] if principal["role"] == "admin" else _build_tools(ws, actor)
    audit = _new_audit(principal)
    try:
        llm = OpenAICompatLLM()
    except ValueError as e:
        return None, queue, str(e)
    os_ = AgentOS(actor, tools, Gatekeeper(registry, "api"), queue, audit, llm)
    return os_, queue, ""


def _health_body() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "anengos",
        "has_api_key": bool(os.environ.get("ANENGOS_API_KEY", "")),
        "auth_required": bool(_admin_token()),
        "model": os.environ.get("ANENGOS_MODEL", "gpt-4o-mini"),
        "workspace": str(WORKSPACE),
        "tenants": len([t for t in _TENANTS.values() if t.get("status") == "active"]),
    }


def _run_task(query: str, principal: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    quota_err = _quota_error(principal)
    if quota_err:
        return 429, {"error": quota_err}
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


def _submit_async(query: str, principal: dict[str, Any]) -> str:
    """提交异步任务：立即返回 task_id，后台线程执行；遇待审批挂起，审批后自动续跑。"""
    import threading

    tid = secrets.token_hex(6)
    rec: dict[str, Any] = {
        "id": tid,
        "status": "running",
        "created_at": _ts(),
        "finished_at": None,
        "role": principal["role"],
        "tenant_id": principal.get("tenant_id"),
        "query": query,
        "output": None,
        "blocked": None,
        "pending_approvals": [],
        "error": None,
    }
    with _TASKS_LOCK:
        _TASKS[tid] = rec

    def _worker() -> None:
        try:
            agent, _, err = build_agent(principal)
            if agent is None:
                rec["error"] = err
                rec["status"] = "error"
                return
            session = agent.run(query, max_steps=20)
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
            else:
                rec["output"] = session.output
                rec["blocked"] = session.blocked_reason
                rec["pending_approvals"] = []
                rec["status"] = "done"
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)[:500]
            rec["status"] = "error"
        finally:
            if rec["status"] != "waiting_approval":
                rec["finished_at"] = _ts()

    threading.Thread(target=_worker, daemon=True).start()
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
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)[:500]
            rec["status"] = "error"
            rec["finished_at"] = _ts()

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
                "agents_quota", "storage_quota_mb", "status"])
    if principal["role"] == "admin":
        items = [(tid, t) for tid, t in _TENANTS.items()]
    else:
        tid = principal["tenant_id"]
        t = _TENANTS.get(tid)
        items = [(tid, t)] if t else []
    for tid, t in items:
        rec = _tenant_record(tid)
        w.writerow([tid, t.get("name", ""), rec["usage"]["month"],
                    rec["usage"]["tasks"], rec["quota"]["tasks_per_month"],
                    rec["quota"]["agents"], rec["quota"]["storage_mb"],
                    t.get("status", "active")])
        for m, u in sorted((t.get("billing") or {}).items()):
            w.writerow([tid, t.get("name", ""), m, u.get("tasks", 0),
                        rec["quota"]["tasks_per_month"], rec["quota"]["agents"],
                        rec["quota"]["storage_mb"], "billed"])
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
        if path == "/pay":  # 模拟收银台（mock 通道）
            try:
                self._html(200, (BASE / "pay.html").read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._html(200, "<h1>ANENGOS</h1><p>pay.html 缺失</p>")
            return
        if path == "/api/billing/plans":  # 公开：套餐列表
            self._json(200, {"provider": BILLING_PROVIDER, "plans": BILLING_PLANS})
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
        if path == "/admin/api/usage":
            ok, principal = self._auth()
            if not ok:
                return
            if principal["role"] == "admin":
                rows = []
                for tid, t in _TENANTS.items():
                    rec = _tenant_record(tid)
                    rows.append(
                        {
                            "tenant_id": tid,
                            "name": t.get("name", ""),
                            "status": t.get("status", "active"),
                            "tasks_used": rec["usage"]["tasks"],
                            "tasks_quota": rec["quota"]["tasks_per_month"],
                            "agents_quota": rec["quota"]["agents"],
                            "storage_quota_mb": rec["quota"]["storage_mb"],
                            "month": rec["usage"]["month"],
                        }
                    )
                self._json(200, {"usage": rows, "month": _current_month()})
            else:
                rec = _tenant_record(principal["tenant_id"])
                if rec is None:
                    self._json(404, {"error": "租户不存在"})
                    return
                self._json(
                    200,
                    {
                        "usage": {
                            "tenant_id": principal["tenant_id"],
                            "tasks_used": rec["usage"]["tasks"],
                            "tasks_quota": rec["quota"]["tasks_per_month"],
                            "agents_quota": rec["quota"]["agents"],
                            "storage_quota_mb": rec["quota"]["storage_mb"],
                            "month": rec["usage"]["month"],
                        }
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
                        {k: r.get(k) for k in ("id", "status", "created_at", "finished_at", "query", "error")}
                        for r in mine[:50]
                    ]
                },
            )
            return
        if path.startswith("/admin/api/tasks/"):
            ok, principal = self._auth()
            if not ok:
                return
            tid = path[len("/admin/api/tasks/"):]
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
            quota_err = _quota_error(principal)
            if quota_err:
                self._json(429, {"error": quota_err})
                return
            query = self._read_query()
            if query is None:
                return
            tid = _submit_async(query, principal)
            self._json(202, {"task_id": tid, "status": "running"})
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
            self._json(201, {
                "tenant_id": tid,
                "name": name,
                "token": token,  # 仅此一次明文返回，请立即保存
                "plan": "trial",
                "quota": dict(DEFAULT_QUOTA),
            })
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
            for key in ("tasks_per_month", "agents", "storage_mb"):
                if key in payload:
                    try:
                        quota[key] = max(0, int(payload[key]))
                    except (TypeError, ValueError):
                        self._json(400, {"error": f"{key} 需为非负整数"})
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
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(
        f"ANENGOS listening on :{port}（health: /health, run: POST /run, 管理台: /）",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
