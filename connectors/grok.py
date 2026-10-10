"""Grok（xAI）适配器：多智能体总装线外部智能体之三。

真实后端：xAI Chat Completions（api.x.ai，海外；本机/有代理环境可用，国内服务器直连通常超时）：
  - ANENGOS_GROK_API_KEY   xAI API Key（xai-...）
  - ANENGOS_GROK_MODEL     默认 grok-2-latest
  - ANENGOS_GROK_MODE      cli|http|mock（默认：有 key 走 http，否则 mock）

统一 Schema：submit 返回 AgentResult；health()/describe() 供管理台可观测性与自动注册。
"""

from __future__ import annotations

import datetime
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso


class GrokAdapter(AgentAdapter):
    name = "grok"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 mode: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("ANENGOS_GROK_API_KEY", "")
        self.model = model or os.environ.get("ANENGOS_GROK_MODEL", "grok-2-latest")
        self.base_url = (os.environ.get("ANENGOS_GROK_BASE_URL") or "https://api.x.ai/v1").rstrip("/")
        if mode or os.environ.get("ANENGOS_GROK_MODE"):
            self.mode = (mode or os.environ["ANENGOS_GROK_MODE"]).lower()
        else:
            self.mode = "http" if self.api_key else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        if not task:
            return AgentResult(False, self.name, "skipped", f"[{self.name}] 任务为空，未执行",
                               error="task 为空")
        if self.mode == "http":
            return self._run_http(task)
        if self.mode == "cli":
            return AgentResult(False, self.name, "unconfigured",
                               f"[{self.name}/cli] Grok 未开放 CLI，请使用 http 或 mock 模式",
                               error="no cli", meta={"mode": "cli", "ts": _now_iso()})
        return self._run_mock(task, workspace)

    def _run_http(self, task: str) -> AgentResult:
        if not self.api_key:
            return AgentResult(False, self.name, "unconfigured",
                               f"[{self.name}/http] 未配置 ANENGOS_GROK_API_KEY",
                               error="missing api key", meta={"mode": "http", "ts": _now_iso()})
        try:
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": task}],
            }
            req = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"]
            return AgentResult(True, self.name, "done",
                               f"[{self.name}/http] 完成：{text[:500]}",
                               meta={"mode": "http", "model": self.model, "ts": _now_iso()})
        except urllib.error.HTTPError as e:
            return AgentResult(False, self.name, "error",
                               f"[{self.name}/http] HTTP {e.code}：{e.read().decode(errors='replace')[:300]}",
                               error=f"HTTP {e.code}", meta={"mode": "http", "ts": _now_iso()})
        except Exception as e:  # noqa: BLE001
            return AgentResult(False, self.name, "error",
                               f"[{self.name}/http] 调用失败：{str(e)[:300]}",
                               error=str(e)[:300], meta={"mode": "http", "ts": _now_iso()})

    def _run_mock(self, task: str, workspace: str) -> AgentResult:
        out_dir = Path(workspace) / f"{self.name}_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        name = f"{self.name}_{ts}.md"
        content = (
            f"# {self.name} 交付物（演示模式）\n\n"
            f"- 任务：{task}\n"
            "- 状态：已由 Grok(演示)分析并产出\n"
            "- 说明：配置 ANENGOS_GROK_API_KEY 且网络可达时自动切换为 xAI 真实调用\n"
        )
        (out_dir / name).write_text(content, encoding="utf-8")
        return AgentResult(
            True, self.name, "done",
            f"[{self.name}/演示] 已产出 {out_dir.name}/{name}（{len(content)} 字符）",
            artifact_paths=[f"{out_dir.name}/{name}"],
            meta={"mode": "mock", "ts": _now_iso()},
        )

    # ---------- 可观测性 ----------

    def health(self) -> dict:
        ready = self.mode == "mock" or (self.mode == "http" and bool(self.api_key))
        hint = {
            "http": "已配置 xAI API Key" if self.api_key else "缺少 ANENGOS_GROK_API_KEY",
            "cli": "Grok 未开放 CLI",
            "mock": "演示模式（未配置真实后端）",
        }.get(self.mode, "未知模式")
        return {"name": self.name, "mode": self.mode, "ready": ready, "hint": hint}

    def describe(self) -> dict:
        return {
            "summary": "把文本/内容任务交给 Grok（xAI）执行（http / 演示）",
            "params": {"task": {"type": "string", "required": True}},
        }
