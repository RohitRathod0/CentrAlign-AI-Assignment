"""
WHAT: SQLite storage for MailDesk: inbox emails, their PDF attachments and
      messages sent from the compose form.

WHY:  MailDesk is the "source of information" app (PRD 6.1). Keeping data in a
      real database (not hard-coded HTML) lets search, paging and sending
      behave like a real inbox, and lets `/__admin/reset` restore a known state
      for tests and eval runs.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "maildesk.db"
ATTACHMENTS_DIR = DATA_DIR / "attachments"

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY, from_name TEXT, from_email TEXT, to_email TEXT,
    subject TEXT, body TEXT, sent_at TEXT);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY, email_id INTEGER, filename TEXT, path TEXT);
CREATE TABLE IF NOT EXISTS sent (
    id INTEGER PRIMARY KEY, to_email TEXT, subject TEXT, body TEXT, sent_at TEXT);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def list_emails(conn, q: str = "", page: int = 1, per_page: int = 10) -> tuple[list[dict], int]:
    where, params = "", []
    if q:
        where = "WHERE from_name LIKE ? OR from_email LIKE ? OR subject LIKE ?"
        params = [f"%{q}%"] * 3
    total = conn.execute(f"SELECT COUNT(*) FROM emails {where}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT e.*, (SELECT COUNT(*) FROM attachments a WHERE a.email_id = e.id) AS n_attach
            FROM emails e {where} ORDER BY sent_at DESC LIMIT ? OFFSET ?""",
        params + [per_page, (page - 1) * per_page]).fetchall()
    return [dict(r) for r in rows], total


def get_email(conn, email_id: int) -> tuple[dict | None, list[dict]]:
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    atts = conn.execute("SELECT * FROM attachments WHERE email_id = ?", (email_id,)).fetchall()
    return (dict(row) if row else None), [dict(a) for a in atts]


def get_attachment(conn, attachment_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    return dict(row) if row else None


def add_sent(conn, to_email: str, subject: str, body: str) -> None:
    conn.execute("INSERT INTO sent (to_email, subject, body, sent_at) VALUES (?, ?, ?, ?)",
                 (to_email, subject, body, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    conn.commit()


def list_sent(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM sent ORDER BY id DESC").fetchall()]
