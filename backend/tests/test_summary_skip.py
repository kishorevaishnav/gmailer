import pytest

from backend import ai_summary, auth, config, store
from backend.summarizer import summarize_needy


def test_summary_skip_round_trip():
    assert store.is_summary_skipped("biller@example.com") is False
    store.add_summary_skip("Biller@Example.com ")
    assert store.is_summary_skipped("biller@example.com") is True
    assert "biller@example.com" in store.get_summary_skips()
    store.remove_summary_skip("biller@example.com")
    assert store.is_summary_skipped("biller@example.com") is False
    assert store.get_summary_skips() == []


def test_generate_summary_skips_opted_out_sender():
    store.add_summary_skip("biller@example.com")
    msg = {"id": 1, "sender_email": "biller@example.com", "sender_name": "Biller",
           "subject": "Invoice", "snippet": "pay now", "in_bundle": False}
    assert ai_summary.generate_summary(msg) is None


def test_generate_summary_force_bypasses_skip():
    store.add_summary_skip("biller@example.com")
    msg = {"id": 1, "sender_email": "biller@example.com", "sender_name": "Biller",
           "subject": "Invoice", "snippet": "pay now", "in_bundle": False}
    assert ai_summary.generate_summary(msg, force=True) is not None


def test_summarize_needy_noop_without_token(monkeypatch):
    monkeypatch.setattr(auth, "token_exists", lambda: False)
    monkeypatch.setattr(config, "QUEUE_SUMMARIZE_BATCH", 50)
    assert summarize_needy() == 0


def test_summarize_needy_noop_when_batch_disabled(monkeypatch):
    monkeypatch.setattr(auth, "token_exists", lambda: True)
    monkeypatch.setattr(config, "QUEUE_SUMMARIZE_BATCH", 0)
    assert summarize_needy() == 0


def test_summarize_needy_respects_batch_limit_and_skip(monkeypatch):
    monkeypatch.setattr(auth, "token_exists", lambda: True)
    monkeypatch.setattr(config, "QUEUE_SUMMARIZE_BATCH", 2)
    monkeypatch.setattr(config, "MAX_BATCH", 500)
    monkeypatch.setattr("backend.summarizer.time.sleep", lambda *_: None)

    msgs = [
        {"id": "a", "sender_email": "a@x.com", "sender_name": "A", "subject": "A", "body_text": "x",
         "internal_date_ms": 1, "category": "Newsletter"},
        {"id": "b", "sender_email": "b@x.com", "sender_name": "B", "subject": "B", "body_text": "x",
         "internal_date_ms": 2, "category": "Newsletter"},
        {"id": "c", "sender_email": "c@x.com", "sender_name": "C", "subject": "C", "body_text": "x",
         "internal_date_ms": 3, "category": "Newsletter"},
        {"id": "d", "sender_email": "d@x.com", "sender_name": "D", "subject": "D", "body_text": "x",
         "internal_date_ms": 4, "category": "Newsletter"},
    ]
    monkeypatch.setattr(store, "list_messages", lambda limit=None: msgs)
    store.add_summary_skip("c@x.com")  # excluded before limit slice

    called = []
    monkeypatch.setattr(ai_summary, "generate_summary", lambda m: called.append(m["id"]) or {"one_liner": "ok", "category": m.get("category", "Unclear")})

    assert summarize_needy() == 2
    assert called == ["d", "b"]  # newest-first; c skipped; a dropped beyond limit
    assert "c" not in called  # skipped sender
    assert "a" not in called  # beyond batch limit


def test_summarize_needy_prioritizes_finance_before_batch(monkeypatch):
    monkeypatch.setattr(auth, "token_exists", lambda: True)
    monkeypatch.setattr(config, "QUEUE_SUMMARIZE_BATCH", 2)
    monkeypatch.setattr(config, "MAX_BATCH", 500)
    monkeypatch.setattr("backend.summarizer.time.sleep", lambda *_: None)

    msgs = [
        {"id": "n1", "sender_email": "news@x.com", "sender_name": "N", "subject": "s", "body_text": "x",
         "internal_date_ms": 5, "category": "Newsletter", "summary": None},
        {"id": "bill", "sender_email": "bank@x.com", "sender_name": "Bank", "subject": "due", "body_text": "x",
         "internal_date_ms": 4, "category": "Finance/Bill", "summary": None},
        {"id": "n2", "sender_email": "news2@x.com", "sender_name": "N2", "subject": "s", "body_text": "x",
         "internal_date_ms": 6, "category": "Newsletter", "summary": None},
    ]
    monkeypatch.setattr(store, "list_messages", lambda limit=None: msgs)
    monkeypatch.setattr("backend.summarizer._load_priority_senders", lambda: [])
    monkeypatch.setattr(ai_summary, "generate_summary", lambda m: {"one_liner": "ok", "category": m.get("category", "Unclear")})

    called = []
    orig = ai_summary.generate_summary
    def spy(m):
        r = orig(m)
        called.append(m["category"])
        return r
    monkeypatch.setattr(ai_summary, "generate_summary", spy)

    assert summarize_needy() == 2
    assert called == ["Finance/Bill", "Newsletter"]
