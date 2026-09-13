import pytest

from backend.rules import frequency_map, match_item


def item(**kw):
    base = {"sender_email": "Shop@amazon.com", "sender_name": "Amazon", "subject": "Great offers",
            "promo": False, "category": "Offer/Deal"}
    base.update(kw)
    return base


def rule(**kw):
    base = {"name": "r", "enabled": True, "action": "trash", "scope": "all_mail",
            "sender": None, "subject": [], "category": [], "emails_per_day": None, "about": ""}
    base.update(kw)
    return base


def test_sender_exact_address():
    r = rule(sender="shop@amazon.com")
    assert match_item(r, item(sender_email="Shop@amazon.com", sender_name="Amazon"), {})
    assert not match_item(r, item(sender_email="other@amazon.com"), {})


def test_sender_domain():
    r = rule(sender="@amazon.com")
    assert match_item(r, item(sender_email="X@AmazoN.com", sender_name="X"), {})
    assert not match_item(r, item(sender_email="x@google.com"), {})


def test_sender_display_name():
    r = rule(sender="Best Buy")
    assert match_item(r, item(sender_email="noreply@bestbuy.com", sender_name="Best Buy"), {})
    assert not match_item(r, item(sender_email="noreply@bestbuy.com", sender_name="Other"), {})


def test_promo_scope_requires_promo():
    r = rule(sender="@amazon.com", scope="promo_only")
    assert match_item(r, item(promo=True), {})
    assert not match_item(r, item(promo=False), {})


def test_subject_keyword_case_insensitive():
    r = rule(subject=["RECEIPT"])
    assert match_item(r, item(subject="Your receipt from Apple"), {})
    assert not match_item(r, item(subject="Your order shipped"), {})


def test_category_match_lower():
    r = rule(category=["finance/bill"])
    assert match_item(r, item(category="Finance/Bill"), {})
    assert not match_item(r, item(category="Newsletter"), {})


def test_frequency_requires_ctx():
    r = rule(emails_per_day=3)
    ctx = {"frequency": {"shop@amazon.com": 5.0}}
    assert match_item(r, item(sender_email="shop@amazon.com"), ctx)
    ctx2 = {"frequency": {"shop@amazon.com": 1.0}}
    assert not match_item(r, item(sender_email="shop@amazon.com"), ctx2)


def test_conditions_are_anded():
    r = rule(sender="@amazon.com", subject=["offer"])
    assert match_item(r, item(subject="Big offer"), {})
    assert not match_item(r, item(subject="Big sale"), {})


def test_disabled_rule_never_matches():
    r = rule(sender="@amazon.com", enabled=False)
    assert not match_item(r, item(), {})


def test_frequency_map_builds_per_day():
    traces = [{"sender_email": "a@b.com"}, {"sender_email": "a@b.com"}, {"sender_email": "x@y.com"}]
    out = frequency_map(traces, window_days=10)
    assert out["a@b.com"] == pytest.approx(0.2)
    assert out["x@y.com"] == pytest.approx(0.1)