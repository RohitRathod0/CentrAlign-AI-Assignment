"""
WHAT: MailDesk, the mock company inbox (port 8001). Browser-only: login,
      paged + searchable inbox, email view, PDF attachments, compose/send/sent.

WHY:  It deliberately has NO JSON API, so the agent must read information the
      way an employee does: through real pages in a real browser (PRD 6.1).
      Forms use stable `id`/`name` attributes and visible labels so both
      Playwright and a human can use them. Chaos fault `slow_inbox` (4 s delay
      on the first /inbox load) tests wait/timeout handling.

Run: uvicorn mock_env.maildesk.app:app --port 8001
"""
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mock_env.common.chaos import ChaosMiddleware, ChaosRule, ChaosState
from mock_env.maildesk import db, seed

USER_EMAIL = "alex@acme-internal.test"
USER_PASSWORD = "demo123"
COOKIE = "md_session"
PER_PAGE = 10

app = FastAPI(title="MailDesk")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
chaos = ChaosState()
app.add_middleware(ChaosMiddleware, state=chaos,
                   rules=[ChaosRule("slow_inbox", "GET", "/inbox", "delay", delay_s=4.0)])
SESSIONS: dict[str, str] = {}
seed.ensure_seeded()


def _user(request: Request) -> str | None:
    return SESSIONS.get(request.cookies.get(COOKIE, ""))


def _to_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=302)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/__admin/reset")
def admin_reset():
    """Re-seed and clear sessions + chaos state. Not exposed to the agent."""
    seed.reset()
    SESSIONS.clear()
    chaos.reset()
    return {"ok": True}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/inbox", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None, "email": ""})


@app.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form("")):
    if email.strip().lower() != USER_EMAIL or password != USER_PASSWORD:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password.", "email": email},
            status_code=401)
    token = secrets.token_hex(16)
    SESSIONS[token] = USER_EMAIL
    resp = RedirectResponse("/inbox", status_code=302)
    resp.set_cookie(COOKIE, token, httponly=True)
    return resp


@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, q: str = "", page: int = 1):
    if not _user(request):
        return _to_login()
    conn = db.connect()
    emails, total = db.list_emails(conn, q=q.strip(), page=max(page, 1), per_page=PER_PAGE)
    conn.close()
    pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    return templates.TemplateResponse(request, "inbox.html", {
        "emails": emails, "q": q, "page": page, "pages": pages, "total": total})


@app.get("/email/{email_id}", response_class=HTMLResponse)
def email_view(request: Request, email_id: int):
    if not _user(request):
        return _to_login()
    conn = db.connect()
    email, attachments = db.get_email(conn, email_id)
    conn.close()
    if not email:
        return HTMLResponse("<h1>Email not found</h1>", status_code=404)
    return templates.TemplateResponse(request, "email.html",
                                      {"email": email, "attachments": attachments})


@app.get("/attachments/{attachment_id}")
def attachment(request: Request, attachment_id: int):
    if not _user(request):
        return _to_login()
    conn = db.connect()
    att = db.get_attachment(conn, attachment_id)
    conn.close()
    if not att:
        return HTMLResponse("<h1>Attachment not found</h1>", status_code=404)
    return FileResponse(att["path"], media_type="application/pdf", filename=att["filename"])


@app.get("/compose", response_class=HTMLResponse)
def compose(request: Request):
    if not _user(request):
        return _to_login()
    return templates.TemplateResponse(request, "compose.html", {})


@app.post("/send")
def send(request: Request, to: str = Form(""), subject: str = Form(""), body: str = Form("")):
    if not _user(request):
        return _to_login()
    conn = db.connect()
    db.add_sent(conn, to, subject, body)
    conn.close()
    return RedirectResponse("/sent?ok=1", status_code=302)


@app.get("/sent", response_class=HTMLResponse)
def sent(request: Request, ok: int = 0):
    if not _user(request):
        return _to_login()
    conn = db.connect()
    messages = db.list_sent(conn)
    conn.close()
    return templates.TemplateResponse(request, "sent.html", {"messages": messages, "ok": ok})
