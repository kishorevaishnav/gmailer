import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import store
from main import app

TEMPLATE = """---
name: {name}
enabled: true
action: trash
scope: all_mail
---
## match
sender: {email}
"""


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _md(name, email):
    return TEMPLATE.format(name=name, email=email)


def test_rules_crud(client):
    assert client.get("/api/rules").json() == {"items": []}
    r = client.post("/api/rules", json={"markdown": _md("Alpha", "a@x.com")})
    assert r.status_code == 200
    rule = r.json()
    rid = rule["id"]
    assert rule["enabled"] is True and rule["parsed"]["sender"] == "a@x.com"

    bad = client.post("/api/rules", json={"markdown": "no frontmatter"})
    assert bad.status_code == 400
    assert "errors" in bad.json()["detail"]

    assert len(client.get("/api/rules").json()["items"]) == 1

    toggled = client.patch(f"/api/rules/{rid}", json={"enabled": False}).json()
    assert toggled["enabled"] is False

    assert client.patch("/api/rules/99999", json={"enabled": True}).status_code == 404

    assert client.delete(f"/api/rules/{rid}").json()["ok"] is True
    assert client.get("/api/rules").json()["items"] == []


def test_rules_reorder_preserves_precedence(client):
    a = client.post("/api/rules", json={"markdown": _md("A", "a@x.com")}).json()
    b = client.post("/api/rules", json={"markdown": _md("B", "b@x.com")}).json()
    c = client.post("/api/rules", json={"markdown": _md("C", "c@x.com")}).json()
    r = client.post("/api/rules/reorder", json={"rule_ids": [c["id"], a["id"], b["id"]]})
    assert r.json()["ok"] is True
    order = [x["id"] for x in client.get("/api/rules").json()["items"]]
    assert order == [c["id"], a["id"], b["id"]]


def test_proposal_approve_creates_rule_and_converts_observation(client):
    obs_id = store.upsert_observation(kind="sender", target="p@x.com", action="trash",
                                      summary="You trash promos", evidence_count=4,
                                      signal=0.9, last_seen=time.time())
    pid = store.add_proposal(source="proposer", label="P Promo",
                             summary="Auto-trash P", rationale="You trash all P promos",
                             downside="May hit a real deal",
                             evidence_json="[]", proposed_skill_md=_md("P", "p@x.com"),
                             observation_id=obs_id)
    assert len(client.get("/api/proposals").json()["items"]) == 1
    r = client.post(f"/api/proposals/{pid}/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["rule"]["parsed"]["sender"] == "p@x.com"
    # proposal approved, linked, rule live, observation converted
    assert body["proposal"]["status"] == "approved"
    assert store.get_rule(body["rule"]["id"])["enabled"] is True
    assert store.get_observation(obs_id)["status"] == "converted"
    # approving the same one again is a conflict
    assert client.post(f"/api/proposals/{pid}/approve").status_code == 409


def test_proposal_reject_sets_status(client):
    pid = store.add_proposal(source="proposer", label="Q", summary="s", rationale="r",
                             downside="d", evidence_json="[]", proposed_skill_md=_md("Q", "q@x.com"))
    r = client.post(f"/api/proposals/{pid}/reject")
    assert r.status_code == 200
    assert r.json()["proposal"]["status"] == "rejected"
    assert r.json()["proposal"]["rejected_until"] is not None
    assert client.post(f"/api/proposals/{pid}/reject").status_code == 409
    # rejected proposals no longer show as pending
    assert client.get("/api/proposals").json()["items"] == []


def test_observation_dismiss(client):
    obs_id = store.upsert_observation(kind="sender", target="d@x.com", action="trash",
                                      summary="s", evidence_count=1, signal=0.5,
                                      last_seen=time.time())
    assert client.post(f"/api/wiki/observations/{obs_id}/dismiss").json()["ok"] is True
    assert store.get_observation(obs_id)["status"] == "dismissed"
    assert client.post("/api/wiki/observations/99999/dismiss").status_code == 404


def test_traces_endpoint(client):
    store.add_trace(message_id="m1", sender_email="t@x.com", sender_name="T",
                    subject="S", promo=False, category="News", action="trashed", rule_id=None)
    items = client.get("/api/traces").json()["items"]
    assert items and items[0]["message_id"] == "m1"
    assert client.get("/api/traces", params={"rule_id": 7}).json()["items"] == []


def test_evolve_without_traces_is_safe(client):
    r = client.post("/api/evolve")
    assert r.status_code == 200
    body = r.json()
    assert body["created_observations"] == 0 and body["created_proposals"] == 0