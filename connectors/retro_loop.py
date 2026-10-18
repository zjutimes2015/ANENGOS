"""复盘连接器（学习层 / outer improver skill）。

这是 Tom Blomfield 说的「自我进化发动机」——每周自动读 workspace 里所有
量化产出（技术分析 + 回测报告），找失败模式，沉淀为结构化知识。

按 5 层 AI loop 抽象：
  - 传感器层：读 workspace/quant_technical_output/ 和 quant_backtest_output/ 所有报告
  - 决策层：哪些失败要沉淀（OVERFITTED / 明显误判），哪些只是噪音
  - 工具层：文件系统扫描 + markdown 解析
  - 质量门：本周有没有「闭环」——有没有把失败写成可复用的教训
  - 学习层：生成 weekly_YYYYWW.md 知识文档，下周决策时作为 RAG 上下文
        —— 这就是「失败之后系统有没有变好」的实现

v2（2026-W43 起）：新增「策略改进项」机制——
  - 从 paper_trader 历史交易识别失败模式 → 生成带验收标准的改进项（IMP-xxx）
  - 写入 retro_loop_output/improvements.md，作为下周决策的可执行上下文
  - 下周复盘自动核对：改进项对应的卖出行为（止盈/减仓）是否已出现在历史交易中
      → pending 变 done。这就是「自我进化闭环」的直接证据。

没有这个，前面所有 connector 都是开环：分析/回测完了就完了，下周还会犯同样的错。
"""

from __future__ import annotations

import datetime
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso
from connectors.quant_ima import retrieve_ima_knowledge


