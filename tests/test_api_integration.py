"""端到端集成测试：mock 模型服务器 + app.build_agent 真实链路。

验证「真实模型工具调用 → 治理放行/审批 → 输出」全链路，不依赖外网。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import app


class _MockModelHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        last = body["messages"][-1]
        if last["role"] == "user":
            msg = {
                "role": "assistant",
                "content": "我来写",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "file.write",
                        "arguments": json.dumps({"path": "hello.txt", "content": "hello anengos"}),
                    },
                }],
            }
        else:
            msg = {"role": "assistant", "content": "任务完成：文件已写入并审计。"}
        payload = json.dumps({"choices": [{"message": msg}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


def test_end_to_end_llm_tool_governance(tmp_path, monkeypatch):
    """mock 模型 + 真实治理：文件写入走异步审批，会话正常结束。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockModelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    monkeypatch.setenv("ANENGOS_API_KEY", "fake-key")
    monkeypatch.setenv("ANENGOS_BASE_URL", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("ANENGOS_MODEL", "mock-model")
    monkeypatch.setenv("ANENGOS_HOME", str(tmp_path))

    agent, approvals, err = app.build_agent()
    assert err == ""
    session = agent.run("写一个 hello 文件")
    assert "任务完成" in (session.output or "")
    assert session.blocked_reason is None
    assert len(approvals.pending()) == 1
    assert approvals.pending()[0].tool == "file.write"
    # simulate-first：未批准前副作用不真实生效
    assert not (tmp_path / "workspace" / "hello.txt").exists()
    server.shutdown()
