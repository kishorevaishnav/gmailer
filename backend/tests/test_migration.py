from backend import store
from backend.rules import parse_skill_md


def _seed_legacy():
    conn = store._get()
    conn.execute("INSERT INTO blocked (email, sender_name) VALUES ('a@b.com', 'A Co')")
    conn.execute("INSERT INTO promo_blocked (email, sender_name) VALUES ('c@d.com', 'C Co')")
    conn.commit()


def test_migrate_converts_and_drops_legacy():
    _seed_legacy()
    made = store.migrate_legacy_blocked()
    assert made == 2
    rules = store.list_rules()
    assert len(rules) == 2
    by_sender = {parse_skill_md(r["skill_md"])["sender"]: parse_skill_md(r["skill_md"]) for r in rules}
    assert by_sender["a@b.com"]["action"] == "trash"
    assert by_sender["a@b.com"]["scope"] == "all_mail"
    assert by_sender["c@d.com"]["scope"] == "promo_only"
    conn = store._get()
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='blocked'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='promo_blocked'").fetchone()[0] == 0


def test_migrate_idempotent_when_no_legacy():
    assert store.migrate_legacy_blocked() == 0