"""
WHAT: LedgerOps, the mock internal finance system (port 8002).
      - Web UI (cookie `lo_session`): invoices list/new/detail, mark-paid, vendors.
      - REST API (header `X-API-Key`): /api/vendors, /api/invoices[/{id}].
      - Chaos faults (PRD 6.3): submit_503, session_expiry, blocking_modal,
        stale_list.

WHY:  It is the system the agent changes through the browser and the system
      the Verifier independently reads back through the API (PRD D5), so the
      two channels must share one database and one validation function.
      Fault placement:
        submit_503      -> middleware, first POST /invoices returns a 503 page
        session_expiry  -> 6th cookie-authenticated request drops the session
        blocking_modal  -> overlay on /invoices/new that intercepts clicks
        stale_list      -> first list read after a create omits the newest row
                           (UI list or API list, whichever is read first)

Run: uvicorn mock_env.ledgerops.app:app --port 8002
"""
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mock_env.common.chaos import ChaosMiddleware, ChaosRule, ChaosState
from mock_env.ledgerops import db, seed

USER_EMAIL = "finance@acme-internal.test"
USER_PASSWORD = "ledger123"
COOKIE = "lo_session"
SESSION_EXPIRY_AT = 6

app = FastAPI(title="LedgerOps")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
chaos = ChaosState()
app.add_middleware(ChaosMiddleware, state=chaos,
                   rules=[ChaosRule("submit_503", "POST", "/invoices", "503")])
SESSIONS: dict[str, str] = {}
seed.ensure_seeded()


