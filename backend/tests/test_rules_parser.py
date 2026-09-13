import pytest

from backend.rules import RuleParseError, parse_skill_md


def test_parse_minimal_rule():
    md = "---\nname: Amazon promos\naction: trash\nscope: promo_only\n---\n## match\nsender: @amazon.com\n"
    rule = parse_skill_md(md)
    assert rule["name"] == "Amazon promos"
    assert rule["action"] == "trash"
    assert rule["scope"] == "promo_only"
    assert rule["sender"] == "@amazon.com"
    assert rule["enabled"] is True


def test_parse_all_match_keys():
    md = """---
name: Quiet hours
action: skip
---
## match
sender: Payroll <payroll@corp.com>
subject: ["invoice", "receipt"]
category: ["Finance/Bill"]
emails_per_day: 3

## about
Skip finance noise.
"""
    rule = parse_skill_md(md)
    assert rule["sender"] == "Payroll <payroll@corp.com>"
    assert rule["subject"] == ["invoice", "receipt"]
    assert rule["category"] == ["Finance/Bill"]
    assert rule["emails_per_day"] == 3
    assert rule["about"] == "Skip finance noise."


def test_defaults_applied():
    rule = parse_skill_md("---\nname: X\naction: trash\n---\n## match\nsender: a@b.com\n")
    assert rule["scope"] == "all_mail"
    assert rule["enabled"] is True
    assert rule["subject"] == []
    assert rule["category"] == []


def test_missing_frontmatter_reports_line():
    with pytest.raises(RuleParseError) as exc:
        parse_skill_md("name: X\naction: trash\n")
    assert any("name is required" in e["msg"] for e in exc.value.errors)


def test_bad_action_reports_error():
    with pytest.raises(RuleParseError) as exc:
        parse_skill_md("---\nname: X\naction: explode\n---\n## match\nsender: a@b.com\n")
    assert any("action" in e["msg"] for e in exc.value.errors)


def test_unknown_match_key_reports_line():
    with pytest.raises(RuleParseError) as exc:
        parse_skill_md("---\nname: X\naction: trash\n---\n## match\nsender: a@b.com\nnope: 1\n")
    assert any("unknown match key" in e["msg"] for e in exc.value.errors)


def test_no_match_condition_reports_error():
    with pytest.raises(RuleParseError) as exc:
        parse_skill_md("---\nname: X\naction: trash\n---\n## match\n")
    assert any("at least one match" in e["msg"] for e in exc.value.errors)


def test_bad_emails_per_day_reports_error():
    with pytest.raises(RuleParseError) as exc:
        parse_skill_md("---\nname: X\naction: trash\n---\n## match\nsender: a@b.com\nemails_per_day: many\n")
    assert any("emails_per_day" in e["msg"] for e in exc.value.errors)


def test_sender_requires_at_or_domain():
    with pytest.raises(RuleParseError) as exc:
        parse_skill_md("---\nname: X\naction: trash\n---\n## match\nsender: bademail\n")
    assert any("sender" in e["msg"].lower() or "match" in e["msg"].lower() for e in exc.value.errors)