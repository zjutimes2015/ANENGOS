"""MCP 端点测试：租户 token 鉴权 + JSON-RPC 握手 + 4 个工具全链路（协议级）。"""
import json

from test_auth import _setup
from test_knowledge import _mk_tenant, _upload
from test_quota import _request


def _mcp(url, method, tid, token, params=None, req_id=1):
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    return _request(url + "/mcp", payload, method="POST", token=token)


def test_mcp_handshake_and_tools(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        tid, token = _mk_tenant(url)
        # initialize：协议握手
        code, d = _mcp(url, "initialize", tid, token,
                       {"protocolVersion": "2025-06-18",
                        "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        assert code == 200
        assert d["result"]["protocolVersion"] == "2025-06-18"
        assert d["result"]["serverInfo"]["name"] == "anengos"
        assert "tools" in d["result"]["capabilities"]
        # 通知无需响应
        code, d = _request(url + "/mcp",
                           {"jsonrpc": "2.0", "method": "notifications/initialized"}, method="POST", token=token)
        assert code == 202
        # ping
        code, d = _mcp(url, "ping", tid, token)
        assert code == 200 and d["result"] == {}
        # tools/list：4 个工具
        code, d = _mcp(url, "tools/list", tid, token)
        assert code == 200
        names = [t["name"] for t in d["result"]["tools"]]
        assert names == ["knowledge_list", "knowledge_search", "knowledge_ask", "knowledge_upload"]
        # 未知方法 -> JSON-RPC 错误
        code, d = _mcp(url, "bogus/method", tid, token)
        assert code == 200 and d["error"]["code"] == -32601
    finally:
        srv.shutdown()


def test_mcp_auth_required(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        tid, token = _mk_tenant(url)
        # 无 token / 错误 token -> 401
        code, d = _request(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, method="POST")
        assert code == 401
        code, d = _request(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           method="POST", token="bad-token")
        assert code == 401
        # 管理员 token 不能走 MCP（与客户站一致）
        code, d = _request(url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           method="POST", token="admin123")
        assert code == 401
        # 缺少 method -> JSON-RPC 层错误（Method not found）
        code, d = _request(url + "/mcp", {"jsonrpc": "2.0", "id": 1}, method="POST", token=token)
        assert code == 200 and d["error"]["code"] == -32601
        # 非法 JSON -> 400 parse error
        import urllib.request
        r = urllib.request.Request(url + "/mcp", data=b"not json at all",
                                   headers={"Content-Type": "application/json",
                                            "Authorization": "Bearer " + token}, method="POST")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                body = json.loads(resp.read().decode())
                assert resp.status == 400 and body["error"]["code"] == -32700
        except urllib.error.HTTPError as e:
            assert e.code == 400 and json.loads(e.read().decode())["error"]["code"] == -32700
    finally:
        srv.shutdown()


def test_mcp_tools_full_flow(monkeypatch, tmp_path):
    srv, url = _setup(monkeypatch, tmp_path)
    try:
        tid, token = _mk_tenant(url)
        # 先用管理 API 上传资料（隔离验证：MCP 只操作自己的租户）
        _upload(url, tid, token, "MCP报价.txt", "MCP 接入套餐：标准版 1999 元/月，含语义检索。")
        # knowledge_list
        code, d = _mcp(url, "tools/call", tid, token, {"name": "knowledge_list", "arguments": {}})
        assert code == 200 and not d["result"]["isError"]
        assert "MCP报价.txt" in d["result"]["content"][0]["text"]
        # knowledge_search
        code, d = _mcp(url, "tools/call", tid, token,
                       {"name": "knowledge_search", "arguments": {"query": "1999"}})
        assert code == 200 and not d["result"]["isError"]
        assert "MCP报价.txt" in d["result"]["content"][0]["text"]
        # knowledge_ask：检索链路必须工作（模型不可达时降级为来源提示，均视为通过）
        code, d = _mcp(url, "tools/call", tid, token,
                       {"name": "knowledge_ask", "arguments": {"query": "标准版多少钱"}})
        assert code == 200
        text = d["result"]["content"][0]["text"]
        assert "MCP报价.txt" in text or "来源" in text or "检索到资料但模型不可用" in text
        # knowledge_upload：MCP 上传 -> 列表可见
        code, d = _mcp(url, "tools/call", tid, token,
                       {"name": "knowledge_upload",
                        "arguments": {"filename": "MCP新增.txt", "content": "通过 MCP 上传的补充说明，包含关键词 789xyz。"}})
        assert code == 200 and not d["result"]["isError"] and "上传成功" in d["result"]["content"][0]["text"]
        code, d = _mcp(url, "tools/call", tid, token, {"name": "knowledge_list", "arguments": {}})
        assert "MCP新增.txt" in d["result"]["content"][0]["text"]
        # 缺参 -> isError
        code, d = _mcp(url, "tools/call", tid, token,
                       {"name": "knowledge_search", "arguments": {}})
        assert d["result"]["isError"] is True
        # 未知工具 -> isError
        code, d = _mcp(url, "tools/call", tid, token, {"name": "nope", "arguments": {}})
        assert d["result"]["isError"] is True and "Unknown tool" in d["result"]["content"][0]["text"]
        # 租户隔离：第二个租户看不到第一个租户的资料
        tid_b, tok_b = _mk_tenant(url)
        code, d = _mcp(url, "tools/call", tid_b, tok_b, {"name": "knowledge_list", "arguments": {}})
        assert "MCP报价.txt" not in d["result"]["content"][0]["text"]
    finally:
        srv.shutdown()
