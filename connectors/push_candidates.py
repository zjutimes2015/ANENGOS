# -*- coding: utf-8 -*-
"""候选名单统一写表（飞书多维表 tblU1ymA0Id1UPJ4）。

聚合三个信号源的本地产物，按股票代码 upsert 进同一张候选表，并做状态闭环：
  - 全A低波：all_a_output/all_a_*.md（离线本地库低波池）
  - 多因子低波：multifactor_output/multifactor_*.md（指数成分低波+质量价值）
  - ML封单：workspace/ml_seal_strength.json（封单强度连板预测 Top10）

规则：
  - 凭据优先读 secrets 文件，回退环境变量，不硬编码 app_secret；
  - 同一代码本轮已存在 → 更新该行（刷新名称/排名/总分/收盘价/明细/来源/日期/状态）；
    不存在 → 新建；多来源命中时“来源”标签合并；
  - 状态闭环：候选（本轮入选）/ 已买入 / 已卖出（终态，归档不覆盖）/ 已剔除（超期未再入选）；
  - 只增状态与更新，不物理删除历史记录；归档把超期旧候选置为“已剔除”。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from connectors.push_daily_review import (  # 复用重试 / 取 token / JSON 容错
    _load_json,
    get_token,
    request_with_retry,
)

SECRETS = "/srv/anengos/workspace/.secrets/feishu_app.json"
BASE_TOKEN = "W3cob77DEanyS8sibGCcg07SnWd"
TABLE_ID = "tblU1ymA0Id1UPJ4"

# 候选超过该天数未再入选 → 归档为“已剔除”；已买入/已卖出终态不动。
ARCHIVE_DAYS = 7
# 终态：归档不回写。
TERMINAL_STATUS = {"已买入", "已卖出"}

SOURCE_ALL_A = "全A低波"
SOURCE_MULTI = "多因子低波"
SOURCE_ML = "ML封单"


# ---------------------------------------------------------------------------
# 纯解析 / 聚合 / 归档判定（不触网，供单测）
# ---------------------------------------------------------------------------

def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _num(value) -> float | None:
    """剥离 markdown 加粗 / 正负号 / 百分号 / 占位符，转 float；不可解析返回 None。"""
    if value is None:
        return None
    t = str(value).replace("**", "").replace("+", "").replace("%", "").strip()
    if t in ("", "—", "-", "nan", "None"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _row_code(cells: list[str]) -> str:
    for c in cells:
        m = re.fullmatch(r"(\d{6})", c)
        if m:
            return m.group(1)
    return ""


def parse_all_a(text: str) -> list[dict]:
    """解析全A低波 md：列序 排名|代码|名称|收盘|总分|得分明细。"""
    out: list[dict] = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _cells(line)
        code = _row_code(cells)
        if not code:
            continue
        # 表头 / 分隔行跳过
        if "代码" in cells or set("".join(cells)) <= set("-: "):
            continue
        if len(cells) < 6:
            continue
        out.append({
            "code": code, "name": cells[2], "close": _num(cells[3]),
            "score": _num(cells[4]), "detail": cells[5], "source": SOURCE_ALL_A,
        })
    return out


def parse_multifactor(text: str) -> list[dict]:
    """解析多因子 md：列序 排名|代码|名称|收盘|低波|质量价值|总分|PE|ROE|净利率|附加标签。"""
    out: list[dict] = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _cells(line)
        code = _row_code(cells)
        if not code or "代码" in cells or len(cells) < 11:
            continue
        lowvol = cells[4]
        tags = cells[10]
        detail = "; ".join(x for x in [f"低波 {lowvol}", tags] if x and x != "—")
        out.append({
            "code": code, "name": cells[2], "close": _num(cells[3]),
            "score": _num(cells[6]), "detail": detail, "source": SOURCE_MULTI,
        })
    return out


def parse_ml_json(data: dict) -> list[dict]:
    """解析 ML 封单结果：top10 [{code,name,prob,seal_strength,industry}]。"""
    out: list[dict] = []
    for i, s in enumerate(data.get("top10") or [], 1):
        code = str(s.get("code", ""))
        if not re.fullmatch(r"\d{6}", code):
            continue
        prob = _num(s.get("prob"))
        prob_pct = round(prob * 100, 1) if prob is not None else None
        detail = f"封单强度={s.get('seal_strength', 0):.1f}; 行业={s.get('industry', '')}"
        out.append({
            "code": code, "name": s.get("name", ""), "close": None,
            "score": prob_pct, "detail": detail, "source": SOURCE_ML, "rank": i,
        })
    return out


def merge_candidates(items: list[dict]) -> list[dict]:
    """按代码合并多来源：来源标签取并集，分数取较高者，名称/收盘价补齐。"""
    by_code: dict[str, dict] = {}
    for it in items:
        code = it["code"]
        if code not in by_code:
            by_code[code] = {
                "code": code, "name": it.get("name", ""), "close": it.get("close"),
                "score": it.get("score"), "detail": it.get("detail", ""),
                "sources": [it["source"]], "rank": it.get("rank"),
            }
            continue
        cur = by_code[code]
        if it["source"] not in cur["sources"]:
            cur["sources"].append(it["source"])
        if not cur["name"] and it.get("name"):
            cur["name"] = it["name"]
        if cur["close"] is None and it.get("close") is not None:
            cur["close"] = it["close"]
        if it.get("score") is not None and (cur["score"] is None or it["score"] > cur["score"]):
            cur["score"] = it["score"]
            cur["detail"] = it.get("detail", cur["detail"])
    for cur in by_code.values():
        cur["source"] = "/".join(cur["sources"])
    return list(by_code.values())


def _iso_date(value) -> str | None:
    """读回日期（毫秒时间戳 / ISO 字符串）归一为 YYYY-MM-DD。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value)[:10]
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def archive_plan(existing: list[dict], candidate_codes: set[str],
                 today: datetime, archive_days: int = ARCHIVE_DAYS) -> list[dict]:
    """返回需归档（置“已剔除”）的记录：旧“候选”且本轮未入选、日期超期；终态不动。

    existing 元素：{record_id, code, status(单选项名), date(可读回值)}。
    """
    cutoff = today - timedelta(days=archive_days)
    plan: list[dict] = []
    for rec in existing:
        status = rec.get("status")
        if status in TERMINAL_STATUS:
            continue
        if status != "候选":
            continue
        if rec["code"] in candidate_codes:
            continue
        d = _iso_date(rec.get("date"))
        if d is None:
            # 日期缺失的陈旧候选也归档，避免脏数据长期滞留
            plan.append(rec)
            continue
        if datetime.strptime(d, "%Y-%m-%d") < cutoff:
            plan.append(rec)
    return plan


