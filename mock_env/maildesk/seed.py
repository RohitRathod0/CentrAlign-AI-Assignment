"""
WHAT: Seeds MailDesk with the 12 emails from PRD 6.1 and generates their PDF
      invoices with reportlab. Run directly (`python -m mock_env.maildesk.seed`)
      or via `reset()` from the app's `/__admin/reset`.

WHY:  The seed is the ground truth for every demo scenario, and each email is
      there for a reason:
        - Acme Corp x3 invoices    -> "latest" must be picked by date (INV-2026-1047)
        - Acme Logistics invoice   -> "Acme" alone is ambiguous -> clarification
        - Globex invoice in body   -> a different extraction path (no PDF)
        - Initech 2,40,000 INR     -> above approval threshold -> approval gate
        - 6 noise mails, incl. Acme marketing without invoice -> distractors
      Amounts use Indian digit grouping (2,40,000.00) like real Indian invoices.
"""
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from mock_env.maildesk import db

INBOX_OWNER = "alex@acme-internal.test"

# (sent_at, from_name, from_email, subject, body, invoice-for-pdf or None)
EMAILS = [
    ("2026-08-30 09:12", "Acme Corp", "billing@acme.test",
     "Invoice INV-2026-0912 - August services",
     "Hi Alex,\n\nPlease find attached our invoice for August.\n\nThanks,\nAcme Corp Billing",
     {"vendor": "Acme Corp", "number": "INV-2026-0912", "date": "2026-08-30", "due": "2026-09-29",
      "items": [("Platform subscription (Aug)", "30,000.00"), ("Support hours (10)", "11,300.00")],
      "total": "41,300.00"}),
    ("2026-09-04 10:00", "Priya Nair", "priya.nair@acme-internal.test",
     "Team offsite - agenda",
     "Hi all,\n\nAgenda for the offsite is attached in the calendar invite. See you there!\n\nPriya",
     None),
    ("2026-09-15 08:00", "FinTech Weekly", "news@fintechweekly.test",
     "This week in payments",
     "Top stories: UPI volumes hit a new record; RBI updates card tokenisation rules.",
     None),
    ("2026-09-22 17:30", "IT Helpdesk", "it@acme-internal.test",
     "Scheduled maintenance this Saturday",
     "LedgerOps and MailDesk will be briefly unavailable on Saturday 06:00-07:00 IST.",
     None),
    ("2026-09-24 12:40", "Initech", "billing@initech.test",
     "Invoice INIT-2026-077 - annual license",
     "Dear customer,\n\nAttached is the invoice for your annual enterprise license.\n\nInitech Accounts",
     {"vendor": "Initech", "number": "INIT-2026-077", "date": "2026-09-24", "due": "2026-11-15",
      "items": [("Enterprise license - annual", "2,40,000.00")], "total": "2,40,000.00"}),
    ("2026-09-26 15:05", "Globex Ltd", "accounts@globex.test",
     "Invoice GLX-5520 for consulting",
     "Hello,\n\nPlease find our invoice details below (no attachment).\n\n"
     "Invoice No: GLX-5520\nInvoice Date: 2026-09-26\nDue Date: 2026-10-25\n"
     "Description: Data migration consulting\nTotal Amount Due: INR 18,400.00\n\n"
     "Regards,\nGlobex Ltd Accounts",
     None),
    ("2026-09-27 09:00", "Acme Corp", "news@acme.test",
     "Acme's autumn product launch",
     "Discover what's new at Acme this season! Webinars, demos and early-bird offers inside.",
     None),
    ("2026-09-29 11:15", "Acme Logistics", "billing@acmelogistics.test",
     "Invoice INV-AL-311 - freight charges",
     "Hello,\n\nAttached is our invoice for September freight.\n\nAcme Logistics",
     {"vendor": "Acme Logistics", "number": "INV-AL-311", "date": "2026-09-29", "due": "2026-10-20",
      "items": [("Freight - Mumbai to Pune", "9,500.00"), ("Handling", "3,300.00")],
      "total": "12,800.00"}),
    ("2026-09-30 10:05", "Acme Corp", "billing@acme.test",
     "Invoice INV-2026-1004 - September services",
     "Hi Alex,\n\nPlease find attached our invoice for September.\n\nThanks,\nAcme Corp Billing",
     {"vendor": "Acme Corp", "number": "INV-2026-1004", "date": "2026-09-30", "due": "2026-10-30",
      "items": [("Platform subscription (Sep)", "30,000.00"), ("Support hours (13)", "14,750.00")],
      "total": "44,750.00"}),
    ("2026-10-01 11:20", "Acme Corp", "billing@acme.test",
     "Invoice INV-2026-1047 - October",
     "Hi Alex,\n\nPlease find attached our latest invoice.\n\nThanks,\nAcme Corp Billing",
     {"vendor": "Acme Corp", "number": "INV-2026-1047", "date": "2026-10-01", "due": "2026-10-31",
      "items": [("Platform subscription (Oct)", "30,000.00"),
                ("Implementation support (16 hrs)", "18,250.00")],
      "total": "48,250.00"}),
    ("2026-10-02 09:30", "Rahul Mehta", "rahul.mehta@acme-internal.test",
     "Invite: Q3 close review - Mon 10:00",
     "You are invited to the Q3 close review on Monday at 10:00 in Room 4B.",
     None),
    ("2026-10-02 16:45", "HR Team", "hr@acme-internal.test",
     "Diwali holiday schedule",
     "The office will be closed on the Diwali holidays. Enjoy the festival!",
     None),
]


def _write_invoice_pdf(path, inv: dict) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    _, height = A4
    y = height - 72
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, y, inv["vendor"])
    y -= 22
    c.setFont("Helvetica", 11)
    c.drawString(72, y, "TAX INVOICE")
    y -= 30
    for label, value in (("Invoice No", inv["number"]), ("Invoice Date", inv["date"]),
                         ("Due Date", inv["due"]), ("Bill To", "Acme Internal Pvt Ltd")):
        c.drawString(72, y, f"{label}: {value}")
        y -= 16
    y -= 14
    c.setFont("Helvetica-Bold", 11)
    c.drawString(72, y, "Description")
    c.drawRightString(520, y, "Amount (INR)")
    c.setFont("Helvetica", 11)
    for desc, amount in inv["items"]:
        y -= 16
        c.drawString(72, y, desc)
        c.drawRightString(520, y, amount)
    y -= 28
    c.setFont("Helvetica-Bold", 12)
    c.drawString(72, y, "Total Amount Due")
    c.drawRightString(520, y, f"INR {inv['total']}")
    c.showPage()
    c.save()


def reset() -> None:
    conn = db.connect()
    conn.executescript("DELETE FROM emails; DELETE FROM attachments; DELETE FROM sent;")
    db.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    for sent_at, from_name, from_email, subject, body, invoice in EMAILS:
        cur = conn.execute(
            "INSERT INTO emails (from_name, from_email, to_email, subject, body, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (from_name, from_email, INBOX_OWNER, subject, body, sent_at))
        if invoice:
            filename = f"{invoice['number']}.pdf"
            path = db.ATTACHMENTS_DIR / filename
            _write_invoice_pdf(path, invoice)
            conn.execute("INSERT INTO attachments (email_id, filename, path) VALUES (?, ?, ?)",
                         (cur.lastrowid, filename, str(path)))
    conn.commit()
    conn.close()


def ensure_seeded() -> None:
    conn = db.connect()
    empty = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0
    conn.close()
    if empty:
        reset()


if __name__ == "__main__":
    reset()
    print(f"MailDesk seeded: {len(EMAILS)} emails -> {db.DB_PATH}")
