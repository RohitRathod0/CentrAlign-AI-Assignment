"""
WHAT: Generic browser tools: browser_goto, browser_click, browser_type,
      browser_select, browser_snapshot. Each one acts, waits for the page to
      settle, re-snapshots the DOM, saves a screenshot and classifies the
      outcome into an Observation.

WHY:  These are the agent's hands (PRD "Execution"). Two details matter for
      reliability:
        1. Every action ends with a fresh snapshot, so the next LLM step always
           sees the real current page (modal opened, error shown, redirected
           to login...) instead of what it expected.
        2. Form submit + HTTP 5xx: a browser left on a "503" page has lost the
           form. Like a person pressing Back, the click tool returns to the
           form, restores the values it had, and reports TRANSIENT together
           with `retry_args` (the submit button's index on the restored page),
           so the orchestrator can retry the identical submit after a backoff
           without spending an LLM call.
      Page-level errors are only taken from alerts that appear after a
      navigation, so typing on a page that already shows an error is not
      reported as a new failure.
"""
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError

from agent import config
from agent.browser.snapshot import RESTORE_FORM_JS, element_label
from agent.errors import classify
from agent.models import ErrorKind, Observation, RiskLevel, ToolError
from agent.tools.base import Tool, ToolContext


async def _observe(ctx: ToolContext, summary: str, *, error: ToolError | None = None,
                   navigated: bool = False, http_status: int | None = None,
                   prev_url: str | None = None, target_url: str | None = None) -> Observation:
    page = await ctx.browser.page()
    raw = await ctx.browser.snapshot()
    shot = await ctx.browser.screenshot(f"step-{ctx.step:03d}")
    if error is None:
        error = classify(http_status=http_status if navigated else None, url=page.url,
                         prev_url=prev_url, target_url=target_url,
                         alerts=raw["alerts"] if navigated else ())
    summary += f" Now on {page.url} (\"{raw.get('title', '')}\")."
    if error:
        summary += f" ERROR [{error.kind.value}]: {error.message}"
    return Observation(ok=error is None, summary=summary, error=error, url=page.url, screenshot=shot)


def _locator(page, index: int):
    return page.locator(f'[data-wi="{index}"]')


async def _missing(ctx: ToolContext, index: int) -> Observation:
    return await _observe(ctx, f"There is no element [{index}] on the current page.",
                          error=ToolError(kind=ErrorKind.ELEMENT_NOT_FOUND, retryable=False,
                                          message=f"index {index} not in the latest snapshot"))


async def browser_goto(args: dict, ctx: ToolContext) -> Observation:
    page = await ctx.browser.page()
    prev_url = page.url
    url = urljoin(prev_url, args["url"]) if prev_url.startswith("http") else args["url"]
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
    except PlaywrightError as exc:
        return await _observe(ctx, f"Could not open {url}.", error=classify(exc=exc))
    return await _observe(ctx, f"Opened {url}.", navigated=True,
                          http_status=response.status if response else None,
                          prev_url=prev_url, target_url=url)


async def browser_click(args: dict, ctx: ToolContext) -> Observation:
    index = args["index"]
    page = await ctx.browser.page()
    info = ctx.browser.elements.get(index)
    target = _locator(page, index)
    if await target.count() == 0:
        return await _missing(ctx, index)
    prev_url, marks = page.url, ctx.browser.marks()
    try:
        await target.click(timeout=config.ACTION_TIMEOUT_MS)
    except PlaywrightError as exc:
        return await _observe(ctx, f"Click on {element_label(info)} failed.", error=classify(exc=exc))
    if not await ctx.browser.settle(marks):
        return await _observe(ctx, f"Clicked {element_label(info)} but the next page did not load in time.",
                              error=ToolError(kind=ErrorKind.TRANSIENT, retryable=True,
                                              message="navigation timed out"))
    navigated = ctx.browser.nav_requests > marks[0]
    status = ctx.browser.last_nav_status if navigated else None

    if info and info.get("is_submit") and status and status >= 500:
        error = classify(http_status=status, body="server error page after form submit")
        await page.go_back(wait_until="domcontentloaded", timeout=config.NAV_TIMEOUT_MS)
        await page.evaluate(RESTORE_FORM_JS, info.get("form_values") or {})
        obs = await _observe(ctx, f"Clicked {element_label(info)}: the server answered HTTP {status} "
                                  f"(temporarily unavailable). Went back to the form and restored the "
                                  f"entered values.", error=error)
        same = [i for i, e in ctx.browser.elements.items()
                if e.get("is_submit") and e.get("text") == info.get("text") and e.get("name") == info.get("name")]
        if same and not ctx.browser.dialog_open:     # only an unobstructed identical submit is safe to replay
            obs.data["retry_args"] = {"index": same[0]}
        return obs
    return await _observe(ctx, f"Clicked {element_label(info)}.", navigated=navigated,
                          http_status=status, prev_url=prev_url)


