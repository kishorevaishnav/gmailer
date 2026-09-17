import base64

from backend.gmail_service import _extract_text


def _part(mime, text, filename=""):
    return {"mimeType": mime, "filename": filename,
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}


STUB = "Hello,\n\nYour email software can't display HTML emails. Unsubscribe here."
RICH_HTML = "<html><body><h1>Makers Market</h1><p>" + "Fresh local goods. " * 60 + "</p></body></html>"


def test_rich_html_beats_stub_plain():
    payload = {"parts": [_part("text/plain", STUB), _part("text/html", RICH_HTML)]}
    out = _extract_text(payload)
    assert "Makers Market" in out
    assert "can't display HTML" not in out


def test_plain_kept_when_comparable():
    html = "<html><body><p>" + STUB + " extra words here</p></body></html>"
    payload = {"parts": [_part("text/plain", STUB), _part("text/html", html)]}
    assert _extract_text(payload) == STUB


def test_html_only_single_part_stripped():
    payload = {"mimeType": "text/html", "body": _part("text/html", RICH_HTML)["body"]}
    out = _extract_text(payload)
    assert "Makers Market" in out
    assert "<h1>" not in out


def test_plain_only_unchanged():
    payload = {"parts": [_part("text/plain", STUB)]}
    assert _extract_text(payload) == STUB