def build_fields(cand: dict, date_str: str) -> dict:
    """构造一条候选记录字段；数字缺值不传，避免数字字段类型转换失败。"""
    fields: dict = {
        "代码": cand["code"],
        "名称": cand["name"],
        "得分明细": cand.get("detail", ""),
        "来源": cand["source"],
        "扫描日期": date_str,
        "状态": ["候选"],
    }
    if cand.get("rank") is not None:
        fields["排名"] = cand["rank"]
    if cand.get("score") is not None:
        fields["总分"] = round(float(cand["score"]), 1)
    if cand.get("close") is not None:
        fields["收盘价"] = round(float(cand["close"]), 2)
    return fields


# ---------------------------------------------------------------------------
# 产物读取
# ---------------------------------------------------------------------------

def _latest_md(ws: Path, dirname: str, prefix: str) -> str | None:
    d = ws / dirname
    if not d.exists():
        return None
    files = sorted(d.glob(f"{prefix}_*.md"), reverse=True)
    if not files:
        return None
    try:
        return files[0].read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def collect(ws: str | Path) -> list[dict]:
    """汇总三源候选并按代码合并；某源产物缺失仅跳过，不中断。"""
    w = Path(ws)
    items: list[dict] = []

    all_a = _latest_md(w, "all_a_output", "all_a")
    if all_a:
        items.extend(parse_all_a(all_a))

    multi = _latest_md(w, "multifactor_output", "multifactor")
    if multi:
        items.extend(parse_multifactor(multi))

    ml_path = w / "ml_seal_strength.json"
    if ml_path.exists():
        data = _load_json(str(ml_path), {})
        items.extend(parse_ml_json(data))

    return merge_candidates(items)


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
               "status": status, "date": f.get("扫描日期")}
        prev = out.get(code)
        if prev is None or (rec["date"] or "") > (prev["date"] or ""):
            out[code] = rec
    return list(out.values())


def push(ws: str | Path = "/srv/anengos/workspace") -> dict:
    cands = collect(ws)
    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d %H:%M:%S")

    cred = _credentials()
    token = get_token(cred)

    items = _list_all(token)
    existing = _existing_map(items)
    existing_by_code = {r["code"]: r for r in existing}
    candidate_codes = {c["code"] for c in cands}

    to_create: list[dict] = []
    to_update: dict[str, dict] = {}
    for cand in cands:
        fields = build_fields(cand, date_str)
        prev = existing_by_code.get(cand["code"])
        if prev:
            to_update[prev["record_id"]] = fields
        else:
            to_create.append({"fields": fields})

    base = "https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records" % (
        BASE_TOKEN, TABLE_ID)

    created = updated = archived = 0
    if to_create:
        s, r = request_with_retry("POST", base + "/batch_create",
                                  {"records": to_create}, token)
        if s == 200 and r.get("code") == 0:
            created = len(to_create)
        else:
            print("CREATE_FAIL", s, r)

    if to_update:
        records = [{"record_id": rid, "fields": f} for rid, f in to_update.items()]
        s, r = request_with_retry("POST", base + "/batch_update",
                                  {"records": records}, token)
        if s == 200 and r.get("code") == 0:
            updated = len(to_update)
        else:
            print("UPDATE_FAIL", s, r)

    archived_plan = archive_plan(existing, candidate_codes, today)
    if archived_plan:
        records = [{"record_id": r["record_id"], "fields": {"状态": ["已剔除"]}}
                   for r in archived_plan]
        s, r = request_with_retry("POST", base + "/batch_update",
                                  {"records": records}, token)
        if s == 200 and r.get("code") == 0:
            archived = len(archived_plan)
        else:
            print("ARCHIVE_FAIL", s, r)

    summary = {"candidates": len(cands), "created": created,
               "updated": updated, "archived": archived}
    print("PUSH_CANDIDATES %s" % json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    push()
