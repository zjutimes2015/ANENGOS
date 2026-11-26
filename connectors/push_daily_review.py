# -*- coding: utf-8 -*-
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

SECRETS = "/srv/anengos/workspace/.secrets/feishu_app.json"
POS = "/srv/anengos/workspace/paper_positions.json"
PENDING = "/srv/anengos/workspace/paper_pending_orders.json"
BASE_TOKEN = "W3cob77DEanyS8sibGCcg07SnWd"
TABLE_ID = "tblPShDuSvXeNuVZ"

INITIAL_CAPITAL = 1000000.0
# 飞书调用重试：仅 5xx 与网络层瞬时错误退避重试；4xx 与业务错误不重试
RETRYABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5


def _parse_json(payload):
    """响应体兜底解析：非 JSON 时回退空 dict，避免解析异常中断跑批。"""
    try:
        return json.loads(payload)
    except (ValueError, TypeError, AttributeError):
        return {}


def req(method, url, body=None, token=None):
    """发起一次 HTTP 请求；返回 (status, parsed_body)。网络层错误返回 (0, {})。"""
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, _parse_json(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, _parse_json(e.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {}


def request_with_retry(method, url, body=None, token=None):
    """对可重试的瞬时错误按线性退避重试；返回最终 (status, body)。"""
    last = (0, {})
    for attempt in range(MAX_RETRIES):
        s, r = req(method, url, body=body, token=token)
        if s not in RETRYABLE_STATUS and s != 0:
            return s, r
        last = (s, r)
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF * (attempt + 1))
    return last


def get_token(sec):
    s, r = request_with_retry(
        "POST", "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": sec["app_id"], "app_secret": sec["app_secret"]})
    if r.get("code") != 0:
        raise RuntimeError("get tenant token failed: %s %s" % (s, r))
    return r["tenant_access_token"]


