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
    """mock 模型 + 真实治理：文件写入走异步审批，挂起后批准并续跑完成。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockModelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    monkeypatch.setenv("ANENGOS_API_KEY", "fake-key")
    monkeypatch.setenv("ANENGOS_BASE_URL", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("ANENGOS_MODEL", "mock-model")
    # 显式指向 tmp 工作区（模块 import 时 BASE 已固化，不能用 ANENGOS_HOME 事后改）
    monkeypatch.setattr(app, "BASE", tmp_path)
    monkeypatch.setattr(app, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(app, "AUDIT_FILE", tmp_path / "audit" / "audit.jsonl")
    monkeypatch.setattr(app, "ADMIN_HTML", tmp_path / "admin.html")
    monkeypatch.setattr(app, "_APPROVALS", None)
    monkeypatch.setattr(app, "_TOOLS", None)
    monkeypatch.setattr(app, "_TASKS", {})
    monkeypatch.setattr(app, "_REVIEWER", None)

    agent, approvals, err = app.build_agent()
    assert err == ""
    # 第一轮：模型调 file.write -> 挂起（不继续循环）
    session = agent.run("写一个 hello 文件")
    assert session.paused is True
    assert session.output is None
    assert len(approvals.pending()) == 1
    assert approvals.pending()[0].tool == "file.write"
    # simulate-first：未批准前副作用不真实生效
    assert not (tmp_path / "workspace" / "hello.txt").exists()

    # 批准后自动续跑：注入真实结果，模型结束回合，输出完成总结
    req = approvals.pending()[0]
    approvals.approve(req.request_id)
    result = agent.tools.run(req.tool, req.args)
    session = agent.resume(session, [{"id": req.request_id, "content": result}])
    assert session.paused is False
    assert "任务完成" in (session.output or "")
    assert (tmp_path / "workspace" / "hello.txt").exists()
    server.shutdown()
