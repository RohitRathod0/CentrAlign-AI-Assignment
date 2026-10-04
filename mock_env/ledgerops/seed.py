"""
WHAT: Seeds LedgerOps with the 4 vendors and 5 existing invoices from PRD 6.2.
      Run directly (`python -m mock_env.ledgerops.seed`) or via `reset()` from
      the app's `/__admin/reset`.

WHY:  INV-2026-1004 is already in the ledger on purpose: asking the agent to
      enter Acme's September invoice must hit duplicate detection instead of
      creating a second record. The other rows make the list look realistic.
"""
from mock_env.ledgerops import db

VENDORS = [
    (1, "Acme Corp", "billing@acme.test"),
    (2, "Acme Logistics", "billing@acmelogistics.test"),
    (3, "Globex Ltd", "accounts@globex.test"),
    (4, "Initech", "billing@initech.test"),
]

# (vendor_id, invoice_number, amount, due_date, status)
INVOICES = [
    (1, "INV-2026-0912", 41300.00, "2026-09-29", "paid"),
    (3, "GLX-5401", 16000.00, "2026-09-20", "paid"),
    (4, "INIT-2026-051", 120000.00, "2026-08-31", "paid"),
    (2, "INV-AL-290", 9900.00, "2026-09-15", "paid"),
    (1, "INV-2026-1004", 44750.00, "2026-10-30", "unpaid"),
]


def reset() -> None:
    conn = db.connect()
    conn.executescript("DELETE FROM invoices; DELETE FROM vendors;")
    conn.executemany("INSERT INTO vendors (id, name, email) VALUES (?, ?, ?)", VENDORS)
    for vendor_id, number, amount, due, status in INVOICES:
        conn.execute(
            "INSERT INTO invoices (vendor_id, invoice_number, amount, currency, due_date, status, notes, created_at) "
            "VALUES (?, ?, ?, 'INR', ?, ?, 'Seed data', '2026-09-30T09:00:00Z')",
            (vendor_id, number, amount, due, status))
    conn.commit()
    conn.close()


def ensure_seeded() -> None:
    conn = db.connect()
    empty = conn.execute("SELECT COUNT(*) FROM vendors").fetchone()[0] == 0
    conn.close()
    if empty:
        reset()


if __name__ == "__main__":
    reset()
    print(f"LedgerOps seeded: {len(VENDORS)} vendors, {len(INVOICES)} invoices -> {db.DB_PATH}")
