# -*- coding: utf-8 -*-
import json, urllib.request, urllib.error, sys
from datetime import datetime

SECRETS = "/srv/anengos/workspace/.secrets/feishu_app.json"
POS = "/srv/anengos/workspace/paper_positions.json"
PENDING = "/srv/anengos/workspace/paper_pending_orders.json"
BASE_TOKEN = "W3cob77DEanyS8sibGCcg07SnWd"
TABLE_ID = "tblPShDuSvXeNuVZ"

def req(method, url, body=None, token=None):
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json; charset=utf-8")
    if token: r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

def get_token(sec):
    s, r = req("POST", "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
               {"app_id": sec["app_id"], "app_secret": sec["app_secret"]})
    return r["tenant_access_token"]

def _pending_text():
    """读次日挂单列表，生成复盘表挂单文本；无文件/空列表 → （无挂单）。"""
    try:
        orders = json.load(open(PENDING, encoding="utf-8"))
    except Exception:
        orders = []
    if not orders:
        return "（无挂单）"
    lines = []
    for o in sorted(orders, key=lambda x: x.get("gap_pct", 0)):
        lines.append("%s %s | 挂单%.2f | 止损%.2f | 距突破%.1f%% | %d股" % (
            o.get("code", ""), o.get("name", ""),
            o.get("entry_px", 0), o.get("stop_px", 0),
            o.get("gap_pct", 0), o.get("shares", 0)))
    return "\n".join(lines) if lines else "（无挂单）"

def push():
    sec = json.load(open(SECRETS))
    d = json.load(open(POS))
    token = get_token(sec)
    cash = d.get("cash", 0)
    positions = d.get("positions", [])
    hist = d.get("history", [])
    pos_value = sum(p.get("current_price",0)*p.get("shares",0) for p in positions)
    equity = cash + pos_value

    # 复盘日期取日历当天，与 equity_history 的净值日期对齐；
    # 行情日 last_market_date 仅用于过滤本次运行成交（buy/sell_date 记的是行情日）。
    review_date = datetime.now().strftime("%Y-%m-%d")
    mkt = d.get("last_market_date", review_date)

    eh = d.get("equity_history", [])
    INITIAL = 1000000.0
    cum_ret = round((equity-INITIAL)/INITIAL*100, 2)
    peak = INITIAL
    mdd = 0
    for e in eh:
        peak = max(peak, e["equity"])
        mdd = max(mdd, (peak-e["equity"])/peak*100)

    today_sells = [h for h in hist if h.get("sell_date") == mkt]
    today_buys = [h for h in hist if h.get("buy_date") == mkt and "sell_date" not in h]
    today_pnl = sum(h.get("pnl",0) for h in today_sells)

    hold_lines = []
    for p in positions:
        strat = p.get("strategy", "MA20月频")
        entry = p.get("esb_entry_px", p.get("buy_price",0))
        stop = p.get("stop", p.get("init_stop",0))
        shares = p["shares"]
        cost = shares * p.get("buy_price",0)
        pct = p.get("pnl_pct",0)
        if strat == "ersibang":
            reason = "二四棒突破"; stop_str = "止损%.2f"%stop if stop else "止损未设"
        else:
            reason = "月频信号"; stop_str = "MA20止损"
        hold_lines.append("%s %s | %s | 入场%.2f | %s | %d股(%.0f元) | %+.1f%%" % (
            p["code"], p["name"], reason, entry, stop_str, shares, cost, pct))
    hold_text = "\n".join(hold_lines) if hold_lines else "空仓"

    sell_lines = []
    for h in today_sells:
        sell_lines.append("%s %s | %.2f→%.2f | %d股 | %s | 盈亏%.0f元" % (
            h.get("code",""), h.get("name",""), h.get("buy_price",0),
            h.get("sell_price",0), h.get("shares",0), h.get("sell_reason",""), h.get("pnl",0)))
    sell_text = "\n".join(sell_lines) if sell_lines else "无卖出"

    pending_text = _pending_text()

    fields = {
        "日期": review_date, "总资产": round(equity,2), "现金": round(cash,2),
        "持仓市值": round(pos_value,2), "仓位%": round(pos_value/equity*100,1) if equity else 0,
        "今日买": len(today_buys), "今日卖": len(today_sells),
        "今日实现盈亏": round(today_pnl,2), "持仓数": len(positions),
        "累计收益率%": cum_ret, "最大回撤%": round(mdd,2),
        "持仓明细": hold_text, "今日卖出明细": sell_text,
        "次日挂单列表": pending_text,
    }

    url = "https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records?page_size=10" % (BASE_TOKEN, TABLE_ID)
    s, r = req("GET", url, token=token)
    for it in r.get("data",{}).get("items",[]):
        f = it["fields"]
        if f.get("日期") == review_date:
            u2 = "https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records/%s" % (BASE_TOKEN, TABLE_ID, it["record_id"])
            s2, r2 = req("PUT", u2, {"fields": fields}, token)
            print("UPDATED %s sells=%d pnl=%.0f" % (review_date, len(today_sells), today_pnl) if s2==200 and r2.get("code")==0 else "FAIL %s %s"%(s2,r2))
            return
    url = "https://open.feishu.cn/open-apis/bitable/v1/apps/%s/tables/%s/records" % (BASE_TOKEN, TABLE_ID)
    s, r = req("POST", url, {"fields": fields}, token)
    print("PUSHED %s equity=%.0f positions=%d sells=%d" % (review_date, equity, len(positions), len(today_sells)) if s==200 and r.get("code")==0 else "PUSH_FAIL %s %s"%(s,r))

if __name__ == "__main__":
    push()
