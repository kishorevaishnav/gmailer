import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import store
from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _seed(mid, domain="example.com"):
    store.save_message({
        "id": mid, "sender_name": "Sender", "sender_email": f"noreply@{domain}",
        "subject": "s", "snippet": "", "internal_date_ms": 1,
        "label_ids": [], "body_text": "",
    })


def test_domain_trash_caches_removed_and_gmail_called(client, monkeypatch):
    _seed("m1"); _seed("m2")
    trashed = []
    monkeypatch.setattr("main.require_service", lambda: object())
    monkeypatch.setattr(
        "main.gmail_service.bulk_execute",
        lambda c, ids, fn: (trashed.extend(ids), [])[1],
    )
    monkeypatch.setattr("main._record_trace", lambda *a, **k: None)

    r = client.post("/api/domains/example.com/trash", json={"message_ids": ["m1", "m2", "m3"]})

    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 3
    assert body["failed"] == []
    assert body["removed_from_cache"] == 2
    assert set(trashed) == {"m1", "m2", "m3"}
    assert [m["id"] for m in store.list_messages()] == []


def test_domain_trash_failed_ids_kept_in_cache(client, monkeypatch):
    _seed("m1"); _seed("m2")
    monkeypatch.setattr("main.require_service", lambda: object())
    monkeypatch.setattr(
        "main.gmail_service.bulk_execute",
        lambda c, ids, fn: ["m1"],
    )
    monkeypatch.setattr("main._record_trace", lambda *a, **k: None)

    r = client.post("/api/domains/example.com/trash", json={"message_ids": ["m1", "m2"]})

    body = r.json()
    assert body["processed"] == 1
    assert body["failed"] == ["m1"]
    assert body["removed_from_cache"] == 1
    assert {m["id"] for m in store.list_messages()} == {"m1"}


def test_domain_trash_empty_ids_400(client, monkeypatch):
    monkeypatch.setattr("main.require_service", lambda: object())
    r = client.post("/api/domains/example.com/trash", json={"message_ids": []})
    assert r.status_code == 400


def test_domain_trash_unauthenticated(client, monkeypatch):
    def raise_unauth():
        raise HTTPException(status_code=401, detail="Not authenticated")

    monkeypatch.setattr("main.require_service", raise_unauth)

    r = client.post("/api/domains/example.com/trash", json={"message_ids": ["m1"]})
    assert r.status_code == 401