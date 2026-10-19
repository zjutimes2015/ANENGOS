#!/usr/bin/env python3
"""P3 因子周日挖掘节拍（独立于容器，直接读写挂载目录，零 LLM / 零全量拉取）。

crontab 每周日 19:00（在 error_evolution 20:00 前）：
  0 19 * * 0 /home/ubuntu/tv-venv/bin/python3 /home/ubuntu/factor_discovery_cron.py >> /home/ubuntu/factor_discovery_cron.log 2>&1

三源产候选（写入 factor_library.json 的 candidate 队列，等待容器内检验过 L2-L4）：
  源A 参数网格微扰：对 active 低波因子换窗口(10/30/60日)产同族变体；
  源B 残差正交：需面板，留接口（容器检验阶段实现）；
  源C 失败反推：读 error_logs/error_log.jsonl，按失败环节聚合高频模式 → 过滤型候选假设。

不在这里跑全量重检验（烧数据/LLM），只产"待检验假设"入队，形成自循环队列。
"""
import datetime
import json
import os
from collections import Counter
from pathlib import Path

WS = Path(os.environ.get("ANENGOS_WS", "/opt/anengos/workspace"))
LIB = WS / "factor_library.json"

# 源A：低波族参数网格（窗口微扰），active=vol20 时展开
GRID = {
    "low_vol": [
        ("vol10", "10日低波动", "std(日收益率,10)", "asc(低波优先)"),
        ("vol30", "30日低波动", "std(日收益率,30)", "asc(低波优先)"),
        ("vol60", "60日低波动", "std(日收益率,60)", "asc(低波优先)"),
    ],
}


def _load_lib() -> dict:
    if LIB.exists():
        return json.loads(LIB.read_text(encoding="utf-8"))
    return {"version": 1, "factors": {}}


def _save_lib(data: dict) -> None:
    LIB.parent.mkdir(parents=True, exist_ok=True)
    LIB.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def source_grid(data: dict) -> list[str]:
    """源A：对每个 active 且其 family 在网格表中的因子，展开窗口变体，已存在跳过。"""
    added = []
    existing = set(data["factors"].keys())
    for fid, rec in list(data["factors"].items()):
        if rec.get("status") != "active":
            continue
        fam = rec.get("family")
        for nid, name, formula, direction in GRID.get(fam, []):
            if nid in existing:
                continue
            data["factors"][nid] = {
                "id": nid, "name": name, "family": fam,
                "pool": rec.get("pool", []), "freq": rec.get("freq", "5d"),
                "formula": formula, "direction": direction,
                "pit": True, "logic": f"由 active 因子 {fid} 参数网格微扰(换窗口)而来",
                "status": "candidate", "metrics": {"source": "grid", "parent": fid},
                "history": [{"date": _today(), "event": "源A参数网格入队"}],
                "created": _today(), "updated": _today(),
            }
            existing.add(nid)
            added.append(nid)
    return added


def source_errors(data: dict) -> list[str]:
    """源C：读 error_log.jsonl，按失败环节聚合，为高频模式产过滤型候选假设。"""
    log = WS / "error_logs" / "error_log.jsonl"
    if not log.exists():
        return []
    link_count = Counter()
    examples: dict[str, str] = {}
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        link = obj.get("failure_link") or obj.get("link") or "未分类"
        link_count[link] += 1
        examples.setdefault(link, obj.get("reason") or obj.get("next_action") or "")

    added = []
    existing = set(data["factors"].keys())
    # 失败环节 → 过滤型假设模板
    templates = {
        "信号层（判断依据本身错误）": ("filter_signal_quality", "信号质量过滤(距MA20安全垫)",
                                  "入场前要求价格距MA20有安全垫，避免贴线被洗"),
        "风控层（仓位/止损规则缺失）": ("filter_risk_stop", "风控止损过滤",
                                  "入场前校验止损距离/仓位上限，超限拒入"),
    }
    for link, cnt in link_count.items():
        if cnt < 2:  # 高频(≥2)才升级为假设，避免噪音
            continue
        key = None
        for tag, t in templates.items():
            if tag.split("（")[0] in link:
                key = t
                break
        if not key:
            continue
        nid, name, logic = key
        if nid in existing:
            continue
        data["factors"][nid] = {
            "id": nid, "name": name, "family": "filter",
            "pool": [], "freq": "5d", "formula": "见logic(过滤条件)",
            "direction": "过滤", "pit": True, "logic": logic,
            "status": "candidate",
            "metrics": {"source": "error", "fail_count": cnt, "example": examples.get(link, "")[:120]},
            "history": [{"date": _today(), "event": f"源C失败反推入队({link}×{cnt})"}],
            "created": _today(), "updated": _today(),
        }
        existing.add(nid)
        added.append(nid)
    return added


def main() -> None:
    data = _load_lib()
    g = source_grid(data)
    e = source_errors(data)
    _save_lib(data)

    now = datetime.datetime.now(datetime.timezone.utc)
    y, w, _ = now.isocalendar()
    out_dir = WS / "factor_discovery_output"
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = Counter(r["status"] for r in data["factors"].values())
    queue = [fid for fid, r in data["factors"].items() if r["status"] == "candidate"]

    lines = [
        f"# 因子挖掘周报 {y}W{w:02d}（cron {now.strftime('%Y-%m-%d')} UTC）",
        "",
        f"- 本周新增候选：grid={g}；error={e}",
        f"- 库存：{dict(summary)}",
        f"- 待检验队列({len(queue)})：{', '.join(queue) if queue else '空'}",
        "",
        "## 说明",
        "- 源B 残差正交需历史面板，在容器检验阶段实现；本周只产假设入队，未跑重检验。",
        "- 候选由容器内检验过 L2(时序+BH+bootstrap)→L3(去冗)→L4(lift)→L5(OOS) 后才可能升 active。",
    ]
    name = f"discovery_{y}W{w:02d}.md"
    (out_dir / name).write_text("\n".join(lines), encoding="utf-8")
    print(f"[{now.isoformat()}] grid={g} error={e} summary={dict(summary)} queue={len(queue)}")


if __name__ == "__main__":
    main()
