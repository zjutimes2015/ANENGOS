# -*- coding: utf-8 -*-
"""ml_seal_strength.py：加封单强度特征的涨停连板预测（离线产物写 ml_seal_strength.json）。

取近 30 个工作日东财涨停池，构造首板样本 + 封单强度特征，随机森林预测连板概率，
按概率取 Top10 写 `/srv/anengos/workspace/ml_seal_strength.json`，供 push_candidates 汇总进候选表。

健壮性：
  - 接口逐日失败只记录、不中断；全窗口无样本时写空结果并正常退出（不再裸 except 后空 concat 崩）；
  - 标签只有一类时无法训练分类器，退化为按封单强度排序兜底；
  - 样本量过小（< 10）不训练，避免小样本过拟合报错。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd

WORKSPACE = Path("/srv/anengos/workspace")
OUT_PATH = WORKSPACE / "ml_seal_strength.json"
LOOKBACK_DAYS = 30
MIN_SAMPLES = 10

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import accuracy_score
    from sklearn.model_selection import train_test_split
    _SKLEARN_AVAILABLE = True
except Exception:  # noqa: BLE001
    _SKLEARN_AVAILABLE = False


def _fetch_pool() -> pd.DataFrame:
    """逐工作日拉涨停池；单日失败记日志跳过，返回拼接后的 DataFrame（可能为空）。"""
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    dates = pd.date_range(start_date, end_date, freq="B")
    all_zt: list[pd.DataFrame] = []
    for date in dates:
        date_str = date.strftime("%Y%m%d")
        try:
            df = ak.stock_zt_pool_em(date=date_str)
            if df is not None and len(df) > 0:
                df = df.copy()
                df["date"] = date_str
                all_zt.append(df)
            time.sleep(0.3)
        except Exception as e:  # noqa: BLE001
            print(f"[ML] {date_str} 涨停池拉取失败，跳过: {type(e).__name__} {e}")
    if not all_zt:
        return pd.DataFrame()
    return pd.concat(all_zt, ignore_index=True)


def _build_first_boards(zt_df: pd.DataFrame) -> pd.DataFrame:
    """按代码取首板样本，打标签 y=1（2 连板及以上）否则 0。"""
    first_boards: list[dict] = []
    for code, group in zt_df.groupby("代码"):
        group = group.sort_values("date")
        first = group.iloc[0]
        max_boards = group["连板数"].max()
        first_boards.append({
            "code": code,
            "name": first["名称"],
            "float_mv": first["流通市值"],
            "turnover": first["换手率"],
            "amount": first["成交额"],
            "blast": first["炸板次数"],
            "seal_fund": first["封板资金"],
            "first_seal_min": first["首次封板时间"],
            "industry": first["所属行业"],
            "max_boards": max_boards,
            "y": 1 if max_boards >= 2 else 0,
        })
    df = pd.DataFrame(first_boards)
    if df.empty:
        return df
    df["seal_strength"] = df["seal_fund"] / df["amount"].replace(0, np.nan)
    df["seal_ratio_mv"] = df["seal_fund"] / df["float_mv"].replace(0, np.nan)

    def time_to_minutes(t) -> float:
        t = str(t).zfill(6)
        return int(t[:2]) * 60 + int(t[2:4])

    df["first_seal_min"] = df["first_seal_min"].apply(time_to_minutes)
    df["seal_stability"] = df["blast"] / (df["first_seal_min"] + 1)
    return df


def _feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    industry_dummies = pd.get_dummies(df["industry"], prefix="ind")
    return pd.concat([
        df[["float_mv", "turnover", "amount", "blast", "seal_fund",
            "first_seal_min", "seal_strength", "seal_ratio_mv", "seal_stability"]],
        industry_dummies,
    ], axis=1)


def _empty_result(reason: str) -> dict:
    return {"n_features": 0, "accuracy": None, "reason": reason,
            "top_features": {}, "top10": []}


def main() -> dict:
    print("拉取涨停数据...")
    zt_df = _fetch_pool()
    if zt_df.empty:
        print("[ML] 近窗口无涨停样本，写出空结果")
        result = _empty_result("no_zt_pool_sample")
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    print(f"共 {len(zt_df)} 条，{zt_df['代码'].nunique()} 只")
    df = _build_first_boards(zt_df)
    if df.empty or len(df) < MIN_SAMPLES:
        result = _empty_result(f"insufficient_samples:{len(df)}")
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    features = _feature_frame(df)
    features = features.fillna(0)
    labels = df["y"]
    print(f"共 {len(df)} 只首板，{int(labels.sum())} 只 2 连板以上，特征数 {features.shape[1]}")

    prob: np.ndarray
    accuracy: float | None = None
    top_features: dict = {}

    can_train = _SKLEARN_AVAILABLE and labels.nunique() == 2 and len(df) >= MIN_SAMPLES
    if can_train:
        X_train, X_test, y_train, y_test = train_test_split(
            features, labels, test_size=0.3, random_state=42)
        rf = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
        rf.fit(X_train, y_train)
        accuracy = float(accuracy_score(y_test, rf.predict(X_test)))
        prob = rf.predict_proba(features)[:, 1]
        importances = pd.Series(rf.feature_importances_, index=features.columns)
        top_features = importances.sort_values(ascending=False).head(10).to_dict()
        print(f"随机森林准确率: {accuracy:.2%}")
    else:
        # 单一标签 / 无 sklearn：退化为按封单强度排序，概率用封单强度归一兜底
        print("[ML] 不可训练（标签单一或缺 sklearn），退化为封单强度排序")
        prob = (df["seal_strength"].fillna(0) /
                df["seal_strength"].fillna(0).max()).to_numpy() if df["seal_strength"].max() > 0 \
            else np.zeros(len(df))

    df["prob"] = prob
    top10 = df.sort_values("prob", ascending=False).head(10)
    top10_records = [{
        "code": r["code"], "name": r["name"], "prob": round(float(r["prob"]), 4),
        "seal_strength": round(float(r["seal_strength"]), 3) if pd.notna(r["seal_strength"]) else 0.0,
        "industry": r["industry"],
    } for _, r in top10.iterrows()]

    result = {
        "n_features": int(features.shape[1]),
        "accuracy": round(accuracy, 4) if accuracy is not None else None,
        "top_features": {k: round(float(v), 4) for k, v in top_features.items()},
        "top10": top10_records,
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已保存：Top10 {len(top10_records)} 只 → {OUT_PATH}")
    return result


if __name__ == "__main__":
    main()
