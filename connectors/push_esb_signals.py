# -*- coding: utf-8 -*-
"""二四棒信号写表（飞书多维表「交易系统（二四棒）」tbl9ak6Yt92pGSY4）。

读取 quant_esb_scan 最新扫描产物（breakout 突破 / pending 挂单），结合纸面台账
持仓与历史成交，按股票代码 upsert 进独立表，并做状态闭环：
  - 突破且当前持仓 → 持仓；突破未持仓 → 候选；接近突破 → 挂单；
  - 已卖出为终态，不被后续候选回写覆盖；
  - 本轮未再入选的旧候选/挂单：仍持仓则保留，已成交离场 → 已卖出，其余 → 已剔除。

凭据优先读 secrets 文件，回退环境变量，不硬编码 app_secret。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from connectors.push_daily_review import (  # 复用重试 / 取 token / JSON 容错
    _load_json,
    get_token,
    request_with_retry,
)

SECRETS = "/srv/anengos/workspace/.secrets/feishu_app.json"
BASE_TOKEN = "W3cob77DEanyS8sibGCcg07SnWd"
TABLE_ID = "tbl9ak6Yt92pGSY4"
POS_FILE = "/srv/anengos/workspace/paper_positions.json"
PENDING_FILE = "/srv/anengos/workspace/paper_pending_orders.json"
SCAN_DIRNAME = "esb_scan_output"

# 候选/挂单超过该天数未再入选 → 归档为“已剔除”；持仓/已卖出终态不动。
ARCHIVE_DAYS = 7
TERMINAL_STATUS = {"已卖出"}
BATCH_SIZE = 200  # 飞书批量写入单批上限，超出切片循环提交

TYPE_BREAKOUT = "突破"
TYPE_PENDING = "挂单"

# 沪深 A 股段前缀：沪 60/68，深 00/30；北交所(83/87/88/92/43/920 等)不纳入。
_A_SHARE_PREFIX = {"60", "68", "00", "30"}


def _is_a_share(code: str) -> bool:
    """6 位数字且属沪深段；北交所/非法代码返回 False。"""
    return bool(re.fullmatch(r"\d{6}", code)) and code[:2] in _A_SHARE_PREFIX


# ---------------------------------------------------------------------------
# 纯读取 / 状态判定（不触网，供单测）
# ---------------------------------------------------------------------------

def _latest_scan(ws: Path) -> dict:
    """读最新 esb_scan_*.json；缺失/损坏返回空结构。"""
    d = ws / SCAN_DIRNAME
    if not d.exists():
        return {"breakout": [], "pending": [], "signals": []}
    files = sorted(d.glob("esb_scan_*.json"), reverse=True)
    if not files:
        return {"breakout": [], "pending": [], "signals": []}
    data = _load_json(str(files[0]), {})
    if not isinstance(data, dict):
        return {"breakout": [], "pending": [], "signals": []}
    data.setdefault("breakout", [])
    data.setdefault("pending", [])
    data.setdefault("signals", data["breakout"] + data["pending"])
    return data


def account_state(ws: Path) -> tuple[dict[str, dict], set[str]]:
    """返回 (持仓按代码映射, 已离场代码集合)。

    持仓取自台账 positions；已离场取自 history 中已有卖出记录、且当前不在持仓的代码。
    """
    pos = _load_json(str(ws / "paper_positions.json"), {})
    held: dict[str, dict] = {}
    for p in pos.get("positions") or []:
        code = str(p.get("code", ""))
        if re.fullmatch(r"\d{6}", code):
            held[code] = p
    sold: set[str] = set()
    for h in pos.get("history") or []:
        code = str(h.get("code", ""))
        if re.fullmatch(r"\d{6}", code) and h.get("sell_date") and code not in held:
            sold.add(code)
    return held, sold


_CST = timezone(timedelta(hours=8))


def _date_ts(value) -> int | None:
    """信号日期(YYYY-MM-DD) → 东八区 15:00 毫秒时间戳（DateTime 直连 REST 写入）。"""
    if not value:
        return None
    s = str(value)[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None
    dt = datetime.strptime(s, "%Y-%m-%d").replace(
        hour=15, minute=0, second=0, tzinfo=_CST)
    return int(dt.timestamp() * 1000)


def _parse_date(value) -> datetime | None:
    """回读日期兼容毫秒时间戳与字符串，返回 naive datetime 供归档比较。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, _CST).replace(tzinfo=None)
    s = str(value)[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return datetime.strptime(s, "%Y-%m-%d")
    return None


def _num(value):
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def build_row(sig: dict, held: dict[str, dict], sold: set[str]) -> dict | None:
    """构造一条信号记录字段；代码非法返回 None。"""
    code = str(sig.get("code", ""))
    if not _is_a_share(code):
        return None
    name = sig.get("name") or code
    sig_type = TYPE_BREAKOUT if sig.get("type") == "breakout" else TYPE_PENDING

    position = held.get(code)
    if position is not None:
        status = "持仓"
    elif code in sold:
        # 终态：历史已离场，本轮不再回写该行（由 upsert 层跳过）
        return {"_terminal": True, "code": code}
    elif sig_type == TYPE_PENDING:
        status = "挂单"
    else:
        status = "候选"

    fields: dict = {
        "代码": code,
        "名称": name,
        "信号类型": sig_type,
        "状态": status,
    }
    d = _date_ts(sig.get("last_date"))
    if d is not None:
        fields["信号日期"] = d

    entry_px = _num(sig.get("entry_px"))
    stop_px = _num(sig.get("stop_px"))
    if entry_px is not None:
        fields["左峰价"] = entry_px
    if stop_px is not None:
        fields["左级止损"] = stop_px

    if position is not None:
        stop = _num(position.get("stop") or position.get("init_stop"))
        if stop is not None:
            fields["移动止损"] = stop
        shares = position.get("shares")
        if shares is not None:
            fields["计划股数"] = int(shares)
        cur = _num(position.get("current_price"))
        if cur is not None:
            fields["收盘价"] = cur
        if position.get("buy_date"):
            fields["备注"] = f"买入日 {position['buy_date']}"
    else:
        shares = sig.get("shares")
        if shares is not None:
            fields["计划股数"] = int(shares)
        close_px = _num(sig.get("close_px"))
        if close_px is not None:
            fields["收盘价"] = close_px
        gap = _num(sig.get("gap_pct"))
        if gap is not None:
            fields["距突破%"] = gap

    if sig.get("peak_idx") is not None:
        fields["峰idx"] = int(sig["peak_idx"])
    if sig.get("level_idx") is not None:
        fields["级idx"] = int(sig["level_idx"])
    return {"_terminal": False, "code": code, "fields": fields}


def archive_plan(existing: list[dict], signal_codes: set[str],
                 held: dict[str, dict], sold: set[str],
                 today: datetime, archive_days: int = ARCHIVE_DAYS,
                 excluded: dict[str, str] | None = None) -> list[dict]:
    """返回需归档的既有记录：旧候选/挂单本轮未入选；持仓保留，已离场置已卖出，其余剔除。

    existing 元素：{record_id, code, status, date(可读回值)}。
    excluded: {code: 排雷原因}——本轮被 ST/退市或连续三年负现金流主动剔除，立即归档，不走天数宽限。
    """
    excluded = excluded or {}
    cutoff = today - timedelta(days=archive_days)
    plan: list[dict] = []
    for rec in existing:
        status = rec.get("status")
        if status in TERMINAL_STATUS:
            continue
        if rec["code"] in signal_codes:
            continue
        if rec["code"] in held:
            continue  # 仍持仓：不因本轮扫描未入选而归档
        if rec["code"] in excluded:
            plan.append({**rec, "target": "已剔除", "note": excluded[rec["code"]]})
            continue
        if rec["code"] in sold:
            plan.append({**rec, "target": "已卖出"})
            continue
        d = rec.get("date")
        if d is None:
            plan.append({**rec, "target": "已剔除"})
            continue
        parsed = _parse_date(d)
        if parsed is None or parsed < cutoff:
            plan.append({**rec, "target": "已剔除"})
    return plan


# ---------------------------------------------------------------------------
# 飞书写入
# ---------------------------------------------------------------------------

def _credentials() -> dict:
    sec = _load_json(SECRETS, {})
    app_id = sec.get("app_id") or os.environ.get("FEISHU_APP_ID")
    app_secret = sec.get("app_secret") or os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("飞书凭据缺失：secrets 文件与环境变量均未取到 app_id/app_secret")
    return {"app_id": app_id, "app_secret": app_secret}


def _list_all(token: str) -> list[dict]:
    items, page_token = [], None
    while True:
        url = ("https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records"
               "?page_size=100" % (BASE_TOKEN, TABLE_ID))
        if page_token:
            url += "&page_token=" + page_token
        _, r = request_with_retry("GET", url, token=token)
        data = r.get("data", {}) if isinstance(r, dict) else {}
        items.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    return items


def _existing_map(items: list[dict]) -> list[dict]:
    """读回既有记录：code / status / date / record_id；同代码取最新一条。"""
    out: dict[str, dict] = {}
    for it in items:
        f = it.get("fields", {})
        code = str(f.get("代码", "") or "")
        if not re.fullmatch(r"\d{6}", code):
            continue
        status_val = f.get("状态")
        status = status_val[0] if isinstance(status_val, list) and status_val else status_val
        rec = {"record_id": it.get("record_id"), "code": code,
               "status": status, "date": f.get("信号日期")}
        prev = out.get(code)
        if prev is None or (str(rec["date"]) or "") > (str(prev["date"]) or ""):
            out[code] = rec
    return list(out.values())


def _batch_post(url: str, records: list[dict], token: str,
                batch_size: int = BATCH_SIZE) -> int:
    """按批提交 records；返回成功条数。单批失败打印响应并继续后续批次。"""
    ok = 0
    for i in range(0, len(records), batch_size):
        chunk = records[i:i + batch_size]
        s, r = request_with_retry("POST", url, {"records": chunk}, token)
        if s == 200 and isinstance(r, dict) and r.get("code") == 0:
            ok += len(chunk)
        else:
            print("BATCH_FAIL", s, r)
    return ok


def push(ws: str | Path = "/srv/anengos/workspace") -> dict:
    w = Path(ws)
    scan = _latest_scan(w)
    held, sold = account_state(w)
    today = datetime.now()

    rows: list[dict] = []
    signal_codes: set[str] = set()
    for sig in scan.get("signals") or []:
        row = build_row(sig, held, sold)
        if row is None:
            continue
        signal_codes.add(row["code"])
        if row["_terminal"]:
            continue  # 终态行不回写、不新建
        rows.append(row)

    # 本轮排雷主动剔除：ST/退市 + 连续三年经营现金流为负；立即归档，不走天数宽限
    excluded: dict[str, str] = {}
    for e in scan.get("excluded_st") or []:
        code = e if isinstance(e, str) else e.get("code", "")
        if code:
            excluded[code] = "ST/退市，排雷剔除"
    for e in scan.get("excluded_cf") or []:
        code = e.get("code", "")
        if code:
            excluded[code] = e.get("exclude_reason", "连续三年经营现金流为负，排雷剔除")

    cred = _credentials()
    token = get_token(cred)

    existing = _existing_map(_list_all(token))
    existing_by_code = {r["code"]: r for r in existing}

    to_create: list[dict] = []
    to_update: dict[str, dict] = {}
    for row in rows:
        prev = existing_by_code.get(row["code"])
        if prev:
            to_update[prev["record_id"]] = row["fields"]
        else:
            to_create.append({"fields": row["fields"]})

    base = "https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records" % (
        BASE_TOKEN, TABLE_ID)

    archived_plan = archive_plan(existing, signal_codes, held, sold, today,
                                 excluded=excluded)

    create_records = to_create
    update_records = [{"record_id": rid, "fields": f} for rid, f in to_update.items()]
    archive_records = [
        {"record_id": r["record_id"],
         "fields": {"状态": r["target"], **({"备注": r["note"]} if r.get("note") else {})}}
        for r in archived_plan]

    created = _batch_post(base + "/batch_create", create_records, token)
    updated = _batch_post(base + "/batch_update", update_records, token)
    archived = _batch_post(base + "/batch_update", archive_records, token)

    summary = {"signals": len(rows), "held": len(held), "created": created,
               "updated": updated, "archived": archived}
    print("PUSH_ESB_SIGNALS %s" % json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    push()
