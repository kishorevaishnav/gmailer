import time

from backend import store
from backend.wiki import prefilter

NOW = time.time()


def tr(message_id, email="a@b.com", name="A", subject="Offers", promo=0, category="Offer/Deal",
       action="trashed", ts=None, rule_id=None):
    return {"message_id": message_id, "sender_email": email, "sender_name": name,
            "subject": subject, "promo": promo, "category": category,
            "action": action, "ts": ts or NOW, "rule_id": rule_id}


def test_sender_cluster_when_consistent():
    for i in range(3):
        store.add_trace(**tr(f"m{i}", action="trashed"))
    clusters = prefilter()
    sender_clusters = [c for c in clusters if c["kind"] == "sender"]
    assert any(c["target"].lower() == "a@b.com" and c["dominant_action"] == "trash" for c in sender_clusters)


def test_promo_only_when_heavy_promos():
    for i in range(4):
        store.add_trace(**tr(f"p{i}", promo=1, action="trashed"))
    clusters = prefilter()
    promo = [c for c in clusters if c["kind"] == "sender" and c.get("promo_only")]
    assert any(c["target"].lower() == "a@b.com" for c in promo)


def test_keyword_cluster_across_senders():
    for i, email in enumerate(["x@1.com", "y@2.com", "z@3.com"]):
        store.add_trace(**tr(f"k{i}", email=email, subject="Your invoice ready", action="trashed"))
    clusters = prefilter()
    kw = [c for c in clusters if c["kind"] == "keyword"]
    assert any("invoice" in c["target"].lower() for c in kw)


def test_deduplicates_by_target():
    for i in range(3):
        store.add_trace(**tr(f"m{i}", action="trashed"))
    clusters = prefilter()
    sender_targets = {c["target"] for c in clusters if c["kind"] == "sender"}
    assert len(sender_targets) == 1


def test_multiple_sender_clusters_all_found():
    for i in range(3):
        store.add_trace(**tr(f"a{i}", email="a@b.com", action="trashed"))
    for i in range(3):
        store.add_trace(**tr(f"c{i}", email="c@d.com", action="trashed"))
    clusters = prefilter()
    sender_targets = {c["target"] for c in clusters if c["kind"] == "sender"}
    assert sender_targets == {"a@b.com", "c@d.com"}