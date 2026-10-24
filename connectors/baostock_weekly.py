# -*- coding: utf-8 -*-
"""baostock 周日扫描入口：cron 调用，跑价值族 + 低流动性月频信号，落盘 json + 周报。

用法（容器内）：
  docker exec -e PYTHONPATH=/srv/anengos anengos python /srv/anengos/connectors/baostock_weekly.py
"""
import sys, json, datetime, time
sys.path.insert(0, "/srv/anengos")
from pathlib import Path
import numpy as np
import pandas as pd
import baostock as bs
from connectors.baostock_screen import scan_value
from connectors.baostock_data import code_to_bs

WS = Path("/srv/anengos/workspace")
REBAL = 20
MAX_HOLD = 5
# 月频信号：amt20(低成交额) / vr5(低量比)，均为低者好
LIQ_FACTORS = ["amt20", "vr5"]


def scan_liquidity(codes: list[str], start: str = "2023-01-01", end: str = "2025-10-31") -> dict:
    """月频低流动性信号：拉 close/volume/amount，回测 + 输出最新一期持仓清单。"""
    panel = {}
    lg = bs.login()
    if lg.error_code != "0":
        return {"error": f"login failed: {lg.error_msg}"}
    try:
        for code in codes:
            try:
                rs = bs.query_history_k_data_plus(
                    code_to_bs(code), "date,close,volume,amount",
                    start_date=start, end_date=end, frequency="d", adjustflag="2")
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                df = pd.DataFrame(rows, columns=["date", "close", "volume", "amount"])
                for c in ("close", "volume", "amount"):
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                df = df.set_index("date")
                df["amt20"] = df["amount"].rolling(20).mean()
                df["vr5"] = df["volume"].rolling(5).mean() / df["volume"].rolling(20).mean()
                panel[code] = df
            except Exception:
                continue
            time.sleep(0.05)
    finally:
        bs.logout()

    alld = sorted(set().union(*[d.index for d in panel.values()]))
    rebal = alld[::REBAL]
    latest_date = rebal[-1]
    out = {"latest_date": latest_date, "signals": {}}
    for f in LIQ_FACTORS:
        snap = []
        for code, df in panel.items():
            v = df[f].get(latest_date)
            if v is None or pd.isna(v) or v <= 0:
                continue
            snap.append((code, round(float(v), 4)))
        snap.sort(key=lambda x: x[1])  # 低者好
        picks = [{"code": c, "value": v} for c, v in snap[:MAX_HOLD]]
        out["signals"][f] = {"holdings": picks, "universe": len(snap)}
    return out


def main() -> None:
    cache = json.loads((WS / "day_cache_000905.json").read_text(encoding="utf-8"))
    allc = [c for c in cache["hist"] if c[0] in ("6", "0", "3") and not c.startswith("688")]
    codes = allc[:250]

    value_results = scan_value(allc[:150])
    liq = scan_liquidity(codes)

    out = {"run_at": datetime.datetime.now().isoformat(timespec="seconds"),
           "value": value_results, "liquidity": liq}
    (WS / "baostock_scan.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    y, w, _ = datetime.date.today().isocalendar()
    out_dir = WS / "factor_batch_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"## baostock 周扫 {y}W{w:02d}", "", "### 价值族", ""]
    for r in value_results:
        if "error" in r:
            lines.append(f"- `{r['factor']}` 失败：{r['error']}")
            continue
        tag = "✅" if (r.get("l2") and r.get("l5")) else "❌"
        lines.append(f"- {tag} `{r['factor']}` IC {r['mean_ic']} | L2={r.get('l2')} L5={r.get('l5')}")
    lines += ["", f"### 低流动性月频信号（最新调仓日 {liq.get('latest_date')}）", ""]
    for f, sig in liq.get("signals", {}).items():
        hs = ", ".join([f"{h['code']}({h['value']})" for h in sig["holdings"]])
        lines.append(f"- `{f}` 候选({sig['universe']}只): {hs}")
    (out_dir / f"baostock_{y}W{w:02d}.md").write_text("\n".join(lines), encoding="utf-8")
    print("BS_SCAN_DONE value=%d liq_signals=%d" %
          (len(value_results), len(liq.get("signals", {}))))


if __name__ == "__main__":
    main()
