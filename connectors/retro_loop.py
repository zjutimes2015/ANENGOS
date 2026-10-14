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


class RetroLoopAdapter(AgentAdapter):
    name = "retro_loop"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        ws = Path(workspace)

        # 1. 扫描所有量化产出
        tech_files = list((ws / "quant_technical_output").glob("*.md")) if (ws / "quant_technical_output").exists() else []
        bt_files = list((ws / "quant_backtest_output").glob("*.md")) if (ws / "quant_backtest_output").exists() else []

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

        # 3. 读 paper_trader 绩效
        perf = self._calc_perf(ws / "paper_positions.json")

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
        )
        (out_dir / name).write_text(content, encoding="utf-8")

        closed = bool(gate_counter)  # 本周有没有跑过回测闭环
        status = "closed_loop" if closed else "open_loop"

        return AgentResult(
            True, self.name, "done",
            f"[retro_loop] 本周复盘 {status}：技术分析 {len(tech_files)} 份 / 回测 {len(bt_files)} 份 / 质量门 {dict(gate_counter)}",
            artifact_paths=[f"retro_loop_output/{name}"],
            meta={
                "week": f"{year}W{week:02d}",
                "tech_count": len(tech_files),
                "bt_count": len(bt_files),
                "gate_distribution": dict(gate_counter),
                "overfit_count": len(overfit_files),
                "closed_loop": closed,
                "ts": _now_iso(),
            },
        )

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
        }

    @staticmethod
    def _build_report(tech_count: int, bt_count: int,
                      gate_counter: Counter, overfit_files: list[str],
                      now: datetime.datetime, perf: dict | None = None) -> str:
        perf = perf or {}
        lines = [
            f"# 周复盘知识（{now.strftime('%Y-%m-%d')} UTC）",
            "",
            "## 本周产出统计",
            f"- 技术分析报告：{tech_count} 份",
            f"- 回测报告：{bt_count} 份",
            "",
        ]

        if perf and perf.get("closed_trades", 0) > 0:
            lines += [
                "## 虚拟盘绩效（paper_trader）",
                f"- 已平仓：{perf['closed_trades']} 笔 | 当前持仓：{perf['open_positions']} 只",
                f"- 胜率：{perf['win_rate']:.1f}%（{perf['wins']}胜/{perf['losses']}负）",
                f"- 平均每笔：{perf['avg_pnl_pct']:+.2f}% | 累计：{perf['total_pnl']:+.2f}元",
                f"- 夏普（年化简化）：{perf['sharpe']:.2f} | 最大回撤：{perf['max_drawdown']:.2f}%",
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
