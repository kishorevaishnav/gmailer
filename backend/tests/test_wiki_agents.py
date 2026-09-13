import json
import time

from backend import store
from backend.rules import parse_skill_md
from backend.wiki import maintain_cluster, propose_rule, run_evolve


class FakeLLM:
    def __init__(self, text):
        self.text, self.calls = text, []

    def __call__(self, system, user):
        self.calls.append((system, user))
        return self.text


def cluster():
    traces = []
    for i in range(3):
        store.add_trace(message_id=f"m{i}", sender_email="a@b.com", sender_name="A",
                        subject="Offers", promo=1, category="Offer/Deal", action="trashed")
        traces.append({"message_id": f"m{i}", "sender_email": "a@b.com", "sender_name": "A",
                       "subject": "Offers", "promo": 1, "category": "Offer/Deal", "action": "trashed", "ts": time.time()})
    return {"kind": "sender", "target": "a@b.com", "label": "A — 3/3 trashed",
            "dominant_action": "trash", "promo_only": True, "emails_per_day": None,
            "items": traces, "count": 3}


def test_maintain_cluster_uses_llm_and_upserts():
    llm = FakeLLM("clean summary text")
    obs = maintain_cluster(cluster(), llm)
    assert obs["summary"] == "clean summary text"
    assert obs["kind"] == "sender" and obs["target"] == "a@b.com"
    assert store.list_observations()[0]["evidence_count"] == 3
    # second run updates, not duplicates
    maintain_cluster(cluster(), llm)
    assert len(store.list_observations()) == 1


def test_maintain_cluster_falls_back_without_llm():
    obs = maintain_cluster(cluster(), None)
    assert obs["summary"] and len(store.list_observations()) == 1


def test_propose_rule_uses_llm_and_persists():
    obs = maintain_cluster(cluster(), None)
    llm = FakeLLM(json.dumps({"summary": "Auto-delete promos", "rationale": "You trash all promos", "downside": "Could match a real deal you want"}))
    prop = propose_rule(cluster(), obs, llm)
    assert store.list_proposals("pending")[0]["id"] == prop["id"]
    p = store.get_proposal(prop["id"])
    assert p["rationale"] == "You trash all promos"
    md = parse_skill_md(p["proposed_skill_md"])
    assert md["sender"] == "a@b.com" and md["scope"] == "promo_only" and md["action"] == "trash"


def test_propose_rule_skips_duplicate():
    obs = maintain_cluster(cluster(), None)
    propose_rule(cluster(), obs, None)
    before = len(store.list_proposals("pending"))
    propose_rule(cluster(), obs, None)
    assert len(store.list_proposals("pending")) == before


def test_run_evolve_end_to_end():
    llm = FakeLLM(json.dumps({"summary": "S", "rationale": "R", "downside": "D"}))
    cluster()  # seeds traces
    res = run_evolve(llm_fn=llm)
    assert res["created_observations"] >= 1
    assert res["created_proposals"] >= 1