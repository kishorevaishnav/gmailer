from backend import store


def _msg(mid, subject="Hello", sender="alice@example.com", name="Alice",
         snippet="snip", body="body text here", ms=1000):
    return {
        "id": mid,
        "sender_name": name,
        "sender_email": sender,
        "subject": subject,
        "snippet": snippet,
        "internal_date_ms": ms,
        "label_ids": ["INBOX"],
        "body_text": body,
    }


def test_search_subject():
    store.save_message(_msg("m1", subject="Quarterly invoice due"))
    store.save_message(_msg("m2", subject="Team lunch"))
    hits = store.search_messages("invoice")
    assert [h["id"] for h in hits] == ["m1"]


def test_search_body_and_sender_and_snippet():
    store.save_message(_msg("m1", body="the project codename is narwhal"))
    store.save_message(_msg("m2", sender="billing@shop.com", name="Shop"))
    store.save_message(_msg("m3", snippet="limited time offer inside"))
    assert [h["id"] for h in store.search_messages("narwhal")] == ["m1"]
    assert [h["id"] for h in store.search_messages("billing@shop")] == ["m2"]
    assert [h["id"] for h in store.search_messages("limited time")] == ["m3"]


def test_search_case_insensitive_and_newest_first():
    store.save_message(_msg("m1", subject="Invoice #1", ms=1000))
    store.save_message(_msg("m2", subject="INVOICE #2", ms=2000))
    hits = store.search_messages("invoice")
    assert [h["id"] for h in hits] == ["m2", "m1"]


def test_search_escapes_like_wildcards():
    store.save_message(_msg("m1", subject="100% guaranteed"))
    store.save_message(_msg("m2", subject="something else entirely"))
    assert [h["id"] for h in store.search_messages("100%")] == ["m1"]
    assert [h["id"] for h in store.search_messages("%")] == ["m1"]
    assert store.search_messages("") == []
    assert store.search_messages("   ") == []


def test_search_limit():
    for i in range(5):
        store.save_message(_msg(f"m{i}", subject="common topic", ms=i))
    assert len(store.search_messages("common", limit=3)) == 3
