"""paper_trader / 风控 / 持久化 / 题材打分 回归测试（纯标准库，不触网）。"""

from __future__ import annotations

import json
from pathlib import Path

from connectors import paper_trader as pt
from connectors import quant_theme as theme
from connectors import quant_ersibang as q


# ---- 成本/滑点口径 ----

def test_buy_exec_min_commission():
    bx = pt._buy_exec(10.0, 100)  # 小额，佣金应触最低 5 元
    assert bx["commission"] == 5.0
    assert bx["fill"] > 10.0  # 买入滑点上浮
    assert bx["outlay"] > bx["fill"] * 100


def test_sell_exec_stamp_tax_only_on_sell():
    sx = pt._sell_exec(10.0, 100)
    assert sx["stamp"] > 0  # 卖出收印花税
    assert sx["fill"] < 10.0  # 卖出滑点下浮
    assert sx["net"] < sx["fill"] * 100


# ---- 组合风控闸门 ----

def _pos(code="600000", price=10.0, shares=1000):
    return {"code": code, "current_price": price, "shares": shares}


def test_risk_check_halt_blocks_all_buy():
    ok, why = pt._risk_check("600001", 1000.0, 1000.0, [], 1_000_000, 1_000_000, True)
    assert not ok and "熔断" in why


def test_risk_check_cash_short():
    ok, why = pt._risk_check("600001", 50_000.0, 50_000.0, [], 1_000_000, 1_000.0, False)
    assert not ok and "现金不足" in why


def test_risk_check_single_position_cap():
    # 已有该票 18 万市值，再加 5 万 → 单票 >20%（20 万）被拦
    pos = [_pos("600001", 18.0, 10_000)]  # 18 万
    ok, why = pt._risk_check("600001", 50_000.0, 50_000.0, pos, 1_000_000, 1_000_000, False)
    assert not ok and "单票" in why


def test_risk_check_gross_exposure_cap():
    # 持仓 88 万，再加 5 万 → 总仓 >90% 被拦
    pos = [_pos("600001", 10.0, 88_000)]
    ok, why = pt._risk_check("600002", 50_000.0, 50_000.0, pos, 1_000_000, 1_000_000, False)
    assert not ok and "总仓位" in why


def test_risk_check_pass_when_within_limits():
    pos = [_pos("600001", 10.0, 10_000)]  # 10 万
    ok, why = pt._risk_check("600002", 20_000.0, 20_000.0, pos, 1_000_000, 800_000.0, False)
    assert ok and why == ""


# ---- 回撤熔断 ----

def test_drawdown_halt_triggers_at_threshold():
    hist = [{"equity": 1_000_000}, {"equity": 9_000_000}]
    halt, dd = pt._drawdown_halt(hist, 7_600_000)  # 峰值 900 万 → 回撤 15.6%
    assert halt and dd >= pt.CIRCUIT_BREAKER_DD


def test_drawdown_halt_clear_above_threshold():
    hist = [{"equity": 1_000_000}]
    halt, dd = pt._drawdown_halt(hist, 950_000)
    assert not halt and dd < pt.CIRCUIT_BREAKER_DD


# ---- 持久化：原子写 + 损坏备份 + 不静默丢仓 ----

def test_atomic_write_and_read_back(tmp_path: Path):
    f = tmp_path / "paper_positions.json"
    pt._atomic_write_json(f, {"cash": 1.0})  # 首次建文件，无旧版可备份
    payload = {"positions": [{"code": "600018"}], "history": [], "cash": 500_000.0}
    pt._atomic_write_json(f, payload)  # 二次写：旧版留 .bak
    data, warns = pt._load_account(f)
    assert data["cash"] == 500_000.0 and data["positions"][0]["code"] == "600018"
    assert warns == []
    assert (tmp_path / "paper_positions.json.bak").exists()


