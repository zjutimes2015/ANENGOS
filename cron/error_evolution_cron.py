#!/usr/bin/env python3
"""错误日志进化周日 cron：跑 error_log + 沉淀进化状态 + 刷新策略认知卡。

crontab 每周日 20:00 跑：
  0 20 * * 0 /home/ubuntu/tv-venv/bin/python3 /home/ubuntu/error_evolution_cron.py >> /home/ubuntu/error_evolution_cron.log 2>&1

产物：
  1. /opt/anengos/workspace/retro_loop_output/error_evolution_YYYYWW.md（进化状态，周一合入周复盘）
  2. /opt/anengos/workspace/cognition/cards/*.md 的「虚拟盘跟踪 / 已知缺陷」两节自动刷新
     （`<!-- auto-refresh -->` 标记段由 cron 按真实数据重写，手工内容保留）
"""
import datetime
import json
import re
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


def _load_jsonl(p: Path) -> list[dict]:
    out: list[dict] = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _refresh_section(text: str, new_body: str) -> str:
    """替换 `<!-- auto-refresh -->` 标记段（该标题行到下一个 `## ` 标题或文末）。"""
    pat = re.compile(r"(## .*?<!-- auto-refresh -->\n)(.*?)(\n## |\Z)", re.DOTALL)
    m = pat.search(text)
    if not m:
        return text
    return text[:m.start(2)] + new_body.rstrip("\n") + "\n" + text[m.start(3):]


def _pos_section(positions: list[dict], mkt: str, label: str) -> str:
    if not positions:
        return (f"| 项 | 值 |\n|---|---|\n| {label} | 0 只（无持仓） |\n"
                f"| 最新交易日 | {mkt} |\n| 状态 | 待下周开盘后跟踪 |")
    codes = " / ".join(p["code"] for p in positions)
    buys = " / ".join(p.get("buy_date", "?") for p in positions)
    pnls = []
    for p in positions:
        v = p.get("pnl_pct")
        pnls.append(f"{v:+.2f}%" if isinstance(v, (int, float)) else "待更新")
    pnl_txt = " / ".join(pnls)
    names = " / ".join((p.get("name") or "")[:4] for p in positions)
    return (
        f"| 项 | 值 |\n|---|---|\n"
        f"| {label} | {len(positions)} 只 |\n"
        f"| 代码 | {codes} |\n"
        f"| 名称 | {names} |\n"
        f"| 买入日期 | {buys} |\n"
        f"| 浮盈% | {pnl_txt} |\n"
        f"| 最新交易日 | {mkt} |\n"
        f"| 刷新时间 | {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')}（周日 cron） |"
    )


def _defects_section(logs: list[dict], strategy_keywords: tuple[str, ...]) -> str:
    """按策略关键词筛 open 教训，输出已知缺陷节。"""
    hits = []
    for l in logs:
        text = f"{l.get('name','')} {l.get('code','')} {l.get('failure_link','')} {l.get('next_action','')}"
        if any(k in text for k in strategy_keywords):
            hits.append(l)
    if not hits:
        return "- 无该策略相关 open 教训（cron 刷新）"
    lines = []
    for l in hits[:8]:
        st = l.get("status", "?")
        lines.append(f"- **[{st}] {l.get('failure_link','未知')}** {l.get('name','')}"
                     f"({l.get('code','')})：{l.get('next_action','')[:60]}")
    lines.append("")
    lines.append(f"> 以上由 error_log 自动同步（{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')}）")
    return "\n".join(lines)


def update_cognition_cards() -> None:
    """刷新 workspace/cognition/cards/ 两张认知卡的自动段（虚拟盘跟踪 + 已知缺陷）。"""
    cards_dir = WS / "cognition" / "cards"
    if not cards_dir.exists():
        print("[cognition] cards 目录不存在，跳过刷新")
        return

    pf = WS / "paper_positions.json"
    data = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {}
    positions = data.get("positions", [])
    mkt = data.get("last_market_date") or "未知"
    logs = _load_jsonl(WS / "error_logs" / "error_log.jsonl")

    esb_pos = [p for p in positions if p.get("strategy") == "ersibang"]
    mf_pos = [p for p in positions if p.get("strategy") in (None, "", "multifactor")]

    # —— ersibang 卡 ——
    card = cards_dir / "ersibang.md"
    if card.exists():
        text = card.read_text(encoding="utf-8")
        text = _refresh_section(text, _pos_section(esb_pos, mkt, "虚拟盘持仓（ersibang 策略）"))
        text = _refresh_section(text, _defects_section(
            logs, ("突破", "左峰", "二四棒", "601229", "603000", "600039", "esb", "贴线", "MA20")))
        card.write_text(text, encoding="utf-8")
        print(f"[cognition] 已刷新 {card.name}")

    # —— multifactor 卡 ——
    card = cards_dir / "multifactor.md"
    if card.exists():
        text = card.read_text(encoding="utf-8")
        text = _refresh_section(text, _pos_section(mf_pos, mkt, "虚拟盘持仓（多因子来源）"))
        text = _refresh_section(text, _defects_section(
            logs, ("多因子", "因子", "MA20", "阈值", "IMP-003", "选股", "001979", "688617", "601658")))
        card.write_text(text, encoding="utf-8")
        print(f"[cognition] 已刷新 {card.name}")


def main() -> None:
    run_error_log()

    logs = _load_jsonl(WS / "error_logs" / "error_log.jsonl")
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

    update_cognition_cards()


if __name__ == "__main__":
    main()
