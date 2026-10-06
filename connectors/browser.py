"""浏览器「手」：Chromium + Playwright 驱动，接入 Gatekeeper 治理。

- 内核：Chromium（github.com/chromium/chromium，Chrome/Edge/Opera 及国产浏览器的开源底层）
- 驱动：Playwright（默认）或 MockDriver（测试/离线），可插拔
- 治理：域名级「介绍」白名单——agent 只能访问被用户介绍的域名；
        所有浏览动作写审计日志；点击等有副作用的动作走异步审批（simulate-first）

长出手，但手也受「刹车」管辖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from governance.capability import CapabilityRegistry


@dataclass
class PageState:
    url: str
    title: str
    text: str


class BrowserDriver(Protocol):
    def open(self, url: str) -> PageState: ...
    def extract(self, selector: str | None = None) -> str: ...
    def click(self, selector: str) -> PageState: ...
    def close(self) -> None: ...


class MockBrowserDriver:
    """内存页面模拟器：本地验证与测试用，不真上网。"""

    def __init__(self) -> None:
        self.pages: dict[str, str] = {
            "https://example.com": "Example Domain\nThis domain is for use in illustrative examples.",
        }
        self.current: str | None = None
        self.clicks: list[str] = []

    def open(self, url: str) -> PageState:
        self.current = url
        body = self.pages.get(url, f"页面 {url} 未收录于 Mock 页面库")
        return PageState(url=url, title=urlparse(url).netloc, text=body)

    def extract(self, selector: str | None = None) -> str:
        if self.current is None:
            return "（尚未打开页面）"
        return self.pages.get(self.current, "（空）")

    def click(self, selector: str) -> PageState:
        self.clicks.append(selector)
        return PageState(url=self.current or "about:blank", title="after-click", text=f"已点击 {selector}")

    def close(self) -> None:
        self.current = None


class PlaywrightBrowserDriver:
    """真实 Chromium 驱动：pip install 'anengos[playwright]' && playwright install chromium 后可用。"""

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._sync = None
        self._browser = None
        self._page = None

    def _ensure(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright  # 延迟导入，未装则报清晰错误

        self._sync = sync_playwright().start()
        self._browser = self._sync.chromium.launch(headless=self.headless)
        self._page = self._browser.new_page()

    def open(self, url: str) -> PageState:
        self._ensure()
        self._page.goto(url, timeout=30_000)
        return PageState(
            url=self._page.url,
            title=self._page.title(),
            text=self._page.inner_text("body")[:4000],
        )

    def extract(self, selector: str | None = None) -> str:
        self._ensure()
        if selector:
            return self._page.inner_text(selector)
        return self._page.inner_text("body")

    def click(self, selector: str) -> PageState:
        self._ensure()
        self._page.click(selector, timeout=10_000)
        self._page.wait_for_load_state()
        return PageState(
            url=self._page.url,
            title=self._page.title(),
            text=self._page.inner_text("body")[:4000],
        )

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._sync is not None:
            self._sync.stop()


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


class BrowserTools:
    """浏览器工具集：域名级能力授权 + 审计 + 副作用审批，注册进 ToolRegistry。"""

    def __init__(
        self,
        driver: BrowserDriver,
        registry: CapabilityRegistry,
        service: str = "browser",
    ) -> None:
        self.driver = driver
        self.registry = registry
        self.service = service

    def check_url(self, actor: str, tool: str, url: str) -> tuple[bool, str]:
        """域名必须出现在该 actor 被介绍的 scope 里（逗号分隔，支持 *.sub 前缀匹配）。"""
        cap = self.registry.resolve(actor, tool)
        if cap is None:
            return False, f"{actor} 未介绍使用 {tool}"
        allowed = {d.strip().lower() for d in cap.scope.split(",") if d.strip()}
        host = _domain(url)
        for rule in allowed:
            if rule.startswith("*.") and host.endswith(rule[1:]):
                return True, ""
            if host == rule or host.endswith("." + rule):
                return True, ""
        return False, f"域名 {host} 不在介绍白名单（{cap.scope}）内"

    def make_scope_checker(self, actor_holder):
        """生成给 AgentOS 的 pre_execute 钩子：带 url 的浏览器动作先过域名白名单。"""

        def check(tool: str, args: dict[str, Any]) -> tuple[bool, str]:
            if not tool.startswith("browser."):
                return True, ""
            url = args.get("url", "")
            if not url:
                return True, ""
            return self.check_url(actor_holder(), tool, url)

        return check

    def register(self, reg, actor_holder) -> None:
        """把浏览器工具注册进 ToolRegistry。

        actor_holder 是一个返回当前 actor 名字的可调用对象（如 lambda: session.actor）。
        """
        def _open(args: dict[str, Any]) -> str:
            url = args["url"]
            ok, reason = self.check_url(actor_holder(), "browser.open", url)
            if not ok:
                return f"[拒绝] {reason}"
            state = self.driver.open(url)
            return f"[{state.title}]\n{state.text[:800]}"

        def _extract(args: dict[str, Any]) -> str:
            url = args.get("url", "")
            if url:
                ok, reason = self.check_url(actor_holder(), "browser.extract", url)
                if not ok:
                    return f"[拒绝] {reason}"
                self.driver.open(url)
            return self.driver.extract(args.get("selector"))

        def _click(args: dict[str, Any]) -> str:
            # 点击可能触发购买、提交等副作用：由 Gatekeeper 层按 side_effect 走异步审批
            state = self.driver.click(args["selector"])
            return f"[{state.title}] 点击后：{state.text[:400]}"

        reg.register("browser.open", _open)
        reg.register("browser.extract", _extract)
        reg.register("browser.click", _click)