def _load_json(path, default):
    """读本地 JSON；文件缺失/损坏时回退 default，不抛异常中断跑批。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _pending_text():
    """读次日挂单列表，生成复盘表挂单文本；无文件/空列表 → （无挂单）。"""
    orders = _load_json(PENDING, [])
    if not orders:
        return "（无挂单）"
    lines = []
    for o in sorted(orders, key=lambda x: x.get("gap_pct", 0)):
        lines.append("%s %s | 挂单%.2f | 止损%.2f | 距突破%.1f%% | %d股" % (
            o.get("code", ""), o.get("name", ""),
            o.get("entry_px", 0), o.get("stop_px", 0),
            o.get("gap_pct", 0), o.get("shares", 0)))
    return "\n".join(lines) if lines else "（无挂单）"


def _sell_day(h):
    """一笔卖出记录归属的行情日：优先 sell_quote_date，回退 sell_date。"""
    return str(h.get("sell_quote_date") or h.get("sell_date") or "")[:10]


def _buy_day(h):
    """一笔成交记录归属的行情日：优先 quote_date，回退 buy_date（用于 run_day 推断）。"""
    return str(h.get("quote_date") or h.get("buy_date") or "")[:10]


def _entry_day(h):
    """持仓/成交的实际买入日：只认 buy_date，不用每日刷新的 quote_date（估值日）。"""
    return str(h.get("buy_date") or "")[:10]


def _run_day(d, positions, hist, review_date):
    """本次运行所依据的行情交易日。

    优先台账 last_market_date（引擎每次跑批刷新到行情库最新交易日）；
    其缺失时回退持仓/成交记录里的最新行情日；再回退日历当天。
    空仓后历史成交行情日不会再推进基准，避免旧成交被逐日重复计入当日卖出。
    """
    mkt = str(d.get("last_market_date") or "")[:10]
    if mkt:
        return mkt
    days = [_buy_day(x) for x in positions] + [_sell_day(x) for x in hist]
    days = [x for x in days if x]
    return max(days) if days else review_date


def _split_trades(hist, run_day, positions=None):
    """按本次运行行情日切分当日卖出/买入。

    卖出只取已平仓历史；买入合并两部分：当日买入仍持有的持仓（positions，
    按 buy_date 归属），以及历史里当日买入且尚未平仓的笔。已卖出历史笔不计入买入。
    """
    positions = positions or []
    sells = [h for h in hist if _sell_day(h) == run_day]
    open_buys = [h for h in hist if _entry_day(h) == run_day and "sell_date" not in h]
    pos_buys = [p for p in positions if _entry_day(p) == run_day]
    return sells, open_buys + pos_buys


def _max_drawdown(eh, initial):
    """按净值序列算最大回撤；跳过缺 equity 字段的损坏条目。"""
    peak = initial
    mdd = 0.0
    for e in eh:
        equity = e.get("equity") if isinstance(e, dict) else None
        if equity is None:
            continue
        peak = max(peak, equity)
        if peak:
            mdd = max(mdd, (peak - equity) / peak * 100)
    return round(mdd, 2)


def _list_all_records(request, token):
    """分页拉取复盘表全部记录，避免单页 page_size 上限导致既有行漏匹配。"""
    items, page_token = [], None
    while True:
        url = ("https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records"
               "?page_size=100" % (BASE_TOKEN, TABLE_ID))
        if page_token:
            url += "&page_token=" + page_token
        _, r = request("GET", url, token=token)
        data = r.get("data", {}) if isinstance(r, dict) else {}
        items.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    return items


def _find_record(items, review_date):
    """按复盘日期匹配既有记录；未命中返回 None。"""
    for it in items:
        if it.get("fields", {}).get("日期") == review_date:
            return it
    return None


def push():
    sec = _load_json(SECRETS, {})
    d = _load_json(POS, {})
    token = get_token(sec)
    cash = d.get("cash", 0)
    positions = d.get("positions", []) or []
    hist = d.get("history", []) or []
    pos_value = sum(p.get("current_price", 0) * p.get("shares", 0) for p in positions)
    equity = cash + pos_value

    # 复盘日期取日历当天，与 equity_history 净值日期对齐；
    # 当日成交按本次运行行情日过滤（_run_day 优先台账 last_market_date）。
    review_date = datetime.now().strftime("%Y-%m-%d")
    run_day = _run_day(d, positions, hist, review_date)

    eh = d.get("equity_history", []) or []
    cum_ret = round((equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 2)
    mdd = _max_drawdown(eh, INITIAL_CAPITAL)

    today_sells, today_buys = _split_trades(hist, run_day, positions)
    today_pnl = sum(h.get("pnl", 0) for h in today_sells)

    hold_lines = []
    for p in positions:
        strat = p.get("strategy", "MA20月频")
        entry = p.get("esb_entry_px", p.get("buy_price", 0))
        stop = p.get("stop", p.get("init_stop", 0))
        shares = p.get("shares", 0)
        cost = shares * p.get("buy_price", 0)
        pct = p.get("pnl_pct", 0)
        if strat == "ersibang":
            reason = "二四棒突破"
            stop_str = "止损%.2f" % stop if stop else "止损未设"
        else:
            reason = "月频信号"
            stop_str = "MA20止损"
        hold_lines.append("%s %s | %s | 入场%.2f | %s | %d股(%.0f元) | %+.1f%%" % (
            p.get("code", ""), p.get("name", ""), reason, entry, stop_str, shares, cost, pct))
    hold_text = "\n".join(hold_lines) if hold_lines else "空仓"

    sell_lines = []
    for h in today_sells:
        sell_lines.append("%s %s | %.2f→%.2f | %d股 | %s | 盈亏%.0f元" % (
            h.get("code", ""), h.get("name", ""), h.get("buy_price", 0),
            h.get("sell_price", 0), h.get("shares", 0), h.get("sell_reason", ""), h.get("pnl", 0)))
    sell_text = "\n".join(sell_lines) if sell_lines else "无卖出"

    pending_text = _pending_text()

    fields = {
        "日期": review_date, "总资产": round(equity, 2), "现金": round(cash, 2),
        "持仓市值": round(pos_value, 2), "仓位%": round(pos_value / equity * 100, 1) if equity else 0,
        "今日买": len(today_buys), "今日卖": len(today_sells),
        "今日实现盈亏": round(today_pnl, 2), "持仓数": len(positions),
        "累计收益率%": cum_ret, "最大回撤%": mdd,
        "持仓明细": hold_text, "今日卖出明细": sell_text,
        "次日挂单列表": pending_text,
    }

    items = _list_all_records(request_with_retry, token)
    found = _find_record(items, review_date)
    if found:
        u2 = ("https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records/%s"
              % (BASE_TOKEN, TABLE_ID, found["record_id"]))
        s2, r2 = request_with_retry("PUT", u2, {"fields": fields}, token)
        ok = s2 == 200 and r2.get("code") == 0
        print("UPDATED %s buys=%d sells=%d pnl=%.0f" % (review_date, len(today_buys), len(today_sells), today_pnl)
              if ok else "FAIL %s %s" % (s2, r2))
        return

    url = "https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records" % (BASE_TOKEN, TABLE_ID)
    s, r = request_with_retry("POST", url, {"fields": fields}, token)
    ok = s == 200 and r.get("code") == 0
    print("PUSHED %s equity=%.0f positions=%d buys=%d sells=%d" % (review_date, equity, len(positions), len(today_buys), len(today_sells))
          if ok else "PUSH_FAIL %s %s" % (s, r))


if __name__ == "__main__":
    push()
