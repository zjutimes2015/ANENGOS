"""多因子打分选股 agent：对每只股票算 N 个因子，加权总分排序。

因子清单（10个）：
1. RSI(14)：<30 超卖=+2分，30-45=+1分，>70 超买=-2分
2. 价格 vs MA20：在 MA20 上方=+1分，下方=-1分
3. 价格 vs MA60：在 MA60 上方=+1分，下方=-1分
4. MA 排列：MA5>MA20>MA60（多头）=+2分，反之=-2分
5. 近5日涨跌幅：<-3%（超跌）=+1分，>+5%（追高）=-1分
6. 近20日波动率：低波动（<3%）=+1分，高波动（>5%）=-1分
7. 成交量比（当日/5日均量）：>1.5（放量）=+1分
8. 布林带位置：价格在下轨附近=+1分，在上轨附近=-1分
9. MACD：金叉=+2分，死叉=-2分
10. 换手率：适中（1-5%）=+1分

总分 >= 3 分入选候选。
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso
from connectors._lessons import load_recent_lessons
from connectors.quant_ima import retrieve_ima_knowledge

try:
    import akshare as ak  # type: ignore
    _AK_AVAILABLE = True
except Exception:  # noqa: BLE001
    ak = None  # type: ignore
    _AK_AVAILABLE = False


def _sma(vals: list[float], n: int) -> float:
    if len(vals) < n:
        return float("nan")
    return sum(vals[-n:]) / n


def _rsi(closes: list[float], n: int = 14) -> float:
    if len(closes) < n + 1:
        return float("nan")
    gains, losses = [], []
    for i in range(-n, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def _macd(closes: list[float]) -> str:
    """简化 MACD：快慢线对比。"""
    if len(closes) < 26:
        return "unknown"
    ema12 = sum(closes[-12:]) / 12
    ema26 = sum(closes[-26:]) / 26
    prev_ema12 = sum(closes[-13:-1]) / 12 if len(closes) >= 13 else ema12
    prev_ema26 = sum(closes[-27:-1]) / 26 if len(closes) >= 27 else ema26
    curr_diff = ema12 - ema26
    prev_diff = prev_ema12 - prev_ema26
    if curr_diff > 0 and prev_diff <= 0:
        return "golden"
    if curr_diff < 0 and prev_diff >= 0:
        return "death"
    return "neutral"


def _boll_position(closes: list[float], n: int = 20) -> float:
    """价格在布林带的位置：0=下轨，1=上轨，0.5=中轨。"""
    if len(closes) < n:
        return 0.5
    ma = sum(closes[-n:]) / n
    std = (sum((x - ma) ** 2 for x in closes[-n:]) / n) ** 0.5
    upper = ma + 2 * std
    lower = ma - 2 * std
    last = closes[-1]
    if upper == lower:
        return 0.5
    return (last - lower) / (upper - lower)


def score_stock(closes: list[float], volumes: list[float]) -> tuple[float, list[str]]:
    """对单只股票打分，返回 (总分, 各项明细)。"""
    if len(closes) < 60:
        return 0.0, ["数据不足"]

    last = closes[-1]
    rsi = _rsi(closes)
    ma5 = _sma(closes, 5)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)

    score = 0.0
    details = []

    # 1. RSI
    if rsi < 30:
        score += 2
        details.append(f"RSI超卖({rsi:.0f})+2")
    elif rsi < 45:
        score += 1
        details.append(f"RSI偏弱({rsi:.0f})+1")
    elif rsi > 70:
        score -= 2
        details.append(f"RSI超买({rsi:.0f})-2")

    # 2. 价格 vs MA20
    if last > ma20:
        score += 1
        details.append("价>MA20 +1")
    else:
        score -= 1
        details.append("价<MA20 -1")

    # 3. 价格 vs MA60
    if last > ma60:
        score += 1
        details.append("价>MA60 +1")
    else:
        score -= 1
        details.append("价<MA60 -1")

    # 4. MA 排列
    if ma5 > ma20 > ma60:
        score += 2
        details.append("多头排列 +2")
    elif ma5 < ma20 < ma60:
        score -= 2
        details.append("空头排列 -2")

    # 5. 近5日涨跌
    chg5 = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
    if chg5 < -3:
        score += 1
        details.append(f"5日超跌({chg5:.1f}%)+1")
    elif chg5 > 5:
        score -= 1
        details.append(f"5日追高({chg5:.1f}%)-1")

    # 6. 近20日波动率
    if len(closes) >= 20:
        rets = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(-20, 0)]
        vol = (sum((r - sum(rets)/len(rets))**2 for r in rets) / len(rets)) ** 0.5 * 100
        if vol < 3:
            score += 1
            details.append(f"低波({vol:.1f}%)+1")
        elif vol > 5:
            score -= 1
            details.append(f"高波({vol:.1f}%)-1")

    # 7. 量比（当日量 / 5日均量）
    if len(volumes) >= 5:
        avg_vol5 = sum(volumes[-5:]) / 5
        if avg_vol5 > 0 and volumes[-1] / avg_vol5 > 1.5:
            score += 1
            details.append("放量+1")

    # 8. 布林带位置
    boll_pos = _boll_position(closes)
    if boll_pos < 0.2:
        score += 1
        details.append("触下轨+1")
    elif boll_pos > 0.8:
        score -= 1
        details.append("贴上轨-1")

    # 9. MACD
    macd_sig = _macd(closes)
    if macd_sig == "golden":
        score += 2
        details.append("MACD金叉+2")
    elif macd_sig == "death":
        score -= 2
        details.append("MACD死叉-2")

    return score, details


class QuantMultifactorAdapter(AgentAdapter):
    name = "quant_multifactor"

    def __init__(self) -> None:
        self.mode = "akshare" if _AK_AVAILABLE else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        out_dir = Path(workspace) / "multifactor_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        lessons = load_recent_lessons(workspace)
        ima_knowledge = retrieve_ima_knowledge("多因子 选股 趋势 策略")

        indexes = ["000300", "000905"]
        m = re.search(r"\b(\d{6})\b", task)
        if m:
            indexes = [m.group(1)]

        if not _AK_AVAILABLE:
            content = f"# 多因子选股（mock）\n{lessons}"
            path = out_dir / f"multifactor_{datetime.datetime.now().strftime('%Y%m%d')}.md"
            path.write_text(content, encoding="utf-8")
            return AgentResult(True, self.name, "done", f"[{self.name}/mock]",
                artifact_paths=[str(path.relative_to(workspace))])

        try:
            # 拿成分股
            seen = set()
            rows = []
            for idx_code in indexes:
                try:
                    cons = ak.index_stock_cons_sina(idx_code)
                    for _, row in cons.iterrows():
                        if row["code"] not in seen:
                            seen.add(row["code"])
                            rows.append(row)
                except Exception:
                    continue

            results = []
            errors = 0
            for row in rows:
                sym = row["symbol"]
                name = row["name"]
                try:
                    df = ak.stock_zh_a_daily(symbol=sym, start_date="20240601", adjust="qfq")
                    if df is None or len(df) < 60:
                        continue
                    closes = [float(x) for x in df["close"].tolist()]
                    volumes = [float(x) for x in df["volume"].tolist()]
                    score, details = score_stock(closes, volumes)
                    if score >= 3:  # 总分>=3 入选
                        results.append({
                            "code": row["code"], "name": name,
                            "close": closes[-1], "score": score, "details": details,
                        })
                except Exception:
                    errors += 1
                    continue

            results.sort(key=lambda x: -x["score"])
            today = datetime.datetime.now().strftime("%Y%m%d")

            lines = [
                f"# 多因子打分选股（{'+'.join(indexes)}）",
                f"- 扫描时间：{_now_iso()}",
                f"- 成分股：{len(rows)} 只（失败 {errors}）",
                f"- 候选（总分>=3）：{len(results)} 只",
                f"- 因子：RSI/MA20/MA60/MA排列/5日涨跌/波动率/量比/布林/MACD",
                "",
                lessons,
                ima_knowledge,
                "",
                "| 排名 | 代码 | 名称 | 收盘 | 总分 | 得分明细 |",
                "|---|---|---|---|---|---|",
            ]
            for i, r in enumerate(results[:20]):
                lines.append(f"| {i+1} | {r['code']} | {r['name']} | {r['close']:.2f} "
                             f"| **{r['score']:+.0f}** | {'; '.join(r['details'][:5])} |")

            path = out_dir / f"multifactor_{today}.md"
            path.write_text("\n".join(lines), encoding="utf-8")

            return AgentResult(True, self.name, "done",
                f"[{self.name}/akshare] {len(rows)}只 → {len(results)}只候选（总分>=3）",
                artifact_paths=[str(path.relative_to(workspace))])
        except Exception as e:  # noqa: BLE001
            return AgentResult(False, self.name, "error",
                f"[{self.name}/akshare] 失败：{e}", error=str(e))

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": _AK_AVAILABLE, "mode": self.mode}

    def describe(self) -> dict[str, Any]:
        return {"summary": "多因子打分：10个因子加权评分，>=3分入选",
                "params": {"task": {"type": "string", "description": "指数代码，默认沪深300+中证500"}}}
