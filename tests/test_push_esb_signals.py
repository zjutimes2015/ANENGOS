"""push_esb_signals 二四棒写表回归测试（纯标准库 + 临时目录，不触网）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from connectors import push_esb_signals as pe


def _write_pos(ws: Path, positions: list[dict], history: list[dict]) -> None:
    (ws / "paper_positions.json").write_text(
        json.dumps({"positions": positions, "history": history}), encoding="utf-8")


# ---- _latest_scan ----

def test_latest_scan_missing_dir(tmp_path: Path):
    out = pe._latest_scan(tmp_path)
    assert out["breakout"] == [] and out["pending"] == [] and out["signals"] == []


def test_latest_scan_corrupt_json(tmp_path: Path):
    d = tmp_path / "esb_scan_output"
    d.mkdir()
    (d / "esb_scan_20261120.json").write_text("{bad json", encoding="utf-8")
    out = pe._latest_scan(tmp_path)
    assert out["signals"] == []


def test_latest_scan_reads_latest(tmp_path: Path):
    d = tmp_path / "esb_scan_output"
    d.mkdir()
    (d / "esb_scan_20261119.json").write_text(
        json.dumps({"breakout": [], "pending": [], "signals": []}), encoding="utf-8")
    (d / "esb_scan_20261120.json").write_text(json.dumps({
        "breakout": [{"code": "600000", "type": "breakout"}],
        "pending": [{"code": "000001", "type": "pending"}],
        "signals": [{"code": "600000", "type": "breakout"},
                    {"code": "000001", "type": "pending"}],
    }), encoding="utf-8")
    out = pe._latest_scan(tmp_path)
    assert [s["code"] for s in out["signals"]] == ["600000", "000001"]


# ---- account_state ----

def test_account_state_held_and_sold(tmp_path: Path):
    _write_pos(
        tmp_path,
        positions=[{"code": "600000", "shares": 100, "stop": 9.8}],
        history=[
            {"code": "000001", "sell_date": "2026-11-10"},
            {"code": "600000", "sell_date": "2026-11-05"},  # 仍持仓，不计 sold
        ])
    held, sold = pe.account_state(tmp_path)
    assert "600000" in held
    assert sold == {"000001"}


# ---- build_row ----

def _breakout(code: str = "600000") -> dict:
    return {"code": code, "name": "测试", "type": "breakout",
            "entry_px": 10.5, "stop_px": 9.8, "shares": 2000,
            "peak_idx": 30, "level_idx": 15, "gap_pct": 0.0,
            "close_px": 10.6, "last_date": "2026-11-20"}


def _pending(code: str = "000001") -> dict:
    sig = _breakout(code)
    sig["type"] = "pending"
    sig["gap_pct"] = 2.4
    return sig


def test_build_row_breakout_candidate():
    row = pe.build_row(_breakout(), {}, set())
    assert row["_terminal"] is False
    f = row["fields"]
    assert f["状态"] == ["候选"] and f["信号类型"] == ["突破"]
    assert f["左峰价"] == 10.5 and f["左级止损"] == 9.8
    assert f["计划股数"] == 2000 and f["收盘价"] == 10.6
    assert f["峰idx"] == 30 and f["级idx"] == 15


def test_build_row_pending_order():
    row = pe.build_row(_pending(), {}, set())
    assert row["fields"]["状态"] == ["挂单"]
    assert row["fields"]["信号类型"] == ["挂单"]
    assert row["fields"]["距突破%"] == 2.4


def test_build_row_held_pulls_position_fields():
    held = {"600000": {"shares": 300, "stop": 10.1, "current_price": 11.0,
                       "buy_date": "2026-11-19"}}
    row = pe.build_row(_breakout(), held, set())
    f = row["fields"]
    assert f["状态"] == ["持仓"]
    assert f["移动止损"] == 10.1 and f["计划股数"] == 300
    assert f["收盘价"] == 11.0 and f["备注"] == "买入日 2026-11-19"


def test_build_row_sold_is_terminal():
    row = pe.build_row(_breakout(), {}, {"600000"})
    assert row["_terminal"] is True and "fields" not in row


def test_build_row_bad_code_rejected():
    assert pe.build_row({"code": "920001"}, {}, set()) is None
    assert pe.build_row({"code": "abc"}, {}, set()) is None


# ---- archive_plan ----

def _existing(code: str, status: str, date: str | None, rid: str) -> dict:
    return {"record_id": rid, "code": code, "status": status, "date": date}


def test_archive_plan_held_position_kept():
    existing = [_existing("600000", "持仓", "2026-11-20", "r1")]
    held = {"600000": {}}
    plan = pe.archive_plan(existing, set(), held, set(), datetime.now())
    assert plan == []


def test_archive_plan_sold_marked_sold():
    existing = [_existing("000001", "候选", "2026-11-19", "r2")]
    plan = pe.archive_plan(existing, set(), {}, {"000001"}, datetime.now())
    assert len(plan) == 1 and plan[0]["target"] == "已卖出"


def test_archive_plan_stale_candidate_removed():
    old = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    fresh = datetime.now().strftime("%Y-%m-%d")
    existing = [_existing("600000", "候选", old, "r1"),
                _existing("000001", "挂单", fresh, "r2")]
    plan = pe.archive_plan(existing, set(), {}, set(), datetime.now())
    targets = {p["code"]: p["target"] for p in plan}
    assert targets.get("600000") == "已剔除"
    assert "000001" not in targets


def test_archive_plan_terminal_untouched():
    existing = [_existing("600000", "已卖出", "2026-11-10", "r1")]
    plan = pe.archive_plan(existing, set(), {}, set(), datetime.now())
    assert plan == []
