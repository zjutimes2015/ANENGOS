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

        # 3. 生成本周复盘知识文档
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
    def _build_report(tech_count: int, bt_count: int,
                      gate_counter: Counter, overfit_files: list[str],
                      now: datetime.datetime) -> str:
        lines = [
            f"# 周复盘知识（{now.strftime('%Y-%m-%d')} UTC）",
            "",
            "## 本周产出统计",
            f"- 技术分析报告：{tech_count} 份",
            f"- 回测报告：{bt_count} 份",
            "",
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
