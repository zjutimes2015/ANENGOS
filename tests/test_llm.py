"""LLM 接入测试：消息格式转换与响应解析（不真调 API）。"""

from __future__ import annotations

from kernel.llm import build_chat_request, parse_openai_message, to_openai_messages


def _session_messages():
    return [
        {"role": "user", "content": "写文件"},
        {"role": "assistant", "content": {
            "stop_reason": "tool_use",
            "text": "我来写",
            "content": [{"type": "tool_use", "id": "call_1", "name": "file.write",
                         "input": {"path": "a.txt", "content": "hi"}}],
        }},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1",
                                      "content": "已写入 a.txt"}]},
    ]


def test_to_openai_messages_converts_roles():
    """user/assistant/tool 三角色转换正确，tool_calls 带 arguments JSON。"""
    out = to_openai_messages(_session_messages())
    assert out[0] == {"role": "user", "content": "写文件"}
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == "我来写"
    assert out[1]["tool_calls"][0]["function"]["name"] == "file.write"
    assert '"a.txt"' in out[1]["tool_calls"][0]["function"]["arguments"]
    assert out[2] == {"role": "tool", "tool_call_id": "call_1", "content": "已写入 a.txt"}


def test_build_chat_request_has_tools():
    req = build_chat_request(_session_messages(), [{"name": "file.write", "description": "x"}], "m1")
    assert req["model"] == "m1"
    assert req["tool_choice"] == "auto"
    assert req["tools"][0]["type"] == "function"
    assert req["tools"][0]["function"]["name"] == "file.write"
    assert "parameters" in req["tools"][0]["function"]


def test_parse_openai_tool_call():
    resp = parse_openai_message({
        "content": "好的",
        "tool_calls": [{
            "id": "c2",
            "type": "function",
            "function": {"name": "file.read", "arguments": '{"path": "a.txt"}'},
        }],
    })
    assert resp["stop_reason"] == "tool_use"
    assert resp["content"][0]["name"] == "file.read"
    assert resp["content"][0]["input"] == {"path": "a.txt"}
    assert resp["text"] == "好的"


def test_parse_openai_end_turn():
    resp = parse_openai_message({"content": "完成了"})
    assert resp["stop_reason"] == "end_turn"
    assert resp["text"] == "完成了"
    assert resp["content"] == []
