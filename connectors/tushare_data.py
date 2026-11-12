"""Tushare Pro 数据访问层（PIT 对齐）。

安全：token 绝不硬编码、绝不进 Git。读取优先级：
  1) 环境变量 TUSHARE_TOKEN
  2) workspace/.secrets/tushare_token（服务器本地，已被 .gitignore 排除）

覆盖接口（积分档位由账号决定，未开通会在调用处抛权限错误并由调用方记档）：
  - daily         日线 OHLCV / 涨跌幅 / 成交额 / 换手率
  - daily_basic   日频估值 PE_TTM/PB/PS/股息率/流通·总市值/换手率
  - fina_indicator 季度财务指标 ROE/净利率/毛利率/营收净利同比（PIT 用 ann_date 公告日对齐）

用法：
  from connectors.tushare_data import get_pro, daily_basic, fina_indicator_pit, health
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

WS = Path("/srv/anengos/workspace")
SECRET_FILE = WS / ".secrets" / "tushare_token"
CACHE_DIR = WS / "tushare_cache"

_pro = None


def _read_token() -> str:
    tok = os.environ.get("TUSHARE_TOKEN", "").strip()
    if tok:
        return tok
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    return ""


def get_pro():
    """懒加载 Tushare pro_api；token 缺失抛明确错误。"""
    global _pro
    if _pro is not None:
        return _pro
    import tushare as ts  # type: ignore
    token = _read_token()
    if not token:
        raise RuntimeError("TUSHARE_TOKEN 未配置（环境变量或 .secrets/tushare_token）")
    _pro = ts.pro_api(token)
    return _pro


def _retry(fn: Callable[[], Any], tries: int = 3, base_sleep: float = 1.5):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(base_sleep * (i + 1))
    raise last  # type: ignore[misc]


def daily(ts_code: str, start_date: str, end_date: str) -> Any:
    """日线：YYYYMMDD 字符串；返回 DataFrame。"""
    return _retry(lambda: get_pro().daily(ts_code=ts_code, start_date=start_date, end_date=end_date))


def daily_basic(ts_code: str | None = None, trade_date: str | None = None,
                start_date: str | None = None, end_date: str | None = None) -> Any:
    """日频估值面板；可按单日全市场(trade_date)或单票区间取。"""
    def _call():
        kw: dict[str, Any] = {"fields": "ts_code,trade_date,pe_ttm,pb,ps_ttm,dividend_yield,turnover_rate,total_mv,circ_mv"}
        if ts_code:
            kw["ts_code"] = ts_code
        if trade_date:
            kw["trade_date"] = trade_date
        if start_date:
            kw["start_date"] = start_date
        if end_date:
            kw["end_date"] = end_date
        return get_pro().daily_basic(**kw)
    return _retry(_call)


def fina_indicator_pit(ts_code: str) -> Any:
    """财务指标 PIT 原始表（含 ann_date 公告日）。

    调用方做 PIT 对齐：在公告日 ann_date 之前不得使用该期数据，杜绝前视。
    """
    def _call():
        return get_pro().fina_indicator(ts_code=ts_code,
                                        fields="ts_code,ann_date,end_date,roe,netprofit_margin,grossprofit_margin,or_yoy,netprofit_yoy")
    return _retry(_call)


def health() -> dict[str, Any]:
    """只读探测：token 是否配置 + tushare 是否可导入；不发起需权限的请求。"""
    info: dict[str, Any] = {"token_configured": bool(_read_token())}
    try:
        import tushare  # type: ignore
        info["tushare_installed"] = True
        info["tushare_version"] = getattr(tushare, "__version__", "?")
    except Exception as e:  # noqa: BLE001
        info["tushare_installed"] = False
        info["err"] = str(e)[:160]
    return info
