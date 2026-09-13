from backend import gmail_service, store
from backend.rules import parse_skill_md, parsed_json, rule_md_for_sender
from main import _apply_rules


def _add(md):
    return store.add_rule(md, parsed_json(parse_skill_md(md)))


def item(**kw):
    base = {"id": "m1", "sender_email": "Shop@amazon.com", "sender_name": "Amazon",
            "subject": "Deals", "promo": True, "category": "Offer/Deal", "bundle_key": "k"}
    base.update(kw)
    return base


def test_trash_rule_applied_to_inbox(monkeypatch):
    md = rule_md_for_sender("shop@amazon.com", "Amazon", promo_only=True)
    _add(md)
    trashed = []

    def fake_bulk(client, ids, fn):
        trashed.extend(ids)
        return []

    monkeypatch.setattr(gmail_service, "bulk_execute", fake_bulk)

    items = [item(id="m1", promo=True), item(id="m2", promo=False)]
    kept, n_trash, n_star, n_skip, _rs = _apply_rules(None, items)
    assert trashed == ["m1"]
    assert n_trash == 1 and n_star == 0 and n_skip == 0
    assert [i["id"] for i in kept] == ["m2"]


def test_star_and_skip_rules_fire(monkeypatch):
    _add("---\nname: star promos\naction: star\nscope: promo_only\n---\n## match\nsender: @amazon.com\n")
    _add("---\nname: skip finance\naction: skip\n---\n## match\ncategory: [\"Finance/Bill\"]\n")
    skipped = []

    def fake_bulk(client, ids, fn):
        return []

    def fake_add_skipped(it):
        skipped.append(it["id"])

    monkeypatch.setattr(gmail_service, "bulk_execute", fake_bulk)
    monkeypatch.setattr(store, "add_skipped", fake_add_skipped)
    items = [item(id="m1", promo=True), item(id="m2", promo=False, category="Finance/Bill")]
    kept, n_trash, n_star, n_skip, _rs = _apply_rules(None, items)
    assert n_star == 1 and n_skip == 1 and len(kept) == 0


def test_in_precedence_order_first_match_wins(monkeypatch):
    _add("---\nname: broad\naction: skip\n---\n## match\nsender: @amazon.com\n")
    _add("---\nname: narrow\naction: trash\nscope: promo_only\n---\n## match\nsender: @amazon.com\n")
    monkeypatch.setattr(gmail_service, "bulk_execute", lambda c, ids, fn: [])
    monkeypatch.setattr(store, "add_skipped", lambda it: None)
    items = [item(id="m1", promo=True)]
    _, n_trash, n_star, n_skip, _rs = _apply_rules(None, items)
    assert n_skip == 1 and n_trash == 0  # broad rule matched first


def test_failed_trash_counts_not_incremented(monkeypatch):
    _add("---\nname: t\naction: trash\n---\n## match\nsender: @amazon.com\n")
    monkeypatch.setattr(gmail_service, "bulk_execute", lambda c, ids, fn: ids)  # all fail
    items = [item(id="m1", promo=False)]
    kept, n_trash, _, _, _rs = _apply_rules(None, items)
    assert n_trash == 0
    assert [i["id"] for i in kept] == ["m1"]