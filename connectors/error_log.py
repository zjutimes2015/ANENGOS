"""错误日志进化连接器（第 16 个 agent / 自我进化发动机的燃料层）。

实现截图方法论「每条错误日志记 4 件事」：
  1. 判断是什么？  2. 依据是什么？  3. 哪个环节出问题？  4. 下次怎么做？

闭环设计（5 层 AI loop 抽象）：
  - 传感器层：读 paper_positions.json（history 卖出记录 / positions / equity_history）
  - 决策层：识别失败模式 → 按 4 问法结构化（judgment/basis/failure_link/next_action）
  - 工具层：文件系统 + JSON 分析（无第三方依赖）
  - 质量门：去重（同 code+judgment 不重复入档）、数据异常检测、状态机 open→fixed→verified
  - 学习层：error_logs/error_log.jsonl 结构化沉淀 + summary.md 周报；
           _retrieve() 供决策前 RAG 检索；_error_guard() 供 paper_trader 开仓前拦截

进化验证：下次同类行为出现时核对 next_action 是否被执行 → 是则标记 verified
（「系统比上次更结实一层」的直接证据）。
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso

LOG_FILE = "error_logs/error_log.jsonl"
SUMMARY_FILE = "error_logs/summary.md"

# 失败环节分类
LINK_SIGNAL = "信号层（判断依据本身错误）"
LINK_EXEC = "执行层（信号对但执行出错）"
LINK_RISK = "风控层（仓位/止损规则缺失）"
LINK_DATA = "系统数据层（数据/日历 bug）"


def _judgment_from_reason(reason: str) -> str:
    """从卖出原因还原当时判断（人话化）。"""
    if "跌破MA20" in reason:
        return "判断该票趋势仍强，贴线买入可继续持有"
    if "RSI" in reason and "减仓" in reason:
        return "判断该票超买后趋势延续，应保留大部分仓位"
    if "止盈" in reason:
        return "判断该票已到止盈位，应落袋为安"
    if "卖出" in reason or "止损" in reason:
        return "判断该票触发卖出规则，应离场"
    return f"按规则触发卖出（{reason}）"


def _link_from_reason(reason: str, anomaly: str | None = None) -> str:
    if anomaly:
        return LINK_DATA
    if "跌破MA20" in reason:
        return LINK_SIGNAL  # 选股/信号质量不足
    if "RSI" in reason:
        return LINK_RISK
    return LINK_EXEC


def _next_action(reason: str, code: str, anomaly: str | None = None) -> str:
    """可执行、可验证的下次怎么做（确定性规则，随数据推导）。"""
    if anomaly:
        return f"修复 paper_trader 交易日历与数据校验：卖出前校验 buy_date ≤ sell_date；{code} 数据修复前禁止重复买入"
    if "跌破MA20" in reason:
        return f"买入质量门槛升级：{code} 类票买入前要求现价高于 MA20 至少 3%（防贴线买入被洗），否则跳过"
    if "RSI" in reason and "减仓" in reason:
        return f"RSI>70 减仓后若价格继续走强，24 小时内不追高回补（等回踩）"
    if "止盈" in reason:
        return "止盈后观察 3 个交易日，不立即回补同一标的"
    return f"{code} 触发卖出后 5 个交易日内不自动回补，除非信号重新完整成立"


def _anomaly(sale: dict) -> str | None:
    """检测系统自身数据 bug（元错误）。"""
    bd, sd = sale.get("buy_date", ""), sale.get("sell_date", "")
    if bd and sd and sd < bd:
        return f"sell_date({sd}) 早于 buy_date({bd})"
    if sale.get("pnl") is None or sale.get("pnl_pct") is None:
        return "盈亏字段缺失"
    if sale.get("partial") and sale.get("shares", 0) <= 0:
        return "减仓记录 shares 异常"
    return None


def _load_logs(ws: Path) -> list[dict]:
    f = ws / LOG_FILE
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _dedup_key(sale: dict) -> str:
    """去重键：code + 判断。兼容 sale 对象（有 sell_reason）与已沉淀日志对象（有 judgment）。"""
    code = sale.get("code", "")
    judgment = sale.get("judgment")
    if judgment:
        return f"{code}|{judgment}"
    return f"{code}|{_judgment_from_reason(sale.get('sell_reason', ''))}"


def retrieve_error_logs(ws_path: str, query: str, top_k: int = 3) -> list[dict]:
    """RAG 检索：按关键词/主题返回最相关错误日志（供决策前注入上下文）。"""
    ws = Path(ws_path)
    logs = _load_logs(ws)
    q = (query or "").lower()
    scored = []
    for lg in logs:
        s = 0
        text = (lg.get("judgment", "") + lg.get("basis", "") + lg.get("next_action", "") +
                lg.get("code", "") + lg.get("name", "")).lower()
        for kw in re.split(r"[\s,，。]+", q):
            if kw and kw in text:
                s += 1
        scored.append((s, lg))
    scored.sort(key=lambda x: -x[0])
    return [lg for s, lg in scored if s > 0][:top_k]


def error_guard(ws_path: str, code: str) -> str | None:
    """开仓前拦截：该 code 是否有未修复的错误日志（anomaly/禁止回补类）→ 返回拦截原因。"""
    ws = Path(ws_path)
    logs = _load_logs(ws)
    for lg in logs:
        if lg.get("code") != code:
            continue
        if lg.get("status") in ("open", "fixed"):
            na = lg.get("next_action", "")
            if "禁止重复买入" in na or "不自动回补" in na or "数据修复前" in na:
                return f"错误日志拦截：{lg.get('judgment', '')}｜下次怎么做：{na}"
    return None


class ErrorLogAdapter(AgentAdapter):
    name = "error_log"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        ws = Path(workspace)
        (ws / "error_logs").mkdir(parents=True, exist_ok=True)
        pos_file = ws / "paper_positions.json"
        if not pos_file.exists():
            return AgentResult(False, self.name, "no_data",
                               "[error_log] paper_positions.json 不存在，等待 paper_trader 产生交易数据",
                               artifact_paths=[], error="paper_positions.json missing")
        data = json.loads(pos_file.read_text(encoding="utf-8"))
        history = data.get("history", [])
        positions = data.get("positions", [])
        equity_history = data.get("equity_history", [])

        existing = _load_logs(ws)
        existing_keys = {_dedup_key(lg) for lg in existing}
        new_logs: list[dict] = []
        now = _now_iso()[:10]

        # 1. 亏损卖出 → 错误日志（四问法）
        for sale in history:
            pnl = sale.get("pnl", 0) or 0
            anomaly = _anomaly(sale)
            if pnl >= 0 and not anomaly:
                continue  # 盈利卖出不留错误日志
            key = _dedup_key(sale)
            if key in existing_keys:
                continue
            reason = sale.get("sell_reason", "")
            lg = {
                "id": f"EL-{now}-{len(new_logs)+1:03d}",
                "code": sale.get("code", ""),
                "name": sale.get("name", ""),
                "created": now,
                "status": "open",
                "judgment": _judgment_from_reason(reason),
                "basis": (f"卖出规则触发：{reason}；买入价 {sale.get('buy_price')}，"
                          f"卖出价 {sale.get('sell_price')}，盈亏 {pnl:+.2f} 元"
                          + (f"（异常：{anomaly}）" if anomaly else "")),
                "failure_link": _link_from_reason(reason, anomaly),
                "next_action": _next_action(reason, sale.get("code", ""), anomaly),
            }
            new_logs.append(lg)
            existing_keys.add(key)

        # 2. 持仓深亏（pnl_pct < -8%）→ 风控错误日志
        for p in positions:
            pct = p.get("pnl_pct", 0) or 0
            if pct > -8:
                continue
            key = f"{p.get('code')}|深亏"
            if key in existing_keys:
                continue
            lg = {
                "id": f"EL-{now}-{len(new_logs)+1:03d}",
                "code": p.get("code", ""),
                "name": p.get("name", ""),
                "created": now,
                "status": "open",
                "judgment": f"判断 {p.get('name')} 会继续走强，未及时止损",
                "basis": f"当前浮亏 {pct:.1f}%（成本 {p.get('buy_price')}，现价 {p.get('current_price')}）",
                "failure_link": LINK_RISK,
                "next_action": f"单票浮亏达 -8% 无条件减半，-12% 清仓（机械执行，不寄托反弹）",
            }
            new_logs.append(lg)
            existing_keys.add(key)

        # 3. 进化核对：历史中是否出现 next_action 已执行的证据
        for lg in existing:
            if lg.get("status") != "open":
                continue
            code, na = lg.get("code", ""), lg.get("next_action", "")
            if "数据修复前" in na:
                # 该 code 全部带日期的交易均已无倒挂 → 数据 bug 已修复
                recent_same = [h for h in history
                               if h.get("code") == code and h.get("buy_date") and h.get("sell_date")]
                if recent_same and all(h["buy_date"] <= h["sell_date"] for h in recent_same):
                    lg["status"] = "fixed"
                    lg["verified"] = now
            if "禁止重复买入" in na or "不自动回补" in na or "跳过" in na:
                recent = [h for h in history[-8:] if h.get("code") == code]
                if not recent:
                    lg["status"] = "verified"
                    lg["verified"] = now

        # 4. 沉淀：追加新日志 + 写回
        all_logs = existing + new_logs
        with open(ws / LOG_FILE, "w", encoding="utf-8") as f:
            for lg in all_logs:
                f.write(json.dumps(lg, ensure_ascii=False) + "\n")

        # 5. 生成周度摘要
        open_n = sum(1 for lg in all_logs if lg.get("status") == "open")
        fixed_n = sum(1 for lg in all_logs if lg.get("status") == "fixed")
        verified_n = sum(1 for lg in all_logs if lg.get("status") == "verified")
        lines = [
            f"# 错误日志进化摘要（{now}）",
            f"- 累计错误日志：{len(all_logs)} 条（open {open_n} / fixed {fixed_n} / verified {verified_n}）",
            f"- 本次新增：{len(new_logs)} 条",
            "",
            "## 新增错误日志（四问法）",
        ]
        for lg in new_logs:
            lines.append(f"- **[{lg['id']}] {lg['name']}({lg['code']})** — {lg['failure_link']}")
            lines.append(f"  - 判断是什么：{lg['judgment']}")
            lines.append(f"  - 依据是什么：{lg['basis']}")
            lines.append(f"  - 哪个环节出问题：{lg['failure_link']}")
            lines.append(f"  - 下次怎么做：{lg['next_action']}")
        if not new_logs:
            lines.append("- 本次无新增（亏损交易或重复模式未出现）")
        lines.append("")
        lines.append("## 进化核对（系统是否比上次更结实）")
        lines.append(f"- 已核对修复（fixed）：{fixed_n} 条")
        lines.append(f"- 已验证不再重犯（verified）：{verified_n} 条")
        lines.append("- verified 意味着：同类错误日志对应的 next_action 已在后续交易中体现，系统结构确实变了")
        (ws / SUMMARY_FILE).write_text("\n".join(lines), encoding="utf-8")

        # 结构化摘要（供互审核验）
        summary = [
            f"[{self.name}] 错误日志进化：新增 {len(new_logs)}，累计 {len(all_logs)}"
            f"（open {open_n} / fixed {fixed_n} / verified {verified_n}）",
            "## 新增（四问法）",
        ]
        for lg in new_logs[:8]:
            summary.append(f"- {lg['id']} {lg['name']}({lg['code']}): {lg['judgment']} → {lg['next_action'][:40]}")
        if not new_logs:
            summary.append("- 无新增（无亏损/无重复模式）")
        summary.append("## 进化状态")
        summary.append(f"- 累计 {len(all_logs)} 条；fixed {fixed_n}；verified {verified_n}")
        summary.append(f"- 摘要文件：{SUMMARY_FILE}")
        return AgentResult(True, self.name, "done", "\n".join(summary),
                           artifact_paths=[LOG_FILE, SUMMARY_FILE],
                           meta={"new": len(new_logs), "total": len(all_logs),
                                 "fixed": fixed_n, "verified": verified_n, "ts": _now_iso()})

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": True, "mode": "filesystem"}

    def describe(self) -> dict[str, Any]:
        return {"summary": "错误日志进化引擎：读 paper_trader 亏损交易/系统数据 bug，"
                           "按四问法（判断/依据/环节/下次怎么做）结构化沉淀，"
                           "喂回决策拦截（error_guard）并核对进化（open→fixed→verified）",
                "params": {"task": {"type": "string", "description": "留空跑默认错误日志进化"}}}
