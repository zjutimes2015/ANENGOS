"""OpenAI Codex 适配器：多智能体总装线第一个外部智能体。

三模式可插拔（ANENGOS_CODEX_MODE，默认 mock）：
  - cli   本机/开发机装了 codex CLI：subprocess 调 `codex exec`，产出真实结果
  - http  OpenAI Codex REST（需 ANENGOS_CODEX_API_KEY 且网络可达海外）
  - mock  确定性演示：在工作区产出结果文件，用于服务器/CI 验证整条
          指挥 -> 审批 -> 真实执行 -> 审计 链路

统一 Schema：submit 返回 AgentResult（ok/provider/status/text/artifact_paths/error/meta），
管理台与互审无需解析各家字符串；health() 报告就绪状态，describe() 提供能力描述。
"""

from __future__ import annotations

import datetime
import os
import subprocess
from pathlib import Path

from connectors.base import AgentAdapter, AgentResult, _now_iso


class CodexAdapter(AgentAdapter):
    name = "codex"

    def __init__(self, mode: str | None = None, api_key: str | None = None) -> None:
        self.mode = (mode or os.environ.get("ANENGOS_CODEX_MODE", "mock")).lower()
        self.api_key = api_key or os.environ.get("ANENGOS_CODEX_API_KEY", "")

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        if not task:
            return AgentResult(False, self.name, "skipped", f"[{self.name}] 任务为空，未执行",
                               error="task 为空")
        if self.mode == "cli":
            return self._run_cli(task, workspace)
        if self.mode == "http":
            return self._run_http(task)
        return self._run_mock(task, workspace)

    # ---------- 模式实现 ----------

    def _run_cli(self, task: str, workspace: str) -> AgentResult:
        t0 = datetime.datetime.now(datetime.timezone.utc)
        try:
            r = subprocess.run(
                ["codex", "exec", "--skip-git-repo-check", task],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("ANENGOS_CODEX_TIMEOUT", "300")),
            )
            out = (r.stdout or "").strip()
            err = (r.stderr or "").strip()
            if r.returncode != 0:
                return AgentResult(False, self.name, "error",
                                   f"[{self.name}/cli] 退出码 {r.returncode}：{err[:400] or out[:400]}",
                                   error=(err or out)[:400],
                                   meta={"mode": "cli", "ts": _now_iso(),
                                         "duration_ms": _elapsed_ms(t0)})
            return AgentResult(True, self.name, "done",
                               f"[{self.name}/cli] 完成：{out[:800]}",
                               meta={"mode": "cli", "ts": _now_iso(),
                                     "duration_ms": _elapsed_ms(t0)})
        except FileNotFoundError:
            return AgentResult(False, self.name, "unconfigured",
                               f"[{self.name}/cli] 未安装 codex CLI（npm i -g @openai/codex）",
                               error="codex CLI 未安装", meta={"mode": "cli", "ts": _now_iso()})
        except subprocess.TimeoutExpired:
            return AgentResult(False, self.name, "timeout",
                               f"[{self.name}/cli] 任务超时",
                               error="timeout", meta={"mode": "cli", "ts": _now_iso()})
        except Exception as e:  # noqa: BLE001
            return AgentResult(False, self.name, "error",
                               f"[{self.name}/cli] 执行失败：{str(e)[:300]}",
                               error=str(e)[:300], meta={"mode": "cli", "ts": _now_iso()})

    def _run_http(self, task: str) -> AgentResult:
        if not self.api_key:
            return AgentResult(False, self.name, "unconfigured",
                               f"[{self.name}/http] 未配置 ANENGOS_CODEX_API_KEY",
                               error="missing api key", meta={"mode": "http", "ts": _now_iso()})
        # OpenAI Codex cloud REST（v1/responses，带 computer 工具）；
        # 国内服务器直连海外通常不可达，失败时给出明确提示。
        try:
            import json
            import urllib.error
            import urllib.request

            payload = {
                "model": "codex-1",
                "tools": [
                    {"type": "computer_use_preview", "display_width": 1024, "display_height": 768}
                ],
                "input": [{"type": "message", "role": "user", "content": task}],
                "store": True,
            }
            req = urllib.request.Request(
                "https://api.openai.com/v1/responses",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self.api_key,
                },
            )
            with urllib.request.urlopen(req, timeout=180) as r:
                return AgentResult(True, self.name, "done",
                                   f"[{self.name}/http] 已提交，HTTP {r.status}",
                                   meta={"mode": "http", "ts": _now_iso(), "http": r.status})
        except urllib.error.HTTPError as e:
            return AgentResult(False, self.name, "error",
                               f"[{self.name}/http] HTTP {e.code}：{e.read().decode(errors='replace')[:300]}",
                               error=f"HTTP {e.code}", meta={"mode": "http", "ts": _now_iso()})
        except Exception as e:  # noqa: BLE001
            return AgentResult(False, self.name, "error",
                               f"[{self.name}/http] 调用失败：{str(e)[:300]}",
                               error=str(e)[:300], meta={"mode": "http", "ts": _now_iso()})

    def _run_mock(self, task: str, workspace: str) -> AgentResult:
        """演示模式：在工作区产出结果文件，模拟 Codex 交付编码产物。"""
        out_dir = Path(workspace) / "codex_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        name = f"codex_{ts}.md"
        content = (
            "# Codex 交付物（演示模式）\n\n"
            f"- 任务：{task}\n"
            "- 状态：已由 Codex(演示) 分析并产出\n"
            "- 说明：ANENGOS_CODEX_MODE=mock；接入 codex CLI 或 API key 后切换为真实执行\n"
        )
        (out_dir / name).write_text(content, encoding="utf-8")
        return AgentResult(
            True, self.name, "done",
            f"[{self.name}/演示] 已产出 {out_dir.name}/{name}（{len(content)} 字符）——切真实模式见文档",
            artifact_paths=[f"{out_dir.name}/{name}"],
            meta={"mode": "mock", "ts": _now_iso()},
        )

    # ---------- 可观测性 ----------

    def health(self) -> dict:
        ready = self.mode == "cli" or (self.mode == "http" and bool(self.api_key)) or self.mode == "mock"
        hint = {
            "cli": "已配置 codex CLI",
            "http": "已配置 ANENGOS_CODEX_API_KEY" if self.api_key else "缺少 ANENGOS_CODEX_API_KEY",
            "mock": "演示模式（未配置真实后端）",
        }.get(self.mode, "未知模式")
        return {"name": self.name, "mode": self.mode, "ready": ready, "hint": hint}

    def describe(self) -> dict:
        return {
            "summary": "把编码/技术任务交给 OpenAI Codex 执行（CLI / cloud REST / 演示）",
            "params": {"task": {"type": "string", "required": True}},
        }


def _elapsed_ms(t0: datetime.datetime) -> int:
    return int((datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds() * 1000)
