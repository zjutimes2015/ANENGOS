"""富途 A 股 connector：连 OpenD（127.0.0.1:11111）拉真实行情。

后端：FutuOpenD（宿主机 systemd/前台跑，API 端口 11111）
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
            pct = float(row.get("update_time", ""))  # placeholder

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
        return {"name": self.name, "ready": _FUTU_AVAILABLE, "mode": self.mode,
                "hint": f"连 {self.host}:{self.port}"}

    def describe(self) -> dict[str, Any]:
        return {"summary": "富途 A 股实时快照（OpenD 网关）",
                "params": {"task": {"type": "string", "description": "例如：分析 600519 贵州茅台"}}}
