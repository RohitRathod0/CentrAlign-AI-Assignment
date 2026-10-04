"""
WHAT: MailDesk behaviour: login, newest-first inbox, search, paging, email
      view, PDF attachment content, compose/send.

WHY:  The seed encodes every scenario (latest Acme invoice, Acme Logistics
      ambiguity, Globex invoice in the body, Initech above threshold). These
      tests pin that ground truth so a seed change cannot silently break the
      demo.
"""
import io

import pdfplumber


def test_wrong_password_shows_alert(maildesk):
    resp = maildesk.post("/login", data={"email": "alex@acme-internal.test", "password": "nope"})
    assert resp.status_code == 401 and 'role="alert"' in resp.text


def test_inbox_newest_first_and_paged(maildesk_user):
    page1 = maildesk_user.get("/inbox").text
    assert "12 message(s)" in page1 and "Page 1 of 2" in page1
    assert page1.index("Diwali holiday schedule") < page1.index("Invoice INV-2026-1047")


def test_search_by_sender(maildesk_user):
    text = maildesk_user.get("/inbox", params={"q": "Acme"}).text
    # 4 Acme Corp + 1 Acme Logistics + 4 internal senders @acme-internal.test
    assert "9 message(s)" in text
    assert "INV-AL-311" in text and "INV-2026-1047" in text


def test_latest_acme_pdf_contents(maildesk_user):
    email = maildesk_user.get("/email/10").text
    assert "INV-2026-1047.pdf" in email
    pdf = maildesk_user.get("/attachments/5")
    assert pdf.headers["content-type"] == "application/pdf"
    with pdfplumber.open(io.BytesIO(pdf.content)) as doc:
        text = doc.pages[0].extract_text()
    assert "INV-2026-1047" in text and "48,250.00" in text and "2026-10-31" in text


def test_globex_invoice_is_in_body(maildesk_user):
    text = maildesk_user.get("/email/6").text
    assert "Total Amount Due: INR 18,400.00" in text and "Attachments" not in text


def test_send_and_sent(maildesk_user):
    resp = maildesk_user.post("/send", data={"to": "billing@acme.test", "subject": "Hi", "body": "Thanks"})
    assert "Message sent." in resp.text and "billing@acme.test" in resp.text
