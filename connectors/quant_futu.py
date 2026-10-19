"""富途 A 股 connector：连 OpenD（127.0.0.1:11111）拉真实行情。

安全边界（P8，登录始终由用户在 OpenD 界面亲手完成，本代码不接触账号/密码/验证码）：
  - 账号密码只存在于用户本机 OpenD 进程的登录态，Python 侧只连本地回环，不读取、不回显、不落盘。
  - READ_ONLY=True（默认）：只允许 OpenQuoteContext 行情快照，禁止任何 TradeContext 下单。
  - 强制回环：host 必须是 127.0.0.1/localhost，配成 0.0.0.0 或公网地址直接降级 mock，防止误暴露。
  - 将来若要接真实下单：必须显式 READ_ONLY=False，并逐笔过 MAX_ORDER_VALUE 金额上限与 P7 风控闸门。

后端：FutuOpenD（宿主机常驻，API 端口 11111，仅绑回环）
环境变量：ANENGOS_FUTU_HOST（默认 127.0.0.1）、ANENGOS_FUTU_PORT（默认 11111）

任务解析：6 位代码自动加 SH/SZ 前缀。
输出：实时快照（最新价/成交量/涨跌幅），写入 workspace/quant_futu_output/。
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso
from connectors._lessons import load_recent_lessons

# ---- 安全常量（P8）----
READ_ONLY = True            # 默认只读行情；真实下单前必须显式改 False 并过金额上限/风控
MAX_ORDER_VALUE = 20_000.0  # 预留：单笔委托金额上限（元），仅在 READ_ONLY=False 后生效
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

try:
    from futu import OpenQuoteContext  # type: ignore
    _FUTU_AVAILABLE = True
except Exception:  # noqa: BLE001
    OpenQuoteContext = None  # type: ignore
    _FUTU_AVAILABLE = False


def _parse_code(task: str) -> str | None:
    m = re.search(r"\b(\d{6})\b", task)
    return m.group(1) if m else None


def _futu_code(code: str) -> str:
    return f"SH.{code}" if code.startswith("6") else f"SZ.{code}"


class QuantFutuAdapter(AgentAdapter):
    name = "quant_futu"

    def __init__(self) -> None:
        self.host = os.environ.get("ANENGOS_FUTU_HOST", "127.0.0.1")
        self.port = int(os.environ.get("ANENGOS_FUTU_PORT", "11111"))
        # 安全：非回环地址直接禁用，防止把 OpenD 端口暴露到公网
        self._loopback = self.host in _LOOPBACK
        self._block_reason = ""
        if not self._loopback:
            self._block_reason = f"host={self.host} 非回环(仅允许127.0.0.1)，已禁用"
            self.mode = "mock"
        else:
            self.mode = "futu" if _FUTU_AVAILABLE else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        code = _parse_code(task)
        out_dir = Path(workspace) / "quant_futu_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        lessons = load_recent_lessons(workspace)

        if not code:
            return AgentResult(False, self.name, "skipped",
                f"[{self.name}/{self.mode}] 未识别到 6 位代码，例如：分析 600519")

        futu_code = _futu_code(code)

        # 安全闸门：非回环直接阻断；READ_ONLY 下禁止任何下单上下文（本类当前仅行情）
        if not self._loopback:
            return AgentResult(False, self.name, "blocked",
                f"[{self.name}/secure] {self._block_reason}")
        if not _FUTU_AVAILABLE:
            content = f"# 富途分析（mock，futu-api 未装）\n\n- 代码：{futu_code}\n{lessons}"
            path = out_dir / f"futu_{code}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            path.write_text(content, encoding="utf-8")
            return AgentResult(True, self.name, "done",
                f"[{self.name}/mock] {futu_code} mock 报告",
                artifact_paths=[str(path.relative_to(workspace))])

        qctx = None
        try:
            qctx = OpenQuoteContext(host=self.host, port=self.port)
            ret, snap = qctx.get_market_snapshot([futu_code])
            if ret != 0:
                raise RuntimeError(f"get_market_snapshot 失败：{snap}")

            row = snap.iloc[0]
            last = float(row["last_price"])
            vol = float(row["volume"])
            update = str(row.get("update_time", ""))

            content = (
                f"# 富途 A 股实时快照（真实数据）\n\n"
                f"- 代码：{futu_code}\n- 任务：{task}\n"
                f"- 最新价：{last:.2f}\n- 成交量：{vol:.0f}\n- 更新时间：{update}\n\n"
                f"{lessons}\n"
            )
            path = out_dir / f"futu_{code}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            path.write_text(content, encoding="utf-8")
            return AgentResult(True, self.name, "done",
                f"[{self.name}/futu] {futu_code} 最新价 {last:.2f}",
                artifact_paths=[str(path.relative_to(workspace))])
        except Exception as e:  # noqa: BLE001
            return AgentResult(False, self.name, "error",
                f"[{self.name}/futu] {futu_code} 失败：{e}", error=str(e))
        finally:
            if qctx:
                try:
                    qctx.close()
                except Exception:
                    pass

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": _FUTU_AVAILABLE and self._loopback,
                "mode": self.mode, "read_only": READ_ONLY,
                "loopback": self._loopback, "block_reason": self._block_reason,
                "hint": f"连 {self.host}:{self.port}（仅回环；密码不进代码）"}

    def describe(self) -> dict[str, Any]:
        return {"summary": "富途 A 股实时快照（OpenD 网关）",
                "params": {"task": {"type": "string", "description": "例如：分析 600519 贵州茅台"}}}
