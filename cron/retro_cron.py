#!/usr/bin/env python3
"""每周复盘 cron 脚本（独立于 ANENGOS 容器，直接扫 workspace 目录）。

crontab 每周一 09:00 跑：
  0 9 * * 1 /home/ubuntu/tv-venv/bin/python3 /home/ubuntu/retro_cron.py >> /home/ubuntu/retro_cron.log 2>&1

扫 /opt/anengos/workspace/quant_technical_output 和 quant_backtest_output，
生成 weekly_YYYYWW.md 到 retro_loop_output；自动合入周日 error_evolution cron
沉淀的"错误日志进化"章节。
"""
import datetime
import re
from collections import Counter
from pathlib import Path

WS = Path("/opt/anengos/workspace")


def main():
    tech_dir = WS / "quant_technical_output"
    bt_dir = WS / "quant_backtest_output"
    out_dir = WS / "retro_loop_output"
    out_dir.mkdir(parents=True, exist_ok=True)

    tech_files = list(tech_dir.glob("*.md")) if tech_dir.exists() else []
    bt_files = list(bt_dir.glob("*.md")) if bt_dir.exists() else []

    gate = Counter()
    overfit = []
    for f in bt_files:
        text = f.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"ROBUST|MODERATE|WEAK|OVERFITTED", text.upper())
        if m:
            level = m.group(0)
            gate[level] += 1
            if level == "OVERFITTED":
                overfit.append(f.name)

    now = datetime.datetime.now(datetime.timezone.utc)
    y, w, _ = now.isocalendar()
    name = f"weekly_{y}W{w:02d}.md"
    lines = [
        f"# 周复盘（cron 自动生成 {now.strftime('%Y-%m-%d')} UTC）",
        "",
        f"- 技术分析：{len(tech_files)} 份",
        f"- 回测报告：{len(bt_files)} 份",
        f"- 质量门分布：{dict(gate)}",
        "",
    ]
    if overfit:
        lines.append("## ❌ 过拟合教训（下周避开）")
        for f in overfit:
            lines.append(f"- `{f}`")
    else:
        lines.append("（无过拟合）")

    # 错误日志进化章节（周日 error_evolution cron 沉淀）
    ev_files = sorted(out_dir.glob("error_evolution_*.md"), reverse=True)
    if ev_files:
        lines.append("")
        lines.append("## 错误日志进化（本周状态）")
        lines.append(ev_files[0].read_text(encoding="utf-8", errors="replace"))

    (out_dir / name).write_text("\n".join(lines), encoding="utf-8")
    print(f"[{now.isoformat()}] wrote {out_dir.name}/{name} | tech={len(tech_files)} "
          f"bt={len(bt_files)} gate={dict(gate)} ev={'合入' if ev_files else '无'}")


if __name__ == "__main__":
    main()
