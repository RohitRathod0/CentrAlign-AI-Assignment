"""
WHAT: SQLite storage and business rules for LedgerOps: vendors, invoices and
      the single invoice-validation function shared by the web form and the
      REST API.

WHY:  LedgerOps is both the system the agent changes (via UI) and the system
      the Verifier reads back (via API). One validation function guarantees the
      UI and the API agree on what a valid invoice is (PRD 6.2):
        amount > 0, due_date ISO and not before SEED_DATE,
        invoice_number matches ^[A-Z0-9-]{4,30}$, vendor exists,
        invoice_number unique (duplicate -> separate signal, not a field error).
"""
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "ledgerops.db"
SEED_DATE = date(2026, 10, 3)
INVOICE_NUMBER_RE = re.compile(r"^[A-Z0-9-]{4,30}$")
CURRENCIES = ("INR", "USD")

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (id INTEGER PRIMARY KEY, name TEXT, email TEXT);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY, vendor_id INTEGER, invoice_number TEXT UNIQUE,
    amount REAL, currency TEXT, due_date TEXT, status TEXT, notes TEXT, created_at TEXT);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def list_vendors(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM vendors ORDER BY id").fetchall()]


def _invoice_dict(row) -> dict:
    d = dict(row)
    d["record_id"] = f"LO-{d['id']:04d}"
    return d


INVOICE_SELECT = """SELECT i.*, v.name AS vendor_name FROM invoices i
                    JOIN vendors v ON v.id = i.vendor_id"""


def list_invoices(conn, invoice_number: str = "", vendor_id: int | None = None,
                  status: str = "", due_before: str = "", q: str = "") -> list[dict]:
    clauses, params = [], []
    if invoice_number:
        clauses.append("i.invoice_number = ?")
        params.append(invoice_number)
    if vendor_id:
        clauses.append("i.vendor_id = ?")
        params.append(vendor_id)
    if status:
        clauses.append("i.status = ?")
        params.append(status)
    if due_before:
        clauses.append("i.due_date < ?")
        params.append(due_before)
    if q:
        clauses.append("(i.invoice_number LIKE ? OR v.name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(f"{INVOICE_SELECT} {where} ORDER BY i.id DESC", params).fetchall()
    return [_invoice_dict(r) for r in rows]


def get_invoice(conn, invoice_id: int) -> dict | None:
    row = conn.execute(f"{INVOICE_SELECT} WHERE i.id = ?", (invoice_id,)).fetchone()
    return _invoice_dict(row) if row else None


def find_by_number(conn, invoice_number: str) -> dict | None:
    rows = list_invoices(conn, invoice_number=invoice_number)
    return rows[0] if rows else None


def validate_invoice(conn, raw: dict) -> tuple[dict, dict[str, str]]:
    """Returns (clean values, field errors). Accepts form strings or JSON values."""
    errors: dict[str, str] = {}
    clean: dict = {"notes": str(raw.get("notes") or "").strip()}

    try:
        clean["vendor_id"] = int(raw.get("vendor_id"))
        if not conn.execute("SELECT 1 FROM vendors WHERE id = ?", (clean["vendor_id"],)).fetchone():
            errors["vendor_id"] = "Unknown vendor."
    except (TypeError, ValueError):
        errors["vendor_id"] = "Select a vendor."

    number = str(raw.get("invoice_number") or "").strip()
    clean["invoice_number"] = number
    if not INVOICE_NUMBER_RE.match(number):
        errors["invoice_number"] = "Use 4-30 characters: capital letters, digits and hyphens."

    try:
        clean["amount"] = round(float(str(raw.get("amount", "")).replace(",", "")), 2)
        if clean["amount"] <= 0:
            errors["amount"] = "Amount must be greater than 0."
    except ValueError:
        errors["amount"] = "Enter a number, e.g. 48250.00."

    currency = str(raw.get("currency") or "").strip().upper()
    clean["currency"] = currency
    if currency not in CURRENCIES:
        errors["currency"] = "Currency must be INR or USD."

    try:
        due = date.fromisoformat(str(raw.get("due_date") or "").strip())
        clean["due_date"] = due.isoformat()
        if due < SEED_DATE:
            errors["due_date"] = f"Due date cannot be before {SEED_DATE.isoformat()}."
    except ValueError:
        errors["due_date"] = "Enter a date as YYYY-MM-DD."

    return clean, errors


def create_invoice(conn, clean: dict) -> int:
    cur = conn.execute(
        "INSERT INTO invoices (vendor_id, invoice_number, amount, currency, due_date, status, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'unpaid', ?, ?)",
        (clean["vendor_id"], clean["invoice_number"], clean["amount"], clean["currency"],
         clean["due_date"], clean["notes"],
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    conn.commit()
    return cur.lastrowid


def mark_paid(conn, invoice_id: int) -> None:
    conn.execute("UPDATE invoices SET status = 'paid' WHERE id = ?", (invoice_id,))
    conn.commit()
