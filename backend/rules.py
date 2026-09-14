"""WikiSkill rule "skills": editable markdown -> structured rule -> matcher."""
from __future__ import annotations

import json

_ACTIONS = {"trash", "star", "skip"}
_SCOPES = {"all_mail", "promo_only"}
_TRUE = {"1", "true", "yes", "on"}


class RuleParseError(ValueError):
    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(str(errors))


def _split_frontmatter(text: str) -> tuple[dict, str]:
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return {}, text
    try:
        _, fm, rest = stripped.split("---", 2)
    except ValueError:
        return {}, text
    front: dict = {}
    for i, raw in enumerate(fm.splitlines()):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise RuleParseError([{"line": i + 2, "msg": f"expected 'key: value', got: {line}"}])
        k, v = line.split(":", 1)
        key = k.strip().lower()
        val = v.strip()
        if key == "name":
            if not val:
                raise RuleParseError([{"line": i + 2, "msg": "name cannot be empty"}])
            front["name"] = val
        elif key == "enabled":
            front["enabled"] = val.lower() in _TRUE
        elif key in ("action", "scope"):
            front[key] = val.lower()
        else:
            raise RuleParseError([{"line": i + 2, "msg": f"unknown frontmatter key: {key}"}])
    return front, rest


def _parse_list(s: str) -> list[str]:
    s = s.strip()
    if s.startswith("["):
        try:
            data = json.loads(s)
            return [str(x).strip() for x in data] if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _parse_sender_list(s: str) -> list[str]:
    s = (s or "").strip()
    if s.startswith("["):
        return _parse_list(s)
    return [s] if s else []


def parse_skill_md(md: str) -> dict:
    errors: list[dict] = []
    front, rest = _split_frontmatter(md or "")
    name = front.get("name")
    if not name:
        errors.append({"line": 1, "msg": "name is required (frontmatter must be first)"})

    action = front.get("action")
    if action not in _ACTIONS:
        errors.append({"line": 1, "msg": f"action must be one of {sorted(_ACTIONS)}"})
    scope = front.get("scope", "all_mail")
    if scope not in _SCOPES:
        errors.append({"line": 1, "msg": f"scope must be one of {sorted(_SCOPES)}"})

    body: dict = {}
    lines = rest.splitlines()
    in_match = False
    about_parts: list[str] = []
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        s = line.strip()
        low = s.lower()
        if low.startswith("## "):
            in_match = low == "## match"
            continue
        if low.startswith("# ") or s.startswith("---"):
            continue
        if not in_match:
            if s:
                about_parts.append(s)
            continue
        if not s or s.startswith("#"):
            continue
        if ":" not in s:
            errors.append({"line": i + 1, "msg": f"expected 'key: value' in match, got: {s}"})
            continue
        k, v = s.split(":", 1)
        key = k.strip().lower()
        val = v.strip()
        if key == "sender":
            body["sender"] = _parse_sender_list(val)
        elif key == "subject":
            body["subject"] = _parse_list(val)
        elif key == "category":
            body["category"] = _parse_list(val)
        elif key == "emails_per_day":
            try:
                epd = int(float(val))
                if epd <= 0:
                    raise ValueError
                body["emails_per_day"] = epd
            except ValueError:
                errors.append({"line": i + 1, "msg": "emails_per_day must be a positive integer"})
        else:
            errors.append({"line": i + 1, "msg": f"unknown match key: {key}"})

    senders = body.get("sender") or []
    for entry in senders:
        s = (entry or "").strip()
        if "@" not in s and not s.startswith("@"):
            errors.append({"line": 1, "msg": "sender must be an email (a@b.com), @domain, or display name"})

    if not (senders or body.get("subject") or body.get("category") or body.get("emails_per_day")):
        errors.append({"line": 1, "msg": "at least one match condition is required (sender | subject | category | emails_per_day)"})

    if errors:
        raise RuleParseError(errors)

    return {
        "name": name,
        "enabled": front.get("enabled", True),
        "action": action,
        "scope": scope,
        "sender": senders or None,
        "subject": body.get("subject", []) or [],
        "category": body.get("category", []) or [],
        "emails_per_day": body.get("emails_per_day"),
        "about": "\n".join(about_parts).strip(),
    }


def parsed_json(rule: dict) -> str:
    return json.dumps(rule, sort_keys=True)


