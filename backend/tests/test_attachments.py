from backend.ai_summary import apply_bank_override, is_bank_sender
from backend.gmail_service import extract_attachments


CATS = ["Banks", "Promos", "Unclear"]


def test_bank_senders_detected():
    assert is_bank_sender("info@digital.axisbankmail.bank.in")
    assert is_bank_sender("alerts@info6.citi.com")
    assert is_bank_sender("chase@mcmap.chase.com")
    assert is_bank_sender("help@amex.com", "American Express")
    assert not is_bank_sender("news@costco.com", "Costco")
    assert not is_bank_sender("", "")


def test_bank_override_relabels_promos():
    msg = {"sender_email": "citicards@info6.citi.com", "sender_name": "Citi"}
    assert apply_bank_override(msg, "Promos", CATS) == "Banks"
    assert apply_bank_override(msg, "Banks", CATS) == "Banks"
    assert apply_bank_override({"sender_email": "x@y.com"}, "Promos", CATS) == "Promos"
    assert apply_bank_override(msg, "Promos", ["Promos", "Other"]) == "Promos"


def test_bank_promo_content_goes_to_promos():
    from backend.ai_summary import recategorize_message
    offer = {"sender_email": "info@digital.axisbankmail.bank.in", "sender_name": "Axis",
             "subject": "Save more with handpicked offers of the week", "promo": False}
    assert apply_bank_override(offer, "Banks", CATS) == "Promos"
    stmt = {"sender_email": "citicards@info6.citi.com", "sender_name": "Citi",
            "subject": "Your statement is now available online", "promo": False,
            "category": "Promos"}
    assert recategorize_message(stmt, CATS) == "Banks"
    flagged = {"sender_email": "chase@mcmap.chase.com", "subject": "Monthly update",
               "promo": True, "category": "Banks"}
    assert recategorize_message(flagged, CATS) == "Promos"


def test_recategorize_keeps_valid_and_repairs_case():
    from backend.ai_summary import recategorize_message
    assert recategorize_message({"sender_email": "x@y.com", "category": "promos"}, CATS) == "Promos"
    assert recategorize_message({"sender_email": "x@y.com", "category": "Banks"}, CATS) == "Banks"


def test_recategorize_stale_category_uses_llm(monkeypatch):
    import backend.ai_summary as summaries
    from backend.ai_summary import recategorize_message
    monkeypatch.setattr(summaries, "_llm_category_only", lambda msg, cats: "News")
    msg = {"sender_email": "x@y.com", "subject": "hi", "category": "DeletedCat"}
    assert recategorize_message(msg, CATS) == "News"


def _part(filename="", mime="application/pdf", att_id=None, size=10, parts=None):
    return {
        "filename": filename,
        "mimeType": mime,
        "body": {"attachmentId": att_id, "size": size} if att_id else {},
        "parts": parts or [],
    }


def test_extracts_nested_attachments():
    payload = {"parts": [
        _part(),
        _part("a.pdf", att_id="ATT1", size=100),
        _part("", parts=[_part("b.png", "image/png", "ATT2", 200)]),
    ]}
    out = extract_attachments(payload)
    assert [(a["filename"], a["attachmentId"], a["size"]) for a in out] == [
        ("a.pdf", "ATT1", 100), ("b.png", "ATT2", 200)]
    assert out[0]["mimeType"] == "application/pdf"


def test_skips_nameless_and_inline_parts():
    payload = {"parts": [_part(), _part("inline", att_id=None)]}
    assert extract_attachments(payload) == []
    assert extract_attachments({}) == []
