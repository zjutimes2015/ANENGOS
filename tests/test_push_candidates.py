"""push_candidates 解析 / 合并 / 归档纯函数回归测试（不触网）。"""

from __future__ import annotations

from datetime import datetime

from connectors import push_candidates as pc


# ---- 解析 ----

ALL_A_MD = """# 全A股低波选股

| 排名 | 代码 | 名称 | 收盘 | 总分 | 得分明细 |
|---|---|---|---|---|---|
| 1 | 600018 | 上港集团 | 5.47 | **+2** | 低波(1.8%)+2 |
| 2 | 600025 | 华能水电 | 10.28 | **+1** | 中低波(4.2%)+1 |
"""

MULTI_MD = """# 低波+质量价值观察清单

| 排名 | 代码 | 名称 | 收盘 | 低波 | 质量价值 | 总分 | PE | ROE% | 净利率% | 附加标签 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 600018 | 上港集团 | 5.47 | +2 | +3 | **+5** | 9.1 | 5.9 | 44.0 | 高净利率+2; 低PE+1 |
| 2 | 600030 | 中信证券 | 26.14 | +2 | +2 | **+4** | 10.3 | 7.8 | — | 净利增长≥15%+1; 低PE+1 |
"""


def test_parse_all_a():
    rows = pc.parse_all_a(ALL_A_MD)
    assert len(rows) == 2
    r = rows[0]
    assert r["code"] == "600018" and r["name"] == "上港集团"
    assert r["close"] == 5.47 and r["score"] == 2.0
    assert r["source"] == pc.SOURCE_ALL_A
    assert "低波" in r["detail"]


def test_parse_multifactor():
    rows = pc.parse_multifactor(MULTI_MD)
    assert len(rows) == 2
    r = rows[1]
    assert r["code"] == "600030" and r["score"] == 4.0
    assert r["source"] == pc.SOURCE_MULTI
    assert "低波" in r["detail"] and "低PE" in r["detail"]


def test_parse_ml_json():
    data = {"top10": [{"code": "002086", "name": "东方海洋", "prob": 0.443,
                       "seal_strength": 0.49, "industry": "农产品加工"}]}
    rows = pc.parse_ml_json(data)
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "002086" and r["score"] == 44.3
    assert r["source"] == pc.SOURCE_ML and r["rank"] == 1
    assert "封单强度" in r["detail"]


def test_parse_ml_skips_bad_code():
    data = {"top10": [{"code": "abc", "name": "坏", "prob": 0.5}]}
    assert pc.parse_ml_json(data) == []


# ---- 合并 ----

def test_merge_combines_sources_and_takes_higher_score():
    items = [
        {"code": "600018", "name": "上港集团", "close": 5.47, "score": 2.0,
         "detail": "低波", "source": pc.SOURCE_ALL_A},
        {"code": "600018", "name": "上港集团", "close": 5.47, "score": 5.0,
         "detail": "总分", "source": pc.SOURCE_MULTI},
        {"code": "600025", "name": "华能水电", "close": 10.28, "score": 1.0,
         "detail": "中低波", "source": pc.SOURCE_ALL_A},
    ]
    merged = pc.merge_candidates(items)
    by = {m["code"]: m for m in merged}
    assert set(by["600018"]["sources"]) == {pc.SOURCE_ALL_A, pc.SOURCE_MULTI}
    assert "/".join(by["600018"]["sources"]) == by["600018"]["source"]
    assert by["600018"]["score"] == 5.0  # 取较高分
    assert len(merged) == 2


# ---- 归档判定 ----

def _rec(rid, code, status, date):
    return {"record_id": rid, "code": code, "status": status, "date": date}


def test_archive_stale_candidate():
    today = datetime(2026, 11, 22)
    existing = [_rec("r1", "600018", "候选", "2026-10-17T00:00:00"),
                _rec("r2", "600025", "候选", "2026-11-20T00:00:00")]
    plan = pc.archive_plan(existing, {"600025"}, today)
    assert [p["record_id"] for p in plan] == ["r1"]


def test_archive_keeps_terminal_status():
    today = datetime(2026, 11, 22)
    existing = [_rec("r1", "600018", "已卖出", "2026-10-17T00:00:00"),
                _rec("r2", "600025", "已买入", "2026-10-17T00:00:00")]
    assert pc.archive_plan(existing, set(), today) == []


def test_archive_missing_date_stale_candidate():
    today = datetime(2026, 11, 22)
    existing = [_rec("r1", "600018", "候选", None)]
    plan = pc.archive_plan(existing, set(), today)
    assert [p["record_id"] for p in plan] == ["r1"]


# ---- 字段构造 ----

def test_build_fields_omits_missing_numbers():
    cand = {"code": "002086", "name": "东方海洋", "close": None,
            "score": 44.3, "detail": "封单", "source": pc.SOURCE_ML, "rank": 1}
    f = pc.build_fields(cand, 1763883000000)
    assert f["代码"] == "002086" and f["总分"] == 44.3
    assert f["排名"] == 1 and f["状态"] == "候选"
    assert "收盘价" not in f  # 缺值不传，避免数字字段转换失败


def test_build_fields_rounds_values():
    cand = {"code": "600018", "name": "上港集团", "close": 5.476,
            "score": 2.333, "detail": "低波", "source": pc.SOURCE_ALL_A}
    f = pc.build_fields(cand, 1763883000000)
    assert f["收盘价"] == 5.48 and f["总分"] == 2.3
