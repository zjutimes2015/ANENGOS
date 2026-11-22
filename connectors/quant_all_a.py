"""全A股多因子选股（5000+只）。

取数优先级：
1. 离线本地行情库（/srv/anengos/data/a_stock_daily.db，tushare 增量落库）：
   逐只读最近 60 根日线，本地算低波打分，不逐只走在线接口，收盘后可稳定跑批。
2. 在线 akshare 兜底：本地库缺失/为空时回退 stock_zh_a_spot 快照 + 逐只日线。

打分口径（B 重构后）：仅保留时序检验有效的 20 日低波动单因子（满分 +2），
入选线对齐 ``score > 0``（旧十因子时代的 ``score >= 3`` 与现行打分脱钩，会恒出 0 只）。
Top 30 输出到 all_a_output/all_a_<行情日>.md。

cron：交易日 16:50（本地行情库 16:40 增量落库之后），产物供次日虚拟盘读取。
"""

from __future__ import annotations

import datetime
import re
import sqlite3
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso
from connectors._lessons import load_recent_lessons
from connectors.quant_ima import retrieve_ima_knowledge
from connectors.quant_multifactor import score_stock

LOCAL_DB = "/srv/anengos/data/a_stock_daily.db"
MIN_BARS = 60
PRICE_FLOOR = 2.0
PRICE_CEIL = 200.0
TOP_N = 30
# B 重构后低波打分满分仅 +2，入选线 >0（对齐 multifactor 观察清单口径）。
SCORE_THRESHOLD = 0.0

try:
    import akshare as ak  # type: ignore
    _AK_AVAILABLE = True
except Exception:  # noqa: BLE001
    ak = None  # type: ignore
    _AK_AVAILABLE = False


def _strip_code(full: str) -> str:
    """本地库 code 形如 ``sh.600000``/``sz.000001``，取裸 6 位；北交所/非法返回空串。"""
    full = str(full or "")
    if full.startswith("bj.") or full.startswith("bj"):
        return ""
    bare = full.split(".")[-1]
    return bare if re.fullmatch(r"\d{6}", bare) else ""


def _load_local_universe(db_path: str) -> list[dict]:
    """从本地行情库逐只读最近 MIN_BARS 根日线，构造离线打分序列。

    返回 [{code, closes, volumes, last_date, close}]；缺行情/不足根数的票跳过。
    北交所代码在取数层即排除（本地增量池仅沪深 A 股）。
    """
    db = Path(db_path)
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        codes = [r[0] for r in conn.execute("SELECT DISTINCT code FROM daily")]
        universe: list[dict] = []
        for full in codes:
            code = _strip_code(full)
            if not code:
                continue
            rows = conn.execute(
                "SELECT date, close, volume FROM daily WHERE code = ? "
                "ORDER BY date DESC LIMIT ?",
                (full, MIN_BARS),
            ).fetchall()
            if not rows or len(rows) < MIN_BARS:
                continue
            rows.reverse()
            clean = [(str(r[0])[:10], float(r[1]), float(r[2] or 0))
                     for r in rows if r[1] is not None]
            if len(clean) < MIN_BARS:
                continue
            closes = [x[1] for x in clean]
            volumes = [x[2] for x in clean]
            close = closes[-1]
            if close < PRICE_FLOOR or close > PRICE_CEIL:
                continue
            universe.append({
                "code": code, "closes": closes, "volumes": volumes,
                "last_date": clean[-1][0], "close": close,
            })
        return universe
    finally:
        conn.close()


def score_universe(universe: list[dict], top_n: int = TOP_N) -> list[dict]:
    """对离线/在线统一的序列打分，按低波得分降序取 score>0 的 TopN（纯函数，可测试）。"""
    results: list[dict] = []
    for c in universe:
        score, details = score_stock(c["closes"], c["volumes"])
        if score <= SCORE_THRESHOLD:
            continue
        results.append({
            "code": c["code"], "name": c.get("name") or c["code"],
            "close": c["close"], "score": score, "details": details,
        })
    results.sort(key=lambda x: -x["score"])
    return results[:top_n]


def _online_name_map() -> dict[str, str]:
    """在线快照补名称（键=裸 6 位代码）；不可用/失败返回空 dict。"""
    if not _AK_AVAILABLE:
        return {}
    try:
        spot = ak.stock_zh_a_spot()
        names: dict[str, str] = {}
        for _, row in spot.iterrows():
            code_raw = str(row.get("代码", ""))
            bare = re.fullmatch(r"(\d{6})", code_raw[-6:])
            if bare:
                names[bare.group(1)] = str(row.get("名称", ""))
        return names
    except Exception:  # noqa: BLE001
        return {}


