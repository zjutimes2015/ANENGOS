#!/usr/bin/env python3
"""错误日志进化周日 cron：跑 error_log + 把进化状态沉淀为周复盘输入章节。

crontab 每周日 20:00 跑：
  0 20 * * 0 /home/ubuntu/tv-venv/bin/python3 /home/ubuntu/error_evolution_cron.py >> /home/ubuntu/error_evolution_cron.log 2>&1

产物：/opt/anengos/workspace/retro_loop_output/error_evolution_YYYYWW.md
      （周一 retro_cron.py 生成周复盘时会自动合入"错误日志进化"章节）
"""
import datetime
import json
import subprocess
from pathlib import Path

WS = Path("/opt/anengos/workspace")


def run_error_log() -> None:
    """容器内跑 error_log（保证最新代码 + 与 ANENGOS 同一 workspace）。"""
    code = (
        "from pathlib import Path; "
        "from connectors.error_log import ErrorLogAdapter; "
        "print(ErrorLogAdapter().submit('', str(Path('/srv/anengos/workspace'))).text)"
    )
    subprocess.run(
        ["sudo", "docker", "exec", "-e", "PYTHONPATH=/srv/anengos",
         "anengos", "python", "-c", code],
        capture_output=True, text=True, timeout=180,
    )


def main() -> None:
    run_error_log()

    logs: list[dict] = []
    jf = WS / "error_logs" / "error_log.jsonl"
    if jf.exists():
        for line in jf.read_text(encoding="utf-8").splitlines():
            if line.strip():
                logs.append(json.loads(line))

    open_cnt = sum(1 for l in logs if l.get("status") == "open")
    fixed_cnt = sum(1 for l in logs if l.get("status") == "fixed")
    verified_cnt = sum(1 for l in logs if l.get("status") == "verified")
    by_link: dict[str, int] = {}
    for l in logs:
        fl = l.get("failure_link", "未知")
        by_link[fl] = by_link.get(fl, 0) + 1
    active = [l for l in logs if l.get("status") == "open"]

    now = datetime.datetime.now(datetime.timezone.utc)
    y, w, _ = now.isocalendar()
    out = WS / "retro_loop_output"
    out.mkdir(parents=True, exist_ok=True)
    fname = f"error_evolution_{y}W{w:02d}.md"
    lines = [
        f"# 错误日志进化（{now.strftime('%Y-%m-%d')} 周日 cron）",
        "",
        f"- 累计 {len(logs)} 条 | open {open_cnt} | fixed {fixed_cnt} | verified {verified_cnt}",
        "- 分层分布：",
    ]
    for fl, c in by_link.items():
        lines.append(f"  - {fl}: {c}")
    lines += ["", "## 待办教训（open）"]
    if active:
        for l in active[:10]:
            lines.append(f"- {l['name']}({l['code']})：{l['next_action'][:60]}")
    else:
        lines.append("- 无（全部已 closed）")
    (out / fname).write_text("\n".join(lines), encoding="utf-8")
    print(f"[{now.isoformat()}] wrote {out.name}/{fname} | "
          f"total={len(logs)} open={open_cnt} fixed={fixed_cnt} verified={verified_cnt}")


if __name__ == "__main__":
    main()
