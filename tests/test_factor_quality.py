"""P0 因子质量门单元测试：IC稳健性统计 / 独立审查 / 滚动WF门（纯标准库，无网络）。"""

from __future__ import annotations

from connectors.factor_library import ic_stats, review_factor
from connectors.factor_batch import _walkforward_gate


def test_ic_stats_stable_positive():
    # 稳定为正、波动小 -> IR高、正IC占比高、分段同向
    ics = [0.05, 0.04, 0.06, 0.05, 0.07, 0.04, 0.05, 0.06, 0.05, 0.06,
           0.05, 0.04, 0.06, 0.05, 0.07, 0.05, 0.06, 0.04, 0.05, 0.06,
           0.05, 0.06, 0.05, 0.04, 0.06, 0.05, 0.07, 0.05, 0.06, 0.05]
    s = ic_stats(ics)
    assert s["ir"] > 1.0
    assert s["ic_pos_ratio"] == 1.0
    assert s["seg_same_sign"] == 1.0


def test_ic_stats_choppy_low_positive():
    # 正负摇摆 -> 正IC占比低、IR小
    ics = [0.05, -0.06, 0.04, -0.05, 0.03, -0.07, 0.02, -0.04, 0.05, -0.03,
           0.04, -0.06, 0.03, -0.05, 0.02, -0.04, 0.05, -0.03, 0.04, -0.02,
           0.03, -0.05, 0.02, -0.04, 0.05, -0.03, 0.04, -0.02, 0.03, -0.04]
    s = ic_stats(ics)
    assert s["ic_pos_ratio"] <= 0.5
    assert abs(s["ir"]) < 0.3


def test_review_rejects_non_pit():
    s = ic_stats([0.05] * 30)
    r = review_factor(s, pit=False, n_periods=30)
    assert not r["pass"]
    assert any("PIT" in h for h in r["hard"])


def test_review_rejects_low_ir():
    s = ic_stats([0.05, -0.06] * 15)
    r = review_factor(s, pit=True, n_periods=30)
    assert not r["pass"]
    assert any("IR" in h for h in r["hard"])


def test_review_rejects_reversed_segment():
    # 总体正、IR达标、正IC占比够，但最后一段整体反向 -> 分段反向否决
    ics = [0.04] * 20 + [-0.03] * 10
    s = ic_stats(ics)
    r = review_factor(s, pit=True, n_periods=30)
    assert not r["pass"]
    assert any("反向" in h for h in r["hard"])


def test_review_passes_clean():
    s = ic_stats([0.05, 0.04, 0.06, 0.05, 0.07] * 6)
    r = review_factor(s, pit=True, n_periods=30)
    assert r["pass"]
    assert r["hard"] == []


def test_wf_gate_passes_stable_excess():
    # 全程稳定正超额 -> WF 通过
    excess = [0.005, 0.004, 0.006, 0.005, 0.007, 0.004, 0.005, 0.006,
              0.005, 0.006, 0.005, 0.004, 0.006, 0.005, 0.007, 0.005,
              0.006, 0.004, 0.005, 0.006, 0.005, 0.006, 0.005, 0.004,
              0.006, 0.005, 0.007, 0.005, 0.006, 0.005]
    g = _walkforward_gate(excess)
    assert g["pass"] is True
    assert g["level"] in ("ROBUST", "MODERATE")


def test_wf_gate_blocks_is_pos_oos_neg():
    # 前半正、后半负（样本外反转）-> 拦截，不通过
    excess = [0.01] * 18 + [-0.02] * 12
    g = _walkforward_gate(excess)
    assert g["pass"] is False
    assert g["level"] in ("WEAK", "OVERFITTED")


def test_wf_gate_too_short():
    g = _walkforward_gate([0.01, 0.01])
    assert g["pass"] is False
    assert g["level"] == "WEAK"
