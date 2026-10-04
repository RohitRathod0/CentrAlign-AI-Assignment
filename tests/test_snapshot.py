"""
WHAT: The DOM snapshot (real headless Chromium) indexes interactive elements,
      skips hidden ones, keeps text in reading order, reports alerts and open
      dialogs, and records what a submit button would submit.

WHY:  The snapshot is the agent's only view of a page (PRD D2). If it hides a
      modal or mislabels a field, every later decision is wrong.
"""
import pytest

from agent.browser.session import BrowserSession

HTML = """
<h1>New invoice</h1>
<p role="alert">Amount: must be greater than 0</p>
<form method="post" action="http://localhost:8002/invoices">
  <label for="v">Vendor</label>
  <select id="v" name="vendor_id"><option value="">--</option><option value="1" selected>Acme Corp</option></select>
  <label for="a">Amount</label><input id="a" name="amount" value="48250.00">
  <input type="hidden" name="csrf" value="secret">
  <button type="submit">Save invoice</button>
</form>
<div style="display:none"><button>Invisible</button></div>
<table><tr><td>INV-1</td><td><a href="/invoices/1">LO-0001</a></td></tr></table>
<div role="dialog" aria-modal="true">Announcement <button type="button">Dismiss</button></div>
"""


@pytest.fixture
async def session(tmp_path):
    browser = BrowserSession(headless=True, chaos=False, artifacts_dir=tmp_path)
    yield browser
    await browser.close()


async def test_snapshot_indexes_and_describes_page(session):
    page = await session.page()
    await page.set_content(HTML)
    raw = await session.snapshot()
    text = session.last_snapshot

    labels = {e["text"] for e in session.elements.values()}
    assert labels == {"Vendor", "Amount", "Save invoice", "LO-0001", "Dismiss"}   # hidden ones skipped
    assert '<select name="vendor_id" label="Vendor" selected="Acme Corp"' in text
    assert "INV-1 | [" in text                                                    # table row kept as one line
    assert "MODAL DIALOG IS OPEN" in text and session.dialog_open
    assert raw["alerts"] == ["Amount: must be greater than 0"]

    submit = next(e for e in session.elements.values() if e.get("is_submit"))
    assert submit["form_method"] == "POST" and submit["form_action"] == "http://localhost:8002/invoices"
    assert submit["form_values"] == {"vendor_id": "1", "amount": "48250.00", "csrf": "secret"}

    dismiss = next(i for i, e in session.elements.items() if e["text"] == "Dismiss")
    assert await page.locator(f'[data-wi="{dismiss}"]').count() == 1           # index resolves to the element
