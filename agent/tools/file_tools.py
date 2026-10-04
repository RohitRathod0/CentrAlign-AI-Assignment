"""
WHAT: files_read_pdf: download a PDF (e.g. an email attachment) and return its
      text. The file is saved under artifacts/runs/<run_id>/downloads/ as
      evidence.

WHY:  Invoices arrive as PDFs. The download goes through the browser
      context's request API, so it reuses the browser's login cookies: the
      agent reads the attachment exactly as the signed-in employee would, with
      no separate credentials. Text extraction uses pdfplumber (PRD 4). If the
      app answers with an HTML page instead (usually the login page), the tool
      says so, so the agent knows to sign in first.
"""
import re
from urllib.parse import urljoin, urlparse

import pdfplumber

from agent import config
from agent.errors import classify
from agent.models import ErrorKind, Observation, RiskLevel, ToolError
from agent.tools.base import Tool, ToolContext


def _filename(content_disposition: str, url: str) -> str:
    match = re.search(r'filename="?([^";]+)"?', content_disposition or "")
    name = match.group(1) if match else (urlparse(url).path.rsplit("/", 1)[-1] or "download")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name if name.lower().endswith(".pdf") else name + ".pdf"


async def files_read_pdf(args: dict, ctx: ToolContext) -> Observation:
    page = await ctx.browser.page()
    url = urljoin(page.url, args["url"]) if page.url.startswith("http") else args["url"]
    context = await ctx.browser.context()
    resp = await context.request.get(url, timeout=config.NAV_TIMEOUT_MS)
    body = await resp.body()
    if not resp.ok:
        error = classify(http_status=resp.status, body=body[:200].decode(errors="replace"))
        return Observation(ok=False, summary=f"Download of {url} failed: {error.message}", error=error, url=url)
    if not body.startswith(b"%PDF"):
        msg = (f"{url} returned {resp.headers.get('content-type', 'unknown content')}, not a PDF "
               "(if it is a login page, sign in to the app in the browser first).")
        return Observation(ok=False, summary=msg, url=url,
                           error=ToolError(kind=ErrorKind.BAD_ARGS, message=msg, retryable=False))

    downloads = ctx.artifacts_dir / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    path = downloads / _filename(resp.headers.get("content-disposition", ""), url)
    path.write_bytes(body)
    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages).strip()
        n_pages = len(pdf.pages)
    rel = path.relative_to(config.ARTIFACTS_DIR).as_posix() if path.is_relative_to(config.ARTIFACTS_DIR) \
        else path.as_posix()
    return Observation(ok=True, url=url, data={"text": text[:6000], "file": rel},
                       summary=f"Read PDF {path.name} ({n_pages} page(s), saved as {rel}). Text:\n{text}")


TOOLS = [
    Tool("files_read_pdf", "Download a PDF from a company app (e.g. an email attachment link) and return its "
         "text. Uses the browser's login session.",
         {"type": "object", "properties": {"url": {"type": "string", "description": "Absolute or relative URL"}},
          "required": ["url"]},
         RiskLevel.READ, files_read_pdf),
]
