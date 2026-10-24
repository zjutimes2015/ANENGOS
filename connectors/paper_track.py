# -*- coding: utf-8 -*-
"""富途真实行情盯盘：读 baostock 月频信号持仓，经 OpenD(127.0.0.1:11111) 查真实价，记虚拟盘净值。

用法（容器内，OpenD 在线时）：
  docker exec -e PYTHONPATH=/srv/anengos anengos python /srv/anengos/connectors/paper_track.py
OpenD 未在线时安全退出，不报错崩溃。
"""
import sys, json, datetime, traceback
sys.path.insert(0, "/srv/anengos")
from pathlib import Path

WS = Path("/srv/anengos/workspace")
PORTFOLIO = WS / "paper_track.json"


def to_futu_code(code: str) -> str:
    if code.startswith(("6", "9")):
        return f"SH.{code}"
    return f"SZ.{code}"


def main() -> None:
    scan = json.loads((WS / "baostock_scan.json").read_text(encoding="utf-8"))
    sigs = scan.get("liquidity", {}).get("signals", {})
    targets = {f: [h["code"] for h in v.get("holdings", [])] for f, v in sigs.items()}
    if not any(targets.values()):
        print("NO_SIGNAL"); return

    try:
        from futu import OpenQuoteContext
    except Exception as e:
        print("FUTU_SDK_MISSING", e); return

    qctx = None
    try:
        qctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        snapshot = {}
        for factor, codes in targets.items():
            fcodes = [to_futu_code(c) for c in codes]
            ret, data = qctx.get_market_snapshot(fcodes)
            if ret != 0:
                print("SNAP_FAIL", factor, data); continue
            for _, row in data.iterrows():
                snapshot[row["code"].replace("SH.", "").replace("SZ.", "")] = float(row["last_price"])
    except Exception as e:
        print("OPEND_OFFLINE", type(e).__name__, str(e)[:120]); return
    finally:
        if qctx is not None:
            try: qctx.close()
            except Exception: pass

    # 读历史，算净值
    hist = json.loads(PORTFOLIO.read_text(encoding="utf-8")) if PORTFOLIO.exists() else {"nav": {}, "series": []}
    today = datetime.date.today().isoformat()
    nav = hist.get("nav", {})
    row = {"date": today, "prices": snapshot}
    for factor, codes in targets.items():
        prices = {c: snapshot.get(c) for c in codes if snapshot.get(c)}
        if not prices:
            continue
        cur_avg = sum(prices.values()) / len(prices)
        if factor not in nav:
            nav[factor] = {"base": cur_avg, "codes": codes}
        base = nav[factor]["base"]
        row[f"{factor}_nav"] = round(cur_avg / base, 4)
    row["nav_state"] = nav
    hist["series"].append(row)
    hist["nav"] = nav
    hist["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    PORTFOLIO.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TRACK_DONE", today, {k: v for k, v in row.items() if k.endswith("_nav")})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        print("TRACK_ERROR")
