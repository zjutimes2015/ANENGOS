"""health_check.py: ANENGOS 系统健康检查 hook。

每次 cron 跑之前自动检查：
1. 必要的 connector 是否存在
2. 上次运行的输出文件是否生成
3. 数据文件是否完整
4. 失败时自动告警（写日志 + 可扩展飞书推送）

用法：
  python health_check.py --pre-cron    # cron 跑之前检查
  python health_check.py --post-cron  # cron 跑之后检查
  python health_check.py --full        # 全量检查
"""
from __future__ import annotations
import datetime
import json
import os
import sys
from pathlib import Path

WS = Path("/srv/anengos/workspace")
CONNECTORS = Path("/srv/anengos/connectors")
LOG = WS / "health_check.log"

# 必要的 connector 清单（不能丢）
REQUIRED_CONNECTORS = [
    "factor_batch.py",
    "paper_trader.py",
    "paper_track.py",
    "quant_screener.py",
    "quant_ersibang.py",
    "quant_technical.py",
    "quant_news.py",
    "jev_guardrail.py",
    "walkforward.py",
    "regime_gate.py",
    "feishu_push.py",
]

# 每日必须生成的输出文件
DAILY_OUTPUTS = [
    "news_daily.json",           # 新闻情绪（18:00）
    "paper_track.json",          # 虚拟盘净值（15:40）
]

# 每周必须生成的输出文件
WEEKLY_OUTPUTS = [
    "factor_batch_result.json",   # 因子检验（周日19:30）
    "baostock_weekly.json",       # 周度数据更新（周日20:00）
]


def log(msg: str, level: str = "INFO") -> None:
    """写日志。"""
    ts = datetime.datetime.now().isoformat()
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def check_connectors() -> dict:
    """检查必要的 connector 是否存在。"""
    missing = []
    for name in REQUIRED_CONNECTORS:
        if not (CONNECTORS / name).exists():
            missing.append(name)

    if missing:
        log(f"缺失 connector: {missing}", "ERROR")
        return {"ok": False, "missing": missing}
    else:
        log("所有必要 connector 都在")
        return {"ok": True, "missing": []}


def check_daily_outputs() -> dict:
    """检查每日输出文件是否生成。"""
    today = datetime.date.today().isoformat()
    missing = []
    stale = []

    for name in DAILY_OUTPUTS:
        path = WS / name
        if not path.exists():
            missing.append(name)
            continue
        # 检查是否是今天的（文件 mtime）
        mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        if mtime.date() != datetime.date.today():
            stale.append(f"{name} (最后更新: {mtime.date()})")

    if missing or stale:
        log(f"每日输出缺失: {missing}, 过期: {stale}", "WARN")
        return {"ok": False, "missing": missing, "stale": stale}
    else:
        log("每日输出文件都正常")
        return {"ok": True, "missing": [], "stale": []}


def check_weekly_outputs() -> dict:
    """检查每周输出文件是否生成。"""
    # 周日检查
    if datetime.datetime.today().weekday() != 6:
        return {"ok": True, "skip": "非周日"}

    missing = []
    for name in WEEKLY_OUTPUTS:
        if not (WS / name).exists():
            missing.append(name)

    if missing:
        log(f"每周输出缺失: {missing}", "WARN")
        return {"ok": False, "missing": missing}
    else:
        log("每周输出文件都正常")
        return {"ok": True, "missing": []}


def check_disk_space() -> dict:
    """检查磁盘空间（之前 90% 满过）。"""
    stat = os.statvfs("/")
    total = stat.f_frsize * stat.f_blocks
    free = stat.f_frsize * stat.f_bfree
    used_pct = (total - free) / total * 100

    if used_pct > 85:
        log(f"磁盘使用率 {used_pct:.1f}%，超过85%", "ERROR")
        return {"ok": False, "used_pct": used_pct}
    else:
        log(f"磁盘使用率 {used_pct:.1f}%")
        return {"ok": True, "used_pct": used_pct}


def full_check() -> dict:
    """全量检查。"""
    log("=== 开始健康检查 ===")

    results = {
        "connectors": check_connectors(),
        "daily_outputs": check_daily_outputs(),
        "weekly_outputs": check_weekly_outputs(),
        "disk": check_disk_space(),
    }

    all_ok = all(r.get("ok", False) for r in results.values())

    if all_ok:
        log("=== 健康检查通过 ===")
    else:
        log("=== 健康检查失败 ===", "ERROR")
        # TODO: 这里可以加飞书/微信告警

    return results


if __name__ == "__main__":
    if "--pre-cron" in sys.argv:
        # cron 跑之前只检查 connector 是否存在
        r = check_connectors()
        sys.exit(0 if r["ok"] else 1)
    elif "--post-cron" in sys.argv:
        # cron 跑之后检查输出是否生成
        r = check_daily_outputs()
        sys.exit(0 if r["ok"] else 1)
    elif "--full" in sys.argv:
        full_check()
    else:
        print("用法: python health_check.py [--pre-cron|--post-cron|--full]")
