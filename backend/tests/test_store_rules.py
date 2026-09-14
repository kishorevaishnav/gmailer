import time

import pytest

from backend import store


def test_rule_crud_and_precedence_order():
    a = store.add_rule("md-a", "{}")
    b = store.add_rule("md-b", "{}")
    rules = store.list_rules()
    assert rules[0]["id"] == a and rules[1]["id"] == b
    store.move_rule(a, 1)  # down
    rules = store.list_rules()
    assert rules[0]["id"] == b and rules[1]["id"] == a
    store.update_rule(a, enabled=0, skill_md="md-a2", parsed_json="{}")
    got = store.get_rule(a)
    assert got["skill_md"] == "md-a2" and got["enabled"] == 0
    store.delete_rule(a)
    assert store.get_rule(a) is None
    assert [r["id"] for r in store.list_rules()] == [b]


def _trash_rule_md(name, sender, scope="all_mail"):
    return ("---\n"
            f"name: {name}\nenabled: true\naction: trash\nscope: {scope}\n---\n"
            "## match\n"
            f"sender: {sender}\n")


def _add_trash(name, sender, scope="all_mail"):
    from backend.rules import parse_skill_md, parsed_json
    md = _trash_rule_md(name, sender, scope)
    return store.add_rule(md, parsed_json(parse_skill_md(md)))


def test_append_and_remove_rule_sender():
    rid = _add_trash("Trash ALL [blocklist]", '["a@x.com"]')
    assert store.find_append_target("trash", "all_mail")["id"] == rid
    updated = store.append_rule_sender(rid, "A@X.COM")
    assert updated["parsed"]["sender"] == ["a@x.com"]
    updated = store.append_rule_sender(rid, "b@y.com")
    assert updated["parsed"]["sender"] == ["a@x.com", "b@y.com"]
    updated = store.remove_rule_sender(rid, "A@X.COM")
    assert updated["parsed"]["sender"] == ["b@y.com"]
    assert store.remove_rule_sender(rid, "nobody@z.com") is None
    with pytest.raises(ValueError):
        store.remove_rule_sender(rid, "b@y.com")


def test_rule_covers_sender_with_domain():
    _add_trash("Trash ALL [blocklist]", '["a@x.com", "@y.com"]')
    assert store.rule_covers_sender("trash", "all_mail", "a@x.com")
    assert store.rule_covers_sender("trash", "all_mail", "any@y.com")
    assert not store.rule_covers_sender("trash", "all_mail", "nope@z.com")
    assert not store.rule_covers_sender("star", "all_mail", "a@x.com")
    assert store.rule_covers_sender("trash", "promo_only", "a@x.com")


def test_traces_roundtrip_and_trim():
    store.add_trace(message_id="m1", sender_email="a@b.com", sender_name="A", subject="hi",
                    promo=0, category=None, action="trashed", rule_id=None, ts=time.time())
    store.add_trace(message_id="m2", sender_email="a@b.com", sender_name="A", subject="hi",
                    promo=1, category="Offer/Deal", action="auto_trash", rule_id=1, ts=time.time())
    traces = store.traces_since(3600)
    assert len(traces) == 2
    assert traces[0]["action"] == "trashed"
    assert store.list_traces(limit=1)[0]["action"] == "auto_trash"
    assert store.trace_counts_for_rule(1)["total"] == 1
    store.add_trace(message_id="m3", sender_email="a@b.com", sender_name="A", subject="old",
                    promo=0, category=None, action="trashed", rule_id=None, ts=time.time() - 999999)
    store.trim_traces(max_age_days=1)
    assert all(t["message_id"] != "m3" for t in store.traces_since(1))


def test_wiki_upsert_and_observations():
    old = store.upsert_observation(kind="sender", target="@amazon.com", summary="s1",
                                   evidence_count=3, signal=0.7, last_seen=time.time())
    store.upsert_observation(kind="sender", target="@amazon.com", summary="s2",
                             evidence_count=5, signal=0.8, last_seen=time.time(), action="trash")
    obs = store.list_observations()
    assert len(obs) == 1
    assert obs[0]["id"] == old and obs[0]["summary"] == "s2" and obs[0]["evidence_count"] == 5
    store.mark_observation_converted(old, 42)
    assert store.list_observations()[0]["rule_id"] == 42
    store.dismiss_observation(old)
    assert store.list_observations()[0]["status"] == "dismissed"


def test_proposals_lifecycle_and_dedup():
    pid = store.add_proposal(source="proposer", label="L1", summary="s", rationale="r",
                             downside="d", evidence_json='["t1"]',
                             proposed_skill_md="---\nname: X\naction: trash\n---\n## match\nsender: a@b.com\n")
    assert store.list_proposals()[0]["id"] == pid
    assert store.has_duplicate_proposal("---\nname: X\naction: trash\n---\n## match\nsender: a@b.com\n")
    rid = store.add_rule("md", "{}")
    store.approve_proposal(pid, rid)
    assert store.get_proposal(pid)["status"] == "approved"
    pid2 = store.add_proposal(source="proposer", label="L2", summary="s", rationale="r",
                              downside="d", evidence_json="[]", proposed_skill_md="---\nname: Y\n---\n")
    store.reject_proposal(pid2, suppress_days=30)
    assert store.get_proposal(pid2)["status"] == "rejected"