async def browser_type(args: dict, ctx: ToolContext) -> Observation:
    index, text = args["index"], args["text"]
    page = await ctx.browser.page()
    info = ctx.browser.elements.get(index)
    target = _locator(page, index)
    if await target.count() == 0:
        return await _missing(ctx, index)
    try:
        await target.fill(text, timeout=config.ACTION_TIMEOUT_MS)
    except PlaywrightError as exc:
        return await _observe(ctx, f"Typing into {element_label(info)} failed.", error=classify(exc=exc))
    shown = "***" if info and info.get("type") == "password" else text
    return await _observe(ctx, f"Typed \"{shown}\" into {element_label(info)}.")


async def browser_select(args: dict, ctx: ToolContext) -> Observation:
    index, option = args["index"], args["option"]
    page = await ctx.browser.page()
    info = ctx.browser.elements.get(index)
    target = _locator(page, index)
    if await target.count() == 0:
        return await _missing(ctx, index)
    options = (info or {}).get("options", [])
    try:
        if option in options:
            await target.select_option(label=option, timeout=config.ACTION_TIMEOUT_MS)
        else:
            await target.select_option(value=option, timeout=config.ACTION_TIMEOUT_MS)
    except PlaywrightError as exc:
        error = classify(exc=exc)
        if "did not find some options" in str(exc):
            error = ToolError(kind=ErrorKind.BAD_ARGS, retryable=False,
                              message=f"no option '{option}'. Options are: {options}")
        return await _observe(ctx, f"Selecting in {element_label(info)} failed.", error=error)
    return await _observe(ctx, f"Selected \"{option}\" in {element_label(info)}.")


async def browser_snapshot(args: dict, ctx: ToolContext) -> Observation:
    page = await ctx.browser.page()
    wait = min(max(float(args.get("wait_seconds") or 0), 0), 10)
    if wait:
        await page.wait_for_timeout(wait * 1000)
    return await _observe(ctx, f"Re-read the page{f' after waiting {wait:g}s' if wait else ''}.")


TOOLS = [
    Tool("browser_goto", "Open a URL in the browser (company apps only). Do not use it for PDF files; "
         "use files_read_pdf for those.",
         {"type": "object", "properties": {"url": {"type": "string", "description": "Absolute or relative URL"}},
          "required": ["url"]},
         RiskLevel.READ, browser_goto),
    Tool("browser_click", "Click the element with this [index] from the CURRENT PAGE snapshot "
         "(links, buttons, submit buttons).",
         {"type": "object", "properties": {"index": {"type": "integer"}}, "required": ["index"]},
         RiskLevel.WRITE, browser_click),
    Tool("browser_type", "Replace the content of the input/textarea [index] with text. Dates as YYYY-MM-DD.",
         {"type": "object", "properties": {"index": {"type": "integer"}, "text": {"type": "string"}},
          "required": ["index", "text"]},
         RiskLevel.WRITE, browser_type),
    Tool("browser_select", "Choose an option (by its visible label) in the <select> [index].",
         {"type": "object", "properties": {"index": {"type": "integer"}, "option": {"type": "string"}},
          "required": ["index", "option"]},
         RiskLevel.WRITE, browser_select),
    Tool("browser_snapshot", "Re-read the current page, optionally after waiting (for slow pages).",
         {"type": "object", "properties": {"wait_seconds": {"type": "number", "description": "0-10"}},
          "required": []},
         RiskLevel.READ, browser_snapshot),
]