class RetroLoopAdapter(AgentAdapter):
    name = "retro_loop"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        ws = Path(workspace)

        # 1. 扫描所有量化产出
        tech_files = list((ws / "quant_technical_output").glob("*.md")) if (ws / "quant_technical_output").exists() else []
        bt_files = list((ws / "quant_backtest_output").glob("*.md")) if (ws / "quant_backtest_output").exists() else []
        esb_files = list((ws / "quant_ersibang_output").glob("esb_*.md")) if (ws / "quant_ersibang_output").exists() else []

        # 2. 从回测报告里提取质量门判定
        gate_counter = Counter()
        overfit_files = []
        for f in bt_files:
            text = f.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"\*\*判定：(ROBUST|MODERATE|WEAK|OVERFITTED)\*\*", text)
            if m:
                gate = m.group(1)
                gate_counter[gate] += 1
                if gate == "OVERFITTED":
                    overfit_files.append(f.name)

        # 2.5 二四棒回测状态（最新一份报告的关键指标）
        esb_meta = self._esb_stats(esb_files)

        # 3. 读 paper_trader 绩效
        pos_file = ws / "paper_positions.json"
        perf = self._calc_perf(pos_file)
        history = self._load_history(pos_file)

        # 3.5 策略改进项检测：识别失败模式 → 生成可执行改进项（带验收标准）
        improvements = self._detect_improvements(perf, history, esb_meta)

        # 4. 生成本周复盘知识文档
        now = datetime.datetime.now(datetime.timezone.utc)
        year, week, _ = now.isocalendar()
        out_dir = ws / "retro_loop_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"weekly_{year}W{week:02d}.md"

        content = self._build_report(
            tech_count=len(tech_files),
            bt_count=len(bt_files),
            gate_counter=gate_counter,
            overfit_files=overfit_files,
            now=now,
            perf=perf,
            ima_knowledge=retrieve_ima_knowledge("量化 策略 交易 复盘"),
            improvements=improvements,
            esb_meta=esb_meta,
        )
        (out_dir / name).write_text(content, encoding="utf-8")

        # 4.5 策略改进项清单（下周复盘自动核对 pending → done）
        imp_path = out_dir / "improvements.md"
        imp_path.write_text(self._render_improvements(improvements, now), encoding="utf-8")

        closed = bool(gate_counter)  # 本周有没有跑过回测闭环
        status = "closed_loop" if closed else "open_loop"

        return AgentResult(
            True, self.name, "done",
            f"[retro_loop] 本周复盘 {status}：技术分析 {len(tech_files)} 份 / 回测 {len(bt_files)} 份 / 质量门 {dict(gate_counter)} / 改进项 {len(improvements)} 条",
            artifact_paths=[f"retro_loop_output/{name}", "retro_loop_output/improvements.md"],
            meta={
                "week": f"{year}W{week:02d}",
                "tech_count": len(tech_files),
                "bt_count": len(bt_files),
                "gate_distribution": dict(gate_counter),
                "overfit_count": len(overfit_files),
                "closed_loop": closed,
                "improvement_count": len(improvements),
                "pending_improvements": [i["id"] for i in improvements if i.get("status") == "pending"],
                "ts": _now_iso(),
            },
        )

    @staticmethod
    def _esb_stats(esb_files: list[Path]) -> dict:
        """从二四棒回测报告提取最新一份关键指标。"""
        if not esb_files:
            return {"count": 0}
        latest = sorted(esb_files, reverse=True)[0]
        text = latest.read_text(encoding="utf-8", errors="replace")
        meta: dict = {"count": len(esb_files), "file": latest.name}
        m = re.search(r"胜率: (\d+)/(\d+) \(([\d.]+)%\)", text)
        if m:
            meta["wins"] = int(m.group(1)); meta["closed"] = int(m.group(2))
            meta["win_rate"] = float(m.group(3))
        m = re.search(r"总盈亏: ([+-]?[\d,]+) 元", text)
        if m:
            meta["total_pnl"] = float(m.group(1).replace(",", ""))
        m = re.search(r"年化: ([+-]?[\d.]+)%", text)
        if m:
            meta["annual_pct"] = float(m.group(1))
        m = re.search(r"夏普比率: ([\d.]+)", text)
        if m:
            meta["sharpe"] = float(m.group(1))
        m = re.search(r"数据区间: (\S+) ~ (\S+)", text)
        if m:
            meta["date_start"] = m.group(1); meta["date_end"] = m.group(2)
        return meta

    @staticmethod
    def _load_history(pos_file: Path) -> list[dict]:
        """读 paper_positions.json 的已平仓历史。"""
        if not pos_file.exists():
            return []
        try:
            data = json.loads(pos_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data.get("history", [])
        except Exception:
            return []
        return []

    @staticmethod
    def _detect_improvements(perf: dict, history: list[dict],
                             esb_meta: dict | None = None) -> list[dict]:
        """从交易历史识别策略改进项。每项含 action（可执行）与 acceptance（可验证），
        pending → done 由下周复盘自动核对，构成自我进化闭环。"""
        items: list[dict] = []
        closed = perf.get("closed_trades", 0)
        if closed == 0:
            # 无交易也保留二四棒监控项（检查信号链路）
            if esb_meta and esb_meta.get("count", 0) > 0:
                items.append({
                    "id": "IMP-004", "title": "二四棒虚拟盘信号链路监控",
                    "why": "二四棒已注册且有回测报告，但虚拟盘尚无二四棒成交，需确认每日信号计算与下单是否在跑",
                    "action": "确认 quant_all_a 每日产出趋势池 + paper_trader 3b 段 esb_signal 正常触发",
                    "acceptance": "paper_positions.json history 出现 strategy=ersibang 成交",
                    "status": "pending",
                })
            return items

        reasons = [str(t.get("sell_reason", "")) for t in history]
        wins = perf.get("wins", 0)
        losses = perf.get("losses", 0)

        # IMP-001：止盈规则（对应 paper_trader.TAKE_PROFIT_PCT）
        if any("止盈" in r for r in reasons):
            items.append({
                "id": "IMP-001", "title": "卖出规则增加止盈",
                "why": "本周历史已出现「止盈」卖出，规则已落地",
                "action": "浮盈 ≥ +8% 自动止盈卖出（paper_trader.TAKE_PROFIT_PCT）",
                "acceptance": "历史交易 sell_reason 含「止盈」",
                "status": "done",
            })
        else:
            items.append({
                "id": "IMP-001", "title": "卖出规则增加止盈",
                "why": f"本周 {wins} 胜 {losses} 负，全部靠破位止损卖出，没有盈利落袋机制，浮盈无法兑现",
                "action": "paper_trader 浮盈 ≥ +8% 时自动止盈卖出",
                "acceptance": "历史交易 sell_reason 出现「止盈」",
                "status": "pending",
            })

        # IMP-002：RSI 超买减仓（对应 paper_trader 减仓分支）
        if any("RSI减仓" in r for r in reasons):
            items.append({
                "id": "IMP-002", "title": "RSI>70 超买减仓而非清仓",
                "why": "本周历史已出现「RSI减仓」卖出，规则已落地",
                "action": "RSI>70 时卖出一半持仓，保留剩余吃趋势延续",
                "acceptance": "历史交易 sell_reason 含「RSI减仓」",
                "status": "done",
            })
        else:
            items.append({
                "id": "IMP-002", "title": "RSI>70 超买减仓而非清仓",
                "why": "超买直接清仓会错失趋势延续，且与止盈规则重叠",
                "action": "paper_trader RSI>70 时卖出一半持仓",
                "acceptance": "历史交易 sell_reason 出现「RSI减仓」",
                "status": "pending",
            })

        # IMP-003：选股信号与保护线冲突（胜率健康检查）
        win_rate = perf.get("win_rate", 100)
        if win_rate < 30 and losses > 0:
            items.append({
                "id": "IMP-003", "title": "复核选股信号与 MA20 保护线冲突",
                "why": f"胜率 {win_rate:.1f}% < 30%，候选买入后普遍立即跌破 MA20，入场位与止损线距离过近",
                "action": "人工 review quant_multifactor 阈值，或给买入加 MA20 缓冲过滤",
                "acceptance": "人工确认处理，并在下周决策上下文标注",
                "status": "pending",
            })

        # IMP-004：二四棒策略虚拟盘监控
        esb_hist = [t for t in history if t.get("strategy") == "ersibang"]
        esb_wins = sum(1 for t in esb_hist if t.get("pnl", 0) > 0)
        esb_pnl = sum(t.get("pnl", 0) for t in esb_hist)
        if esb_hist:
            esb_rate = esb_wins / len(esb_hist) * 100
            items.append({
                "id": "IMP-004", "title": "二四棒虚拟盘运行监控",
                "why": f"本周二四棒已成交 {len(esb_hist)} 笔，胜率 {esb_rate:.1f}% / 累计 {esb_pnl:+,.0f}元"
                       f"{'，与回测胜率对比健康' if esb_meta and abs(esb_rate - esb_meta.get('win_rate', 50)) <= 20 else '，与回测胜率偏离较大需核查'}",
                "action": "对比回测胜率与虚拟盘胜率，核查信号/止损执行偏差；连续亏损则暂停该策略",
                "acceptance": "周报记录二四棒虚拟盘绩效且偏差在 ±20% 内",
                "status": "done",
            })
        elif esb_meta and esb_meta.get("count", 0) > 0:
            items.append({
                "id": "IMP-004", "title": "二四棒虚拟盘信号链路监控",
                "why": "二四棒已有回测报告，但虚拟盘尚无二四棒成交，需确认每日信号计算与下单是否在跑",
                "action": "确认 quant_all_a 每日产出趋势池 + paper_trader 3b 段 esb_signal 正常触发",
                "acceptance": "paper_positions.json history 出现 strategy=ersibang 成交",
                "status": "pending",
            })

        return items

    @staticmethod
    def _render_improvements(items: list[dict], now: datetime.datetime) -> str:
        """生成改进项清单文件（下周复盘核对状态用）。"""
        if not items:
            return f"# 策略改进项（{now.strftime('%Y-%m-%d')} UTC）\n\n（无已平仓交易，无改进项）\n"
        lines = [
            f"# 策略改进项（{now.strftime('%Y-%m-%d')} UTC）",
            "",
            "| ID | 标题 | 状态 | 行动 | 验收标准 |",
            "|---|---|---|---|---|",
        ]
        for i in items:
            lines.append(
                f"| {i['id']} | {i['title']} | {i['status']} | {i['action']} | {i['acceptance']} |"
            )
        lines += ["", "> pending → done 由下周 retro_loop 自动核对（检查历史交易中是否出现对应卖出行为）。"]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _calc_perf(pos_file: Path) -> dict:
        """从 paper_positions.json 算交易绩效。"""
        if not pos_file.exists():
            return {}
        try:
            data = json.loads(pos_file.read_text(encoding="utf-8"))
            positions = data.get("positions", []) if isinstance(data, dict) else data
            history = data.get("history", []) if isinstance(data, dict) else []
        except Exception:
            return {}

        if not history:
            return {"open_positions": len(positions), "closed_trades": 0}

        pnl_pcts = [t.get("pnl_pct", 0) for t in history if "pnl_pct" in t]
        wins = [p for p in pnl_pcts if p > 0]
        losses = [p for p in pnl_pcts if p < 0]

        sharpe = 0.0
        if len(pnl_pcts) > 1:
            mean = sum(pnl_pcts) / len(pnl_pcts)
            var = sum((p - mean) ** 2 for p in pnl_pcts) / (len(pnl_pcts) - 1)
            std = math.sqrt(var)
            if std > 0:
                sharpe = mean / std * math.sqrt(252)

        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnl_pcts:
            cum += p
            peak = max(peak, cum)
            dd = peak - cum
            max_dd = max(max_dd, dd)

        total_pnl = sum(t.get("pnl", 0) for t in history)
        # 二四棒分组统计（strategy=ersibang）
        esb_hist = [t for t in history if t.get("strategy") == "ersibang"]
        esb_wins = sum(1 for t in esb_hist if t.get("pnl", 0) > 0)
        esb_open = sum(1 for p in positions if p.get("strategy") == "ersibang")
        return {
            "open_positions": len(positions),
            "closed_trades": len(history),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(history) * 100 if history else 0,
            "avg_pnl_pct": sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0,
            "total_pnl": total_pnl,
            "sharpe": sharpe,
            "max_drawdown": max_dd,
            "esb_closed": len(esb_hist),
            "esb_open": esb_open,
            "esb_win_rate": esb_wins / len(esb_hist) * 100 if esb_hist else 0,
            "esb_pnl": sum(t.get("pnl", 0) for t in esb_hist),
        }

    @staticmethod
    def _build_report(tech_count: int, bt_count: int,
                      gate_counter: Counter, overfit_files: list[str],
                      now: datetime.datetime, perf: dict | None = None,
                      ima_knowledge: str = "",
                      improvements: list[dict] | None = None,
                      esb_meta: dict | None = None) -> str:
        perf = perf or {}
        esb_meta = esb_meta or {}
        lines = [
            f"# 周复盘知识（{now.strftime('%Y-%m-%d')} UTC）",
            "",
            "## 本周产出统计",
            f"- 技术分析报告：{tech_count} 份",
            f"- 回测报告：{bt_count} 份",
            f"- 二四棒回测报告：{esb_meta.get('count', 0)} 份",
            "",
        ]

        if esb_meta.get("count", 0) > 0 and "win_rate" in esb_meta:
            lines += [
                "## 二四棒策略状态（最新回测）",
                f"- 报告：`{esb_meta.get('file')}` | 区间 {esb_meta.get('date_start')} ~ {esb_meta.get('date_end')}",
                f"- 胜率：{esb_meta['win_rate']:.1f}% ({esb_meta.get('wins', 0)}/{esb_meta.get('closed', 0)})"
                f" | 总盈亏 {esb_meta.get('total_pnl', 0):+,.0f} 元 | 年化 {esb_meta.get('annual_pct', 0):+.2f}%",
                f"- 夏普：{esb_meta.get('sharpe', 0):.2f}",
                "",
            ]

        if perf and perf.get("closed_trades", 0) > 0:
            lines += [
                "## 虚拟盘绩效（paper_trader）",
                f"- 已平仓：{perf['closed_trades']} 笔 | 当前持仓：{perf['open_positions']} 只"
                f"（二四棒持仓 {perf.get('esb_open', 0)}）",
                f"- 胜率：{perf['win_rate']:.1f}%（{perf['wins']}胜/{perf['losses']}负）",
                f"- 平均每笔：{perf['avg_pnl_pct']:+.2f}% | 累计：{perf['total_pnl']:+.2f}元",
                f"- 夏普（年化简化）：{perf['sharpe']:.2f} | 最大回撤：{perf['max_drawdown']:.2f}%",
                "",
            ]
            if perf.get("esb_closed", 0) > 0:
                lines += [
                    f"- 二四棒已平仓：{perf['esb_closed']} 笔"
                    f"（胜率 {perf['esb_win_rate']:.1f}% / 盈亏 {perf['esb_pnl']:+,.0f} 元）",
                    "",
                ]

        lines += [
            "## 质量门分布（walk-forward 判定）",
            f"- ROBUST（可进虚拟盘）：{gate_counter.get('ROBUST', 0)}",
            f"- MODERATE（观察）：{gate_counter.get('MODERATE', 0)}",
            f"- WEAK（不推荐）：{gate_counter.get('WEAK', 0)}",
            f"- OVERFITTED（淘汰）：{gate_counter.get('OVERFITTED', 0)}",
            "",
        ]
        if overfit_files:
            lines += [
                "## ❌ 本周过拟合教训（必须沉淀）",
                "",
                "以下回测历史好看但样本外失效，下周决策时要避开相同参数/逻辑：",
            ]
            for f in overfit_files:
                lines.append(f"- `{f}`")
            lines.append("")
        else:
            lines += ["## 本周过拟合教训", "", "（无 OVERFITTED 回测）", ""]

        lines += [
            "## 策略改进项（本周）",
            "",
        ]
        if improvements:
            for i in improvements:
                mark = "✅" if i["status"] == "done" else "⬜"
                lines += [
                    f"- {mark} **{i['id']}** {i['title']}（{i['status']}）",
                    f"  - 为什么：{i['why']}",
                    f"  - 行动：{i['action']}",
                    f"  - 验收：{i['acceptance']}",
                ]
            lines.append("")
            lines.append("> 完整清单见 `retro_loop_output/improvements.md`，下周复盘自动核对 pending → done。")
        else:
            lines += ["（无已平仓交易，暂无改进项）", ""]

        lines += [
            "## 闭环判定",
            "",
        ]
        if bt_count == 0:
            lines += [
                "🟡 **开环**：本周没有跑回测——分析做完就完了，没有进入「回测→质量门→沉淀」闭环。",
                "下周必须至少跑 1 次 walk-forward 回测，否则系统不会进化。",
            ]
        elif gate_counter.get("OVERFITTED", 0) > 0:
            lines += [
                "🟢 **闭环但有失败**：本周跑了回测且发现过拟合——这正是系统在学习的表现。",
                "下周人工 review 这些失败案例，把「为什么过拟合」写成一条 skill。",
            ]
        else:
            lines += [
                "🟢 **闭环健康**：本周回测无过拟合，继续保持。",
                "人工 review ROBUST 候选，决定哪些进虚拟盘。",
            ]

        lines += [
            "",
            "## 下周决策上下文",
            "",
            "（这份文档会被 retro_loop 自动喂给下周的 quant_technical / quant_backtest，",
            " 让 AI 决策时看到历史教训——这就是「失败之后系统变好」）",
        ]
        if ima_knowledge:
            lines += ["", ima_knowledge]
        return "\n".join(lines) + "\n"
    # ---------- 可观测性 ----------

    def health(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": "filesystem",
            "ready": True,
            "hint": "扫描 workspace 下所有 output 目录生成周复盘",
        }

    def describe(self) -> dict[str, Any]:
        return {
            "summary": "每周复盘（学习层）：扫描技术分析+回测产出，找失败模式，沉淀周知识文档",
            "params": {
                "task": {
                    "type": "string",
                    "required": False,
                    "description": "留空即跑本周复盘",
                }
            },
        }
