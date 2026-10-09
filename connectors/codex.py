"""OpenAI Codex 适配器：多智能体总装线第一个外部智能体。

三模式可插拔（ANENGOS_CODEX_MODE，默认 mock）：
  - cli   本机/开发机装了 codex CLI：subprocess 调 `codex exec`，产出真实结果
  - http  OpenAI Codex REST（需 ANENGOS_CODEX_API_KEY 且网络可达海外）
  - mock  确定性演示：在工作区产出结果文件，用于服务器/CI 验证整条
          指挥 -> 审批 -> 真实执行 -> 审计 链路

治理语义：codex.submit 视为有副作用的外部执行，必须经 Gatekeeper 审批
（simulate-first），批准后由审批系统真正调用 submit；产出再交由监督智能体互审。
"""

from __future__ import annotations

import datetime
import os
import subprocess
from pathlib import Path

from connectors.base import AgentAdapter


class CodexAdapter(AgentAdapter):
    name = "codex"

    def __init__(self, mode: str | None = None, api_key: str | None = None) -> None:
        self.mode = (mode or os.environ.get("ANENGOS_CODEX_MODE", "mock")).lower()
        self.api_key = api_key or os.environ.get("ANENGOS_CODEX_API_KEY", "")

    def submit(self, task: str, workspace: str) -> str:
        task = (task or "").strip()
        if not task:
            return "[codex] 任务为空，未执行"
        if self.mode == "cli":
            return self._run_cli(task, workspace)
        if self.mode == "http":
            return self._run_http(task)
        return self._run_mock(task, workspace)

    # ---------- 模式实现 ----------

    def _run_cli(self, task: str, workspace: str) -> str:
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
                return f"[codex/cli] 退出码 {r.returncode}：{err[:400] or out[:400]}"
            return f"[codex/cli] 完成：{out[:800]}"
        except FileNotFoundError:
            return "[codex/cli] 未安装 codex CLI（npm i -g @openai/codex）"
        except subprocess.TimeoutExpired:
            return "[codex/cli] 任务超时"
        except Exception as e:  # noqa: BLE001
            return f"[codex/cli] 执行失败：{str(e)[:300]}"

    def _run_http(self, task: str) -> str:
        if not self.api_key:
            return "[codex/http] 未配置 ANENGOS_CODEX_API_KEY"
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
                return f"[codex/http] 已提交，HTTP {r.status}"
        except urllib.error.HTTPError as e:
            return f"[codex/http] HTTP {e.code}：{e.read().decode(errors='replace')[:300]}"
        except Exception as e:  # noqa: BLE001
            return f"[codex/http] 调用失败：{str(e)[:300]}"

    def _run_mock(self, task: str, workspace: str) -> str:
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
        return f"[codex/演示] 已产出 {out_dir.name}/{name}（{len(content)} 字符）——切真实模式见文档"
