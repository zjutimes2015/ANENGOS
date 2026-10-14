"""共享：读最近 N 期周复盘，作为历史教训上下文（RAG 闭环核心）。

被 quant_technical / quant_backtest 在生成报告前调用，
把"上周哪些信号过拟合/哪些策略被淘汰"喂回给本次决策。
没有这个，每周都是从零开始——这就是开环。
"""

from __future__ import annotations

from pathlib import Path


def load_recent_lessons(workspace: str, max_weeks: int = 2) -> str:
    """读最近 N 期 weekly_*.md，拼成交付报告头部的"历史教训"段。

    没有复盘文件时返回空字符串（首次使用，正常）。
    """
    retro_dir = Path(workspace) / "retro_loop_output"
    if not retro_dir.exists():
        return ""

    files = sorted(retro_dir.glob("weekly_*.md"), reverse=True)[:max_weeks]
    if not files:
        return ""

    parts = ["## 历史教训（来自最近周复盘，决策时请参考）"]
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                parts.append(f"\n### {f.name}\n\n{text}")
        except OSError:
            continue
    return "\n".join(parts)
