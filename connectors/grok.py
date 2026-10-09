"""Grok（xAI）适配器：多智能体总装线外部智能体之三。

真实后端：xAI Chat Completions（api.x.ai，海外；本机/有代理环境可用，国内服务器直连通常超时）：
  - ANENGOS_GROK_API_KEY   xAI API Key（xai-...）
  - ANENGOS_GROK_MODEL     默认 grok-2-latest
  - ANENGOS_GROK_MODE      cli|http|mock（默认：有 key 走 http，否则 mock）

mock 模式：工作区产出确定性交付物，用于服务器/CI 验证整条链路。
"""

from __future__ import annotations

import datetime
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter


class GrokAdapter(AgentAdapter):
    name = "grok"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("ANENGOS_GROK_API_KEY", "")
        self.model = model or os.environ.get("ANENGOS_GROK_MODEL", "grok-2-latest")
        self.base_url = (os.environ.get("ANENGOS_GROK_BASE_URL") or "https://api.x.ai/v1").rstrip("/")
        if os.environ.get("ANENGOS_GROK_MODE"):
            self.mode = os.environ["ANENGOS_GROK_MODE"].lower()
        else:
            self.mode = "http" if self.api_key else "mock"

    def submit(self, task: str, workspace: str) -> str:
        task = (task or "").strip()
        if not task:
            return f"[{self.name}] 任务为空，未执行"
        if self.mode == "http":
            return self._run_http(task)
        if self.mode == "cli":
            return f"[{self.name}/cli] Grok 未开放 CLI，请使用 http 或 mock 模式"
        return self._run_mock(task, workspace)

    def _run_http(self, task: str) -> str:
        if not self.api_key:
            return f"[{self.name}/http] 未配置 ANENGOS_GROK_API_KEY"
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
            return f"[{self.name}/http] 完成：{text[:500]}"
        except urllib.error.HTTPError as e:
            return f"[{self.name}/http] HTTP {e.code}：{e.read().decode(errors='replace')[:300]}"
        except Exception as e:  # noqa: BLE001
            return f"[{self.name}/http] 调用失败：{str(e)[:300]}"

    def _run_mock(self, task: str, workspace: str) -> str:
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
        return f"[{self.name}/演示] 已产出 {out_dir.name}/{name}（{len(content)} 字符）"
