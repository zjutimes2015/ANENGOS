"""IMA 知识库连接器：检索腾讯 IMA 个人知识库，供量化决策作为外部知识上下文（RAG 闭环）。

后端：腾讯 IMA OpenAPI（https://ima.qq.com）
  - 凭证：环境变量 IMA_CLIENT_ID / IMA_API_KEY（或 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY）
    → 兜底读取 ~/.config/ima/client_id + api_key（chmod 600）
  - 凭证只发往 ima.qq.com，不进日志、不进产物。

任务解析（自然语言）：
  "列出我的知识库" / "有哪些知识库"        -> 列出全部知识库
  "浏览 交易 知识库"                       -> 浏览指定知识库内容
  "在 交易 库搜索 趋势"                    -> 库内搜索
  "搜索 量化策略"（未指定库）              -> 全库搜索（个人库）
  "读取 <标题>" / "原文"                   -> 读取匹配文档的原文信息

输出：知识库总览 + 命中内容摘要，写入 workspace/quant_ima_output/。
任何报告都注入当前调用时的知识库检索结果 → 让"外部知识"进入决策上下文。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso

_IMA_BASE = "https://ima.qq.com"
_IMA_CTX = "skill_version=1.1.10"
_SKILL_DIR_HINT = "~/.agents/skills/ima-skill（ima_api.cjs）"


def _load_credentials() -> tuple[str, str] | None:
    """按优先级读凭证：环境变量 → ~/.config/ima/ 配置文件。"""
    cid = (os.environ.get("IMA_CLIENT_ID") or os.environ.get("IMA_OPENAPI_CLIENTID") or "").strip()
    key = (os.environ.get("IMA_API_KEY") or os.environ.get("IMA_OPENAPI_APIKEY") or "").strip()
    if cid and key:
        return cid, key

    conf = Path.home() / ".config" / "ima"
    cid_f, key_f = conf / "client_id", conf / "api_key"
    try:
        if cid_f.exists() and key_f.exists():
            c = cid_f.read_text(encoding="utf-8", errors="replace").strip()
            k = key_f.read_text(encoding="utf-8", errors="replace").strip()
            if c and k:
                return c, k
    except OSError:
        pass
    return None


def _ima_post(api_path: str, body: dict[str, Any]) -> dict[str, Any]:
    """统一 POST 调用 IMA OpenAPI；凭证缺失或业务错误时抛异常。"""
    creds = _load_credentials()
    if not creds:
        raise RuntimeError("IMA 凭证未配置（环境变量 IMA_CLIENT_ID/IMA_API_KEY 或 ~/.config/ima/ 文件）")
    cid, key = creds

    req = urllib.request.Request(
        f"{_IMA_BASE}/{api_path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "ima-openapi-clientid": cid,
            "ima-openapi-apikey": key,
            "ima-openapi-ctx": _IMA_CTX,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(raw)
            raise RuntimeError(f"IMA HTTP {e.code}: {err.get('msg') or err.get('message') or raw[:200]}")
        except ValueError:
            raise RuntimeError(f"IMA HTTP {e.code}: {raw[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"IMA 网络错误: {e}")

    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError(f"IMA 返回非 JSON: {raw[:200]}")

    if data.get("code") != 0:
        raise RuntimeError(f"IMA 业务错误: {data.get('msg') or raw[:200]}")
    return data.get("data") or {}


# ---------- 供其他 connector 复用的 RAG 检索 ----------

def retrieve_ima_knowledge(query: str, max_items: int = 5, max_kbs: int = 6) -> str:
    """在个人知识库中检索 query，返回压缩后的知识上下文（供决策报告注入）。

    全库检索（只搜自己创建的库），取前 max_items 条命中。
    凭证未配置或全失败时返回空字符串（不阻断主流程）。
    """
    try:
        kbs = _ima_post("openapi/wiki/v1/search_knowledge_base", {"query": "", "cursor": "", "limit": 20})
    except Exception:  # noqa: BLE001
        return ""
    info_list = kbs.get("info_list") or []
    mine = [kb for kb in info_list if kb.get("role_type") == "创建者"][:max_kbs]

    hits: list[str] = []
    seen: set[str] = set()
    # 拆词搜索：IMA 中文知识库按词匹配，整句 query 命中率低
    keywords = [w.strip() for w in re.split(r"[\s,，、;；]+", query) if len(w.strip()) >= 2]
    if not keywords:
        keywords = [query]
    for kb in mine:
        for kw in keywords[:4]:
            try:
                resp = _ima_post("openapi/wiki/v1/search_knowledge", {"query": kw, "knowledge_base_id": kb.get("kb_id", ""), "cursor": ""})
            except Exception:  # noqa: BLE001
                continue
            for item in (resp.get("info_list") or [])[:max_items]:
                title = item.get("title") or item.get("name") or ""
                if title and title not in seen:
                    seen.add(title)
                    hits.append(f"- [{kb.get('kb_name', '')}] {title}")
    if not hits:
        return ""
    return "## IMA 知识库命中（外部知识，决策时参考）\n" + "\n".join(hits)


# ---------- Adapter ----------

class QuantIMAAdapter(AgentAdapter):
    name = "quant_ima"

    def __init__(self) -> None:
        self.mode = "ima" if _load_credentials() else "unconfigured"

    def _list_kbs(self) -> list[dict[str, Any]]:
        data = _ima_post("openapi/wiki/v1/search_knowledge_base", {"query": "", "cursor": "", "limit": 20})
        return data.get("info_list") or []

    def _find_kb_id(self, name_kw: str) -> str | None:
        for kb in self._list_kbs():
            if name_kw and name_kw in (kb.get("kb_name") or ""):
                return kb.get("kb_id")
        return None

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        out_dir = Path(workspace) / "quant_ima_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now()
        path = out_dir / f"ima_{now.strftime('%Y%m%d_%H%M%S')}.md"

        if self.mode == "unconfigured":
            content = (
                f"# IMA 知识库（未配置凭证）\n\n- 任务：{task}\n- 时间：{_now_iso()}\n\n"
                f"凭证需配置环境变量 IMA_CLIENT_ID / IMA_API_KEY，或 ~/.config/ima/ 文件。\n"
            )
            path.write_text(content, encoding="utf-8")
            return AgentResult(
                False, self.name, "unconfigured",
                f"[{self.name}/unconfigured] IMA 凭证未配置，跳过知识库检索",
                artifact_paths=[str(path.relative_to(workspace))],
            )

        try:
            # 1) 知识库总览（总是输出）
            kbs = self._list_kbs()
            mine = [kb for kb in kbs if kb.get("role_type") == "创建者"]
            lines = [f"# IMA 知识库检索报告\n\n- 任务：{task}\n- 时间：{_now_iso()}\n\n## 我的知识库（{len(mine)}）\n"]
            for kb in mine:
                lines.append(
                    f"- {kb.get('kb_name')}（{kb.get('content_count', 0)} 条内容）"
                    f"{' - ' + str(kb.get('description')) if kb.get('description') else ''}"
                )
            sub_kbs = [kb for kb in kbs if kb.get("role_type") != "创建者"]
            if sub_kbs:
                lines.append(f"\n## 订阅/共享知识库（{len(sub_kbs)}）\n")
                for kb in sub_kbs:
                    lines.append(f"- {kb.get('kb_name')}（{kb.get('content_count', 0)} 条内容）")

            # 2) 解析意图
            browse_kb = None
            search_query = None
            m = re.search(r"(?:浏览|查看|打开)(?:知识库)?[\"“' ]*([\u4e00-\u9fa5A-Za-z0-9]+)", task)
            if m:
                browse_kb = m.group(1)
            m2 = re.search(r"(?:搜索|查找|检索|查询)[:：\s\"“'']+([^\n]{1,60})", task)
            if m2:
                search_query = m2.group(1).strip().strip('"“”\'')

            if not browse_kb and not search_query:
                # 无明确子任务 → 全库检索任务文本里最像关键词的部分
                kw = re.sub(r"(?:列出|有哪些|我的|知识库|浏览|搜索|查找|检索|查询|请|帮我|一下)", "", task)
                kw = kw.strip()
                if kw:
                    search_query = kw

            # 3) 浏览指定知识库
            if browse_kb:
                kb_id = self._find_kb_id(browse_kb)
                if kb_id:
                    data = _ima_post("openapi/wiki/v1/get_knowledge_list", {"knowledge_base_id": kb_id, "cursor": "", "limit": 20})
                    items = data.get("knowledge_list") or []
                    lines.append(f"\n## 知识库「{browse_kb}」内容（{len(items)} 条）\n")
                    for it in items:
                        lines.append(f"- {it.get('title') or it.get('name') or '(无标题)'}")

            # 4) 库内/全库搜索
            if search_query:
                lines.append(f"\n## 搜索「{search_query}」命中\n")
                if browse_kb:
                    kb_id = self._find_kb_id(browse_kb)
                    kbs2 = [k for k in mine + sub_kbs if k.get("kb_id") == kb_id] if kb_id else mine
                else:
                    kbs2 = mine[:6]
                keywords = [w.strip() for w in re.split(r"[\s,，、;；]+", search_query) if len(w.strip()) >= 2] or [search_query]
                found = 0
                seen_titles: set[str] = set()
                for kb in kbs2:
                    kb_id = kb.get("kb_id", "")
                    kb_name = kb.get("kb_name", "")
                    for kw in keywords[:4]:
                        try:
                            resp = _ima_post("openapi/wiki/v1/search_knowledge", {"query": kw, "knowledge_base_id": kb_id, "cursor": ""})
                        except RuntimeError:
                            continue
                        for item in (resp.get("info_list") or [])[:10]:
                            title = item.get("title") or item.get("name") or "(无标题)"
                            if title in seen_titles:
                                continue
                            seen_titles.add(title)
                            lines.append(f"- [{kb_name}] {title}")
                            found += 1
                if found == 0:
                    lines.append("（无命中，试试其他关键词或浏览知识库内容）")

            content = "\n".join(lines) + "\n"
            path.write_text(content, encoding="utf-8")
            return AgentResult(
                True, self.name, "done",
                f"[{self.name}/ima] 知识库检索完成：{len(mine)} 个个人库 + 命中摘要已写入报告",
                artifact_paths=[str(path.relative_to(workspace))],
            )
        except Exception as e:  # noqa: BLE001
            content = f"# IMA 知识库检索（失败）\n\n- 任务：{task}\n- 错误：{e}\n- 时间：{_now_iso()}\n"
            path.write_text(content, encoding="utf-8")
            return AgentResult(
                False, self.name, "error",
                f"[{self.name}/ima] 检索失败：{e}",
                error=str(e),
                artifact_paths=[str(path.relative_to(workspace))],
            )

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": self.mode != "unconfigured", "mode": self.mode,
                "hint": "IMA 凭证已配置" if self.mode != "unconfigured" else "未配置 IMA 凭证（环境变量或 ~/.config/ima/）"}

    def describe(self) -> dict[str, Any]:
        return {
            "summary": "腾讯 IMA 个人知识库检索：列出知识库 / 浏览内容 / 搜索文档，输出命中摘要供决策参考",
            "params": {"task": {"type": "string", "description": "例如：列出我的知识库；在 交易 库搜索 趋势；搜索 量化策略"}},
        }