@app.middleware("http")
async def no_store(request: Request, call_next):
    """Finance pages must not be served from the browser cache (Back re-fetches them)."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def _api_key() -> str:
    return os.getenv("LEDGER_API_KEY", "dev-ledger-key")


def _auth(request: Request) -> str | RedirectResponse:
    """Session user, or a redirect to login (plain, or `?expired=1` for chaos expiry)."""
    token = request.cookies.get(COOKIE, "")
    user = SESSIONS.get(token)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if chaos.tick("auth_requests", request) == SESSION_EXPIRY_AT and chaos.fire("session_expiry", request):
        SESSIONS.pop(token, None)
        return RedirectResponse("/login?expired=1", status_code=302)
    return user


def _api_denied(request: Request) -> JSONResponse | None:
    if request.headers.get("x-api-key") != _api_key():
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


def _drop_newest_if_stale(request: Request, invoices: list[dict]) -> list[dict]:
    if invoices and chaos.fire_armed("stale_list", request):
        newest = max(i["id"] for i in invoices)
        return [i for i in invoices if i["id"] != newest]
    return invoices


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
    return RedirectResponse("/invoices", status_code=302)


# ----------------------------------------------------------------- UI -----

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, expired: int = 0):
    return templates.TemplateResponse(request, "login.html",
                                      {"error": None, "expired": expired, "email": ""})


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    email, password = str(form.get("email", "")), str(form.get("password", ""))
    if email.strip().lower() != USER_EMAIL or password != USER_PASSWORD:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Invalid email or password.", "expired": 0, "email": email}, status_code=401)
    token = secrets.token_hex(16)
    SESSIONS[token] = USER_EMAIL
    resp = RedirectResponse("/invoices", status_code=302)
    resp.set_cookie(COOKIE, token, httponly=True)
    return resp


@app.get("/invoices", response_class=HTMLResponse)
def invoices_page(request: Request, status: str = "", vendor_id: int | None = None, q: str = ""):
    user = _auth(request)
    if isinstance(user, RedirectResponse):
        return user
    conn = db.connect()
    rows = db.list_invoices(conn, status=status, vendor_id=vendor_id, q=q.strip())
    conn.close()
    rows = _drop_newest_if_stale(request, rows)
    return templates.TemplateResponse(request, "invoices.html",
                                      {"invoices": rows, "status": status, "q": q})


def _render_new(request: Request, values: dict, errors: dict, duplicate: dict | None,
                show_modal: bool = False, status_code: int = 200):
    conn = db.connect()
    vendors = db.list_vendors(conn)
    conn.close()
    return templates.TemplateResponse(request, "invoice_new.html", {
        "vendors": vendors, "values": values, "errors": errors, "duplicate": duplicate,
        "currencies": db.CURRENCIES, "show_modal": show_modal}, status_code=status_code)


@app.get("/invoices/new", response_class=HTMLResponse)
def invoice_new(request: Request):
    user = _auth(request)
    if isinstance(user, RedirectResponse):
        return user
    return _render_new(request, {"currency": "INR"}, {}, None,
                       show_modal=chaos.fire("blocking_modal", request))


@app.post("/invoices")
async def invoice_create(request: Request):
    user = _auth(request)
    if isinstance(user, RedirectResponse):
        return user
    form = dict(await request.form())
    conn = db.connect()
    clean, errors = db.validate_invoice(conn, form)
    existing = None if errors else db.find_by_number(conn, clean["invoice_number"])
    if errors or existing:
        conn.close()
        return _render_new(request, form, errors, existing)
    invoice_id = db.create_invoice(conn, clean)
    conn.close()
    chaos.arm("stale_list", request)
    return RedirectResponse(f"/invoices/{invoice_id}?created=1", status_code=302)


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: int, created: int = 0, paid: int = 0):
    user = _auth(request)
    if isinstance(user, RedirectResponse):
        return user
    conn = db.connect()
    invoice = db.get_invoice(conn, invoice_id)
    conn.close()
    if not invoice:
        return HTMLResponse("<h1>Invoice not found</h1>", status_code=404)
    return templates.TemplateResponse(request, "invoice_detail.html",
                                      {"invoice": invoice, "created": created, "paid": paid})


@app.post("/invoices/{invoice_id}/mark-paid")
def invoice_mark_paid(request: Request, invoice_id: int):
    user = _auth(request)
    if isinstance(user, RedirectResponse):
        return user
    conn = db.connect()
    db.mark_paid(conn, invoice_id)
    conn.close()
    return RedirectResponse(f"/invoices/{invoice_id}?paid=1", status_code=302)


@app.get("/vendors", response_class=HTMLResponse)
def vendors_page(request: Request):
    user = _auth(request)
    if isinstance(user, RedirectResponse):
        return user
    conn = db.connect()
    vendors = db.list_vendors(conn)
    conn.close()
    return templates.TemplateResponse(request, "vendors.html", {"vendors": vendors})


# ---------------------------------------------------------------- API -----

@app.get("/api/vendors")
def api_vendors(request: Request):
    if denied := _api_denied(request):
        return denied
    conn = db.connect()
    vendors = db.list_vendors(conn)
    conn.close()
    return vendors


@app.get("/api/invoices")
def api_invoices(request: Request, invoice_number: str = "", vendor_id: int | None = None,
                 status: str = "", due_before: str = ""):
    if denied := _api_denied(request):
        return denied
    conn = db.connect()
    rows = db.list_invoices(conn, invoice_number=invoice_number, vendor_id=vendor_id,
                            status=status, due_before=due_before)
    conn.close()
    return _drop_newest_if_stale(request, rows)


@app.get("/api/invoices/{invoice_id}")
def api_invoice(request: Request, invoice_id: int):
    if denied := _api_denied(request):
        return denied
    conn = db.connect()
    invoice = db.get_invoice(conn, invoice_id)
    conn.close()
    if not invoice:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return invoice


@app.post("/api/invoices")
async def api_invoice_create(request: Request):
    if denied := _api_denied(request):
        return denied
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "validation", "fields": {"body": "Invalid JSON."}}, status_code=422)
    conn = db.connect()
    clean, errors = db.validate_invoice(conn, body if isinstance(body, dict) else {})
    if errors:
        conn.close()
        return JSONResponse({"error": "validation", "fields": errors}, status_code=422)
    existing = db.find_by_number(conn, clean["invoice_number"])
    if existing:
        conn.close()
        return JSONResponse({"error": "duplicate", "existing_id": existing["id"]}, status_code=409)
    invoice_id = db.create_invoice(conn, clean)
    invoice = db.get_invoice(conn, invoice_id)
    conn.close()
    chaos.arm("stale_list", request)
    return JSONResponse(invoice, status_code=201)
