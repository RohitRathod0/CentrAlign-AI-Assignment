"""
WHAT: BrowserSession: one Chromium context per run (PRD 3). It starts lazily on
      the first browser/PDF tool call, tracks main-frame navigations, takes the
      indexed DOM snapshot after every action, saves screenshots as evidence and
      closes with the run.

WHY:  - Lazy start: runs that never touch the browser (tests, API-only tasks)
        do not pay for launching Chromium.
      - Navigation tracking: a click may or may not navigate, and a server can
        take seconds to answer (chaos `slow_inbox`). Counting navigation
        requests and DOMContentLoaded events lets `settle()` wait exactly as
        long as a page change actually takes, and records the final HTTP
        status of the document (needed to notice a 503 after a form submit).
      - Chaos runs: the `X-Chaos: 1` header is added to every request of this
        context, which switches failure injection on in the mock apps for this
        run only.
      - `elements` (from the latest snapshot) is what the policy engine reads
        to understand what a click on [index] would submit.
"""
import asyncio
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from agent import config
from agent.browser.snapshot import SNAPSHOT_JS, format_snapshot


class BrowserSession:
    def __init__(self, headless: bool, chaos: bool, artifacts_dir: Path):
        self.headless = headless
        self.chaos = chaos
        self.artifacts_dir = artifacts_dir
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.elements: dict[int, dict] = {}
        self.last_snapshot = ""
        self.dialog_open = False
        self.nav_requests = 0
        self.dcl_events = 0
        self.last_nav_status: int | None = None

    async def page(self) -> Page:
        if self._page is None:
            await self._start()
        return self._page

    async def context(self) -> BrowserContext:
        await self.page()
        return self._context

    async def _start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        headers = {"X-Chaos": "1"} if self.chaos else {}
        self._context = await self._browser.new_context(viewport={"width": 1280, "height": 860},
                                                        extra_http_headers=headers)
        self._context.set_default_timeout(config.ACTION_TIMEOUT_MS)
        self._context.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
        page = await self._context.new_page()
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("domcontentloaded", self._on_dcl)
        self._page = page

    def _on_request(self, request) -> None:
        if request.is_navigation_request() and request.frame == self._page.main_frame:
            self.nav_requests += 1

    def _on_response(self, response) -> None:
        if response.request.is_navigation_request() and response.frame == self._page.main_frame:
            self.last_nav_status = response.status

    def _on_dcl(self, _page) -> None:
        self.dcl_events += 1

    def marks(self) -> tuple[int, int]:
        """Counters to pass to settle() after an action that may navigate."""
        return self.nav_requests, self.dcl_events

    async def settle(self, marks: tuple[int, int]) -> bool:
        """Wait for a navigation started after `marks` to load. Returns False on timeout."""
        nav_before, dcl_before = marks
        await asyncio.sleep(0.25)
        if self.nav_requests == nav_before:
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + config.NAV_TIMEOUT_MS / 1000
        while self.dcl_events == dcl_before:
            if loop.time() > deadline:
                return False
            await asyncio.sleep(0.1)
        try:
            await self._page.wait_for_load_state("load", timeout=5_000)
        except Exception:
            pass                 # DOM is ready; a slow sub-resource must not fail the step
        return True

    async def snapshot(self) -> dict:
        raw = await self._page.evaluate(SNAPSHOT_JS)
        self.elements = {int(k): v for k, v in raw.get("elements", {}).items()}
        self.dialog_open = bool(raw.get("dialog"))
        self.last_snapshot = format_snapshot(raw)
        return raw

    async def screenshot(self, name: str) -> str | None:
        """Save a PNG under the run's artifacts folder; returns a path relative to artifacts/."""
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            path = self.artifacts_dir / f"{name}.png"
            await self._page.screenshot(path=str(path))
            if path.is_relative_to(config.ARTIFACTS_DIR):
                return path.relative_to(config.ARTIFACTS_DIR).as_posix()
            return path.as_posix()
        except Exception:
            return None          # evidence is best-effort; never fail an action over it

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        if self._pw is not None:
            await self._pw.stop()
        self._pw = self._browser = self._context = self._page = None
