import pytest
from fastapi.testclient import TestClient

from backend import store
from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _seed(mid="m1", sender="alerts@info6.citi.com", category="Promos"):
    store.save_message({
        "id": mid, "sender_name": "Citi", "sender_email": sender,
        "subject": "Statement", "snippet": "", "internal_date_ms": 1,
        "label_ids": [], "body_text": "", "category": category,
    })


def test_manual_set_locks_and_learns_mapping(client):
    _seed()
    r = client.post("/api/categories/set", json={"id": "m1", "category": "Personal"})
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "Personal"
    assert body["category_locked"] is True
    assert body["learned_mapping"] == {"pattern": "alerts@info6.citi.com", "category": "Personal",
                                                "subject_contains": ""}
    maps = {m["pattern"]: m for m in store.get_sender_map()}
    assert maps["alerts@info6.citi.com"]["category"] == "Personal"


def test_learned_mapping_drives_future_mail(client):
    _seed()
    client.post("/api/categories/set", json={"id": "m1", "category": "Personal"})
    _seed(mid="m2", category="Promos")
    r = client.post("/api/categories/remap-one", json={"id": "m2"})
    assert r.json()["category"] == "Personal"


def test_set_with_reason_records_condition(client):
    store.add_category("Banks")
    _seed()
    r = client.post("/api/categories/set",
                    json={"id": "m1", "category": "Personal", "subject_contains": "Statement, Invoice"})
    assert r.status_code == 200
    assert r.json()["learned_mapping"]["subject_contains"] == "statement, invoice"
    maps = {m["pattern"]: m for m in store.get_sender_map()}
    assert maps["alerts@info6.citi.com"]["subject_contains"] == "statement, invoice"
    from backend.ai_summary import sender_mapped_category
    cats = store.get_categories()
    assert sender_mapped_category(
        {"sender_email": "alerts@info6.citi.com", "subject": "Your invoice is ready"}, cats) == "Personal"
    assert sender_mapped_category(
        {"sender_email": "alerts@info6.citi.com", "subject": "A transaction alert"}, cats) == "Banks"


def test_unknown_category_rejected(client):
    _seed()
    r = client.post("/api/categories/set", json={"id": "m1", "category": "Nope"})
    assert r.status_code == 400


def test_missing_message_404(client):
    r = client.post("/api/categories/set", json={"id": "ghost", "category": "Personal"})
    assert r.status_code == 404