class QuantAllAAdapter(AgentAdapter):
    name = "quant_all_a"

    def __init__(self) -> None:
        self.mode = "akshare" if _AK_AVAILABLE else "mock"

    def _scan_online(self, limit: int | None) -> tuple[list[dict], int, str, int]:
        """在线兜底：快照粗筛 + 逐只日线打分。返回 (results, scanned, last_date, errors)。"""
        print("拉全市场快照...")
        spot = ak.stock_zh_a_spot()
        total_spot = len(spot)
        print(f"全市场 {total_spot} 只")

        coarse = []
        for _, row in spot.iterrows():
            code_raw = str(row.get("代码", ""))
            name = str(row.get("名称", ""))
            price = float(row.get("最新价", 0) or 0)
            if not code_raw or len(code_raw) < 2:
                continue
            prefix, code = code_raw[:2], code_raw[2:]
            if prefix == "bj":  # 北交所：流动性低，跳过
                continue
            if "ST" in name or "st" in name:
                continue
            if price < PRICE_FLOOR or price > PRICE_CEIL:
                continue
            if price == 0:  # 停牌
                continue
            coarse.append({"code": code, "name": name,
                            "symbol": f"{prefix}{code}", "price": price})

        if limit:
            coarse = coarse[:limit]
        print(f"粗筛后 {len(coarse)} 只（limit={limit}）")

        universe: list[dict] = []
        errors = 0
        last_date = ""
        for i, c in enumerate(coarse):
            try:
                df = ak.stock_zh_a_daily(symbol=c["symbol"], start_date="20240601", adjust="qfq")
                if df is None or len(df) < MIN_BARS:
                    continue
                closes = [float(x) for x in df["close"].tolist()]
                volumes = [float(x) for x in df["volume"].tolist()]
                universe.append({"code": c["code"], "name": c["name"],
                                 "closes": closes, "volumes": volumes,
                                 "close": closes[-1], "last_date": str(df["date"].iloc[-1])[:10]})
                last_date = max(last_date, str(df["date"].iloc[-1])[:10])
            except Exception:  # noqa: BLE001
                errors += 1
                continue
            if (i + 1) % 200 == 0:
                print(f"进度 {i+1}/{len(coarse)}，候选 {len(universe)}")

        results = score_universe(universe, TOP_N)
        return results, total_spot, last_date, errors

    def submit(self, task: str, workspace: str) -> AgentResult:
        out_dir = Path(workspace) / "all_a_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        lessons = load_recent_lessons(workspace)
        ima_knowledge = retrieve_ima_knowledge("全A 选股 因子 趋势")

        limit = None
        m = re.search(r"limit[=:]?(\d+)", task or "")
        if m:
            limit = int(m.group(1))

        # 1) 优先离线本地行情库（收盘后增量落库，不逐只走在线接口）
        universe = _load_local_universe(LOCAL_DB)
        source = "local"
        errors = 0
        if universe:
            names = _online_name_map()
            for c in universe:
                c["name"] = names.get(c["code"], c["code"])
            if limit:
                universe = universe[:limit]
            results = score_universe(universe, TOP_N)
            scanned = len(universe)
            last_date = max(c["last_date"] for c in universe)
            print(f"[local] 本地库 {scanned} 只 → 候选 {len(results)}（行情日 {last_date}）")
        else:
            # 2) 本地库缺失/为空 → 在线 akshare 兜底
            source = "online"
            if not _AK_AVAILABLE:
                content = f"# 全A选股（mock）\n{lessons}"
                path = out_dir / f"all_a_{datetime.datetime.now().strftime('%Y%m%d')}.md"
                path.write_text(content, encoding="utf-8")
                return AgentResult(True, self.name, "done", f"[{self.name}/mock]",
                    artifact_paths=[str(path.relative_to(workspace))])
            try:
                results, scanned, last_date, errors = self._scan_online(limit)
            except Exception as e:  # noqa: BLE001
                return AgentResult(False, self.name, "error",
                    f"[{self.name}/online] 失败：{e}", error=str(e))

        report_day = last_date or datetime.datetime.now().strftime("%Y%m%d")
        report_day_compact = report_day.replace("-", "")

        lines = [
            f"# 全A股低波选股（{scanned}只扫描 → {len(results)}只候选）",
            f"- 扫描时间：{_now_iso()}",
            f"- 行情日：{report_day}",
            f"- 数据源：{'本地行情库(离线)' if source == 'local' else '在线 akshare 兜底'}",
            f"- 扫描通过：{scanned} 只（价格 2-200、日线≥{MIN_BARS} 根、非北交所）",
            f"- 精筛候选（低波得分>0，Top{TOP_N}）：{len(results)} 只" + (f"（在线失败 {errors}）" if errors else ""),
            "- 主因子：20日低波动异象（vol20，时序单因子检验 IC -0.0572，回测 ROBUST）",
            "",
            lessons,
            ima_knowledge,
            "",
            "| 排名 | 代码 | 名称 | 收盘 | 总分 | 得分明细 |",
            "|---|---|---|---|---|---|",
        ]
        for i, r in enumerate(results[:TOP_N]):
            lines.append(f"| {i+1} | {r['code']} | {r['name']} | {r['close']:.2f} "
                         f"| **{r['score']:+.0f}** | {'; '.join(r['details'][:5])} |")

        path = out_dir / f"all_a_{report_day_compact}.md"
        path.write_text("\n".join(lines), encoding="utf-8")

        return AgentResult(True, self.name, "done",
            f"[{self.name}/{source}] 全A {scanned}只 → {len(results)}只候选",
            artifact_paths=[str(path.relative_to(workspace))],
            meta={"source": source, "scanned": scanned, "candidates": len(results),
                  "last_date": report_day})

    def health(self) -> dict[str, Any]:
        local = Path(LOCAL_DB).exists()
        return {"name": self.name, "ready": local or _AK_AVAILABLE,
                "mode": "local" if local else ("akshare" if _AK_AVAILABLE else "mock"),
                "local_db": local}

    def describe(self) -> dict[str, Any]:
        return {"summary": "全A股低波选股，优先本地行情库离线打分，在线 akshare 兜底，Top30",
                "params": {"task": {"type": "string", "description": "留空跑全A；limit=N 调试限数"}}}
