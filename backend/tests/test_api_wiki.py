import time

import pytest
from fastapi.testclient import TestClient

from backend import store
from backend.rules import parse_skill_md
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
    r = client.post("/api/rules", json={"skill_md": _md("Alpha", "a@x.com")})
    assert r.status_code == 200
    rid = r.json()["id"]
    assert client.get("/api/rules").json()["items"][0]["parsed"]["sender"] == "a@x.com"

    bad = client.post("/api/rules", json={"skill_md": "no frontmatter"})
    assert bad.status_code == 400
    assert "errors" in bad.json()["detail"]

    assert client.put(f"/api/rules/{rid}", json={"enabled": False}).status_code == 200
    assert client.get("/api/rules").json()["items"][0]["enabled"] is False
    assert client.put(f"/api/rules/{rid}", json={"skill_md": "junk"}).status_code == 400
    assert client.put("/api/rules/99999", json={"enabled": True}).status_code == 404

    toggled = client.post(f"/api/rules/{rid}/toggle").json()
    assert toggled["enabled"] is True

    assert client.delete(f"/api/rules/{rid}").json()["ok"] is True
    assert client.get("/api/rules").json()["items"] == []


def test_rules_move_changes_precedence(client):
    a = client.post("/api/rules", json={"skill_md": _md("A", "a@x.com")}).json()["id"]
    b = client.post("/api/rules", json={"skill_md": _md("B", "b@x.com")}).json()["id"]
    order = lambda: [x["id"] for x in client.get("/api/rules").json()["items"]]
    assert order() == [a, b]
    assert client.post(f"/api/rules/{b}/move", json={"dir": "up"}).json()["ok"] is True
    assert order() == [b, a]
    assert client.post(f"/api/rules/{b}/move", json={"dir": "up"}).json()["ok"] is True
    assert order() == [b, a]


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
    r = client.post(f"/api/proposals/{pid}/approve", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["rule"]["parsed"]["sender"] == "p@x.com" and body["rule_id"] == body["rule"]["id"]
    assert body["proposal"]["status"] == "approved"
    assert store.get_rule(body["rule_id"])["enabled"] is True
    assert store.get_observation(obs_id)["status"] == "converted"
    assert client.post(f"/api/proposals/{pid}/approve", json={}).status_code == 409


def test_proposal_approve_with_override_and_apply(monkeypatch, client):
    pid = store.add_proposal(source="proposer", label="Q", summary="s", rationale="r",
                             downside="d", evidence_json="[]", proposed_skill_md=_md("Q", "q@x.com"))
    acted: list[str] = []
    monkeypatch.setattr("main.require_service", lambda: object())
    monkeypatch.setattr("main.gmail_service.bulk_execute", lambda c, ids, fn: (acted.extend(ids), [])[1])
    store.save_message({"id": "m1", "sender_email": "q@x.com", "sender_name": "Q",
                        "subject": "hi", "promo": 0, "category": "Newsletter"})
    r = client.post(f"/api/proposals/{pid}/approve",
                    json={"apply_now": True, "candidate_ids": ["m1"]})
    assert r.status_code == 200
    assert r.json()["apply"]["trash"] == 1 and "m1" in acted
    traces = store.list_traces(limit=10, rule_id=r.json()["rule_id"])
    assert any(t["message_id"] == "m1" and t["action"] == "auto_trash" for t in traces)


def test_proposal_reject_sets_status(client):
    pid = store.add_proposal(source="proposer", label="R", summary="s", rationale="r",
                             downside="d", evidence_json="[]", proposed_skill_md=_md("R", "r@x.com"))
    r = client.post(f"/api/proposals/{pid}/reject", json={})
    assert r.status_code == 200
    assert r.json()["proposal"]["status"] == "rejected"
    assert r.json()["proposal"]["rejected_until"] is not None
    assert client.post(f"/api/proposals/{pid}/reject", json={}).status_code == 409
    assert client.get("/api/proposals").json()["items"] == []


def test_wiki_dismiss_and_create_rule(client):
    obs_id = store.upsert_observation(kind="sender", target="d@x.com", action="trash",
                                      summary="s", evidence_count=1, signal=0.5,
                                      last_seen=time.time())
    assert client.get("/api/wiki").json()["items"][0]["id"] == obs_id
    assert client.post(f"/api/wiki/{obs_id}/dismiss", json={}).json()["ok"] is True
    assert store.get_observation(obs_id)["status"] == "dismissed"
    assert client.post("/api/wiki/99999/dismiss", json={}).status_code == 404

    obs2 = store.upsert_observation(kind="sender", target="c@x.com", action="star",
                                    summary="You always star C", evidence_count=3,
                                    signal=0.8, last_seen=time.time())
    r = client.post(f"/api/wiki/{obs2}/create-rule", json={})
    assert r.status_code == 200
    prop = store.get_proposal(r.json()["id"])
    assert parse_skill_md(prop["proposed_skill_md"])["sender"] == "c@x.com"
    assert parse_skill_md(prop["proposed_skill_md"])["action"] == "star"
    assert client.post(f"/api/wiki/{obs2}/create-rule", json={}).status_code == 409


def test_rule_apply_candidate_ids(monkeypatch, client):
    monkeypatch.setattr("main.require_service", lambda: object())
    acted: list[str] = []
    monkeypatch.setattr("main.gmail_service.bulk_execute", lambda c, ids, fn: (acted.extend(ids), [])[1])
    store.save_message({"id": "m1", "sender_email": "a@x.com", "sender_name": "A",
                        "subject": "hi", "promo": 0, "category": "Newsletter"})
    rid = client.post("/api/rules", json={"skill_md": _md("A", "a@x.com")}).json()["id"]
    r = client.post(f"/api/rules/{rid}/apply", json={"candidate_ids": ["m1"]})
    assert r.status_code == 200
    assert r.json()["trash"] == 1 and "m1" in acted
    traces = store.list_traces(limit=10, rule_id=rid)
    assert any(t["message_id"] == "m1" and t["action"] == "auto_trash" for t in traces)
    assert client.post("/api/rules/99999/apply", json={"candidate_ids": []}).status_code == 404


def test_evolve_without_traces_is_safe(client):
    r = client.post("/api/evolve", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["created_observations"] == 0 and body["created_proposals"] == 0