def test_corrupt_file_backed_up_not_wiped(tmp_path: Path):
    f = tmp_path / "paper_positions.json"
    f.write_text("{ this is not json ", encoding="utf-8")
    data, warns = pt._load_account(f)
    # 回退空结构，但带告警；原损坏文件保留，另存 corrupt 样本
    assert data["positions"] == [] and data["history"] == []
    assert warns and "解析失败" in warns[0]
    assert f.exists()
    corrupt = list(tmp_path.glob("paper_positions.json.corrupt-*"))
    assert corrupt


# ---- 挂单有效期：日历不可用时跳周末 ----

def test_next_trade_day_skips_weekend():
    # 2026-11-13 周五 → 下一个自然日跳过周六周日 → 周一 11-16
    assert pt._next_trade_day("2026-11-13") == "2026-11-16"


# ---- 题材打分：缺维度剔除分母，不被 0 拉低 ----

def test_theme_score_ignores_missing_dimension():
    board = {
        "change_pct": 6.0,     # 涨幅满分档（+6% → 30 分）
        "leader_pct": 10.0,    # 领涨满分档（10% → 20 分）
        "up_count": None, "down_count": None,      # 广度缺数据
        "main_net_inflow": None,                  # 资金流缺数据
    }
    # 仅涨幅+领涨两维（权重 30/20），满分应归一到 100，而不是被缺维度压到 50 分档
    assert theme.score_board(board) == 100.0


def test_theme_score_partial_dimensions():
    board = {
        "change_pct": -2.0, "leader_pct": 0.0,
        "up_count": None, "down_count": None, "main_net_inflow": None,
    }
    assert theme.score_board(board) == 0.0


# ---- P0 风控封顶：风险定额定仓 / 窄距离保护 / 上限约束 ----

def test_size_shares_risk_budget_basis():
    # 100万本金、1% 风险预算=1万；入场10、止损9.50，每股风险0.50 → 2万股
    shares = q.size_shares(1_000_000, 0.01, 10.0, 9.50)
    assert shares == 20_000
    # 止损潜在亏损不超过风险预算
    assert (10.0 - 9.50) * shares <= 1_000_000 * 0.01 + 100 * 10.0


def test_size_shares_narrow_stop_distance_caps_leverage():
    # 止损距离仅0.10（< 入场价2%）：不放大到旧公式的 10万股；
    # 本例单票市值20%上限先到（2万股），与距离折算共同压住仓位。
    shares = q.size_shares(1_000_000, 0.01, 10.0, 9.90)
    assert shares == 20_000
    assert shares < 100_000  # 未保护旧公式会得到 100,000 股


def test_size_shares_narrow_distance_folded_min_dist():
    # 放宽市值上限以隔离距离折算：0.10 的窄距离按 0.20 折算，股数不翻倍
    shares = q.size_shares(1_000_000, 0.01, 10.0, 9.90, position_cap_pct=1.0)
    assert shares == 50_000  # 旧公式（按0.10）会得到 100,000 股


def test_size_shares_position_cap_bounds_market_value():
    # 风险定额算出的股数受单票市值20%上限约束（入场3元、预算放宽）
    shares = q.size_shares(1_000_000, 0.15, 3.0, 2.70)
    assert 3.0 * shares <= 1_000_000 * pt.SINGLE_POSITION_CAP
    assert shares == 66_600


def test_size_shares_cash_bound():
    # 可用现金 5000 元、入场 10 元 → 最多 500 股
    shares = q.size_shares(1_000_000, 0.01, 10.0, 9.50, cash=5_000.0)
    assert shares == 500


def test_risk_check_blocks_above_risk_budget():
    # 单笔潜在亏损 1.8 万 > 1.5% 预算 1.5 万 → 拦截
    ok, why = pt._risk_check("600001", 1000.0, 1000.0, [], 1_000_000,
                             1_000_000, False, risk_amt=18_000.0)
    assert not ok and "风险" in why


def test_risk_check_pass_within_risk_budget():
    # 潜在亏损 8000 ≤ 预算 1.5 万，其余条件满足 → 放行
    ok, why = pt._risk_check("600002", 20_000.0, 20_000.0, [], 1_000_000,
                             800_000.0, False, risk_amt=8_000.0)
    assert ok and why == ""