def sender_entry_matches(entry: str, email: str, name: str = "") -> bool:
    s = (entry or "").strip()
    if s.startswith("@"):
        return email.endswith(s.lower())
    if "@" in s:
        return email == s.lower()
    return name.lower() == s.lower() or email == s.lower()


def _as_sender_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(x) for x in value]


def match_item(rule: dict, item: dict, ctx: dict | None = None) -> bool:
    """First-match-wins predicate. ctx = {"frequency": {email: emails_per_day}}."""
    if not rule.get("enabled", True):
        return False
    if rule.get("scope") == "promo_only" and not item.get("promo"):
        return False

    senders = _as_sender_list(rule.get("sender"))
    email = (item.get("sender_email") or "").strip().lower()
    name = (item.get("sender_name") or "").strip()
    if senders and not any(sender_entry_matches(s, email, name) for s in senders):
        return False

    subs = rule.get("subject") or []
    subject = (item.get("subject") or "").lower()
    if subs and not any(k.lower() in subject for k in subs):
        return False

    cats = [c.lower() for c in (rule.get("category") or [])]
    cat = (item.get("category") or "").strip().lower()
    if cats and cat not in cats:
        return False

    epd = rule.get("emails_per_day")
    if epd:
        freq = (ctx or {}).get("frequency") or {}
        if freq.get(email, 0) < epd:
            return False
    return True


def frequency_map(traces: list[dict], window_days: float = 30.0) -> dict[str, float]:
    """emails/day per sender derived from trace rows."""
    counts: dict[str, int] = {}
    for t in traces:
        e = (t.get("sender_email") or "").strip().lower()
        counts[e] = counts.get(e, 0) + 1
    if window_days <= 0:
        return {}
    return {e: c / window_days for e, c in counts.items()}


ACTION_VERBS = {"trash": "Trash", "star": "Star", "skip": "Skip"}


def normalize_sender(sender: str) -> str:
    s = (sender or "").strip()
    return s.lower() if "@" in s else s


def rule_name(parsed: dict) -> str:
    verb = ACTION_VERBS.get((parsed.get("action") or "").lower(), "Apply")
    scope = "PROMOS" if (parsed.get("scope") or "").lower() == "promo_only" else "ALL"
    bits = []
    senders = _as_sender_list(parsed.get("sender"))
    if senders:
        bits.append(f"from {senders[0]}" + (f" +{len(senders) - 1} more" if len(senders) > 1 else ""))
    if parsed.get("subject"):
        bits.append("subject [" + ", ".join(parsed["subject"]) + "]")
    if parsed.get("category"):
        bits.append("category [" + ", ".join(parsed["category"]) + "]")
    if parsed.get("emails_per_day"):
        bits.append(f">= {parsed['emails_per_day']}/day")
    return f"{verb} {scope} {' + '.join(bits) or 'everything'}"


def render_skill_md(parsed: dict, enabled: bool = True) -> str:
    lines = ["---",
             f"name: {parsed.get('name') or 'Untitled rule'}",
             f"enabled: {'true' if enabled else 'false'}",
             f"action: {parsed.get('action')}",
             f"scope: {parsed.get('scope', 'all_mail')}",
             "---", "## match"]
    senders = _as_sender_list(parsed.get("sender"))
    if len(senders) == 1:
        lines.append(f"sender: {senders[0]}")
    elif senders:
        lines.append(f"sender: {json.dumps(senders, ensure_ascii=False)}")
    if parsed.get("subject"):
        lines.append(f"subject: {json.dumps(parsed['subject'], ensure_ascii=False)}")
    if parsed.get("category"):
        lines.append(f"category: {json.dumps(parsed['category'], ensure_ascii=False)}")
    if parsed.get("emails_per_day"):
        lines.append(f"emails_per_day: {parsed['emails_per_day']}")
    lines += ["", "## about", parsed.get("about") or ""]
    return "\n".join(lines) + "\n"


def rule_md_for_sender(email: str, name: str, promo_only: bool = False) -> str:
    sender = normalize_sender(email)
    scope = "promo_only" if promo_only else "all_mail"
    about = (
        "Automatically trash promo mail from this sender."
        if promo_only
        else "Automatically trash all mail from this sender."
    )
    return (
        "---\n"
        f"name: {rule_name({'action': 'trash', 'scope': scope, 'sender': sender})}\n"
        "enabled: true\n"
        "action: trash\n"
        f"scope: {scope}\n"
        "---\n"
        "## match\n"
        f"sender: {sender}\n"
        "\n"
        "## about\n"
        f"{about}\n"
    )