from backend import store
from backend.ai_summary import recategorize_message, sender_mapped_category
from backend.gmail_service import extract_attachments


CATS = ["Banks", "Promos", "Unclear"]


def _with_cats(*names):
    for n in names:
        store.add_category(n)
    return store.get_categories()


def test_sender_map_bank_and_promo_split():
    cats = _with_cats("Banks", "Promos")
    assert sender_mapped_category(
        {"sender_email": "citicards@info6.citi.com", "subject": "Your statement"}, cats) == "Banks"
    assert sender_mapped_category(
        {"sender_email": "info@digital.axisbankmail.bank.in",
         "subject": "Save more with handpicked offers of the week"}, cats) == "Promos"
    assert sender_mapped_category(
        {"sender_email": "chase@mcmap.chase.com", "subject": "Monthly update", "promo": True},
        cats) == "Promos"
    assert sender_mapped_category({"sender_email": "news@costco.com", "subject": "x"}, cats) is None


def test_sender_map_longest_pattern_wins_and_crud():
    cats = _with_cats("Banks", "Services")
    assert sender_mapped_category({"sender_email": "a@xfinity.com"}, cats) == "Services"
    store.add_sender_map("alerts@info6.citi.com", "Services", False)
    assert sender_mapped_category({"sender_email": "alerts@info6.citi.com"}, cats) == "Services"
    assert sender_mapped_category({"sender_email": "other@info6.citi.com"}, cats) == "Banks"
    items = store.remove_sender_map("alerts@info6.citi.com")
    assert all(m["pattern"] != "alerts@info6.citi.com" for m in items)


def test_sender_map_missing_category_skips():
    assert sender_mapped_category({"sender_email": "a@xfinity.com"}, ["Banks"]) is None


def test_bank_promo_content_goes_to_promos():
    cats = _with_cats("Banks", "Promos")
    stmt = {"sender_email": "citicards@info6.citi.com", "sender_name": "Citi",
            "subject": "Your statement is now available online", "promo": False,
            "category": "Promos"}
    assert recategorize_message(stmt, cats) == "Banks"


def test_recategorize_keeps_valid_and_repairs_case():
    from backend.ai_summary import recategorize_message
    assert recategorize_message({"sender_email": "x@y.com", "category": "promos"}, CATS) == "Promos"
    assert recategorize_message({"sender_email": "x@y.com", "category": "Banks"}, CATS) == "Banks"


def test_manual_lock_survives_remap_and_auto_unlocks():
    from backend import store as _store
    from backend.ai_summary import recategorize_message
    _store.add_category("Banks")
    _store.add_category("Personal")
    cats = _store.get_categories()
    _store.save_message({"id": "m1", "sender_email": "citicards@info6.citi.com",
                         "sender_name": "Citi", "subject": "Statement",
                         "snippet": "", "internal_date_ms": 1, "label_ids": [],
                         "body_text": "", "category": "Personal"})
    row = _store.set_message_category("m1", "Personal", locked=True)
    assert row["category_locked"] is True
    assert recategorize_message(row, cats) == "Personal"
    assert recategorize_message(row, cats, force=True) == "Banks"


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
