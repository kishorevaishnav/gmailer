# WikiSkill Rules & Wiki Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded blocked/promo-delete lists with a WikiSkill-style, user-editable auto-delete rule engine (markdown skill files) plus two local Ollama agents (Wiki Maintainer + Skill Proposer), all surfaced in a sidebar "Rules & Wiki" panel.

**Architecture:** Three-layer WikiSkill model in SQLite — `traces` (immutable raw actions), `wiki_observations` (maintainer-distilled knowledge), `rules` (editable markdown skills executed in precedence order on every queue pull). A deterministic prefilter mines traces into candidate clusters; a Wiki Maintainer agent distills them into observations; a Skill Proposer agent drafts markdown proposals; the user edits/approves/rejects in the frontend (human gate). All just single Ollama calls in `backend/wiki.py` — no agent framework, no downloaded WikiSkill code.

**Tech Stack:** FastAPI (`main.py`), SQLite (`backend/store.py`), Python 3.11, vanilla JS + Tailwind CDN (`static/app.js`, `static/index.html`, `static/app.css`), pytest + httpx (dev), local Ollama `gemma3:4b` via `backend/ai_summary.py`.

**Design spec:** `docs/superpowers/specs/2026-09-13-rules-wiki-design.md`

---

### Task 1: Dev test scaffold + config knobs

**Files:**
- Create: `requirements-dev.txt`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_rules_parser.py`
- Modify: `backend/config.py`

- [ ] **Step 1: Write the failing parser smoke test**

`backend/tests/test_rules_parser.py`:

```python
from backend.rules import parse_skill_md

def test_parse_minimal_rule():
    md = "---\nname: Amazon promos\naction: trash\nscope: promo_only\n---\n## match\nsender: @amazon.com\n"
    rule = parse_skill_md(md)
    assert rule["name"] == "Amazon promos"
    assert rule["action"] == "trash"
    assert rule["scope"] == "promo_only"
    assert rule["sender"] == "@amazon.com"
    assert rule["enabled"] is True
```

- [ ] **Step 2: Create dev requirements file**

`requirements-dev.txt`:

```
-r requirements.txt
pytest>=8.0
httpx>=0.27
```

- [ ] **Step 3: Create test conftest (fresh temp DB per test)**

`backend/tests/conftest.py`:

```python
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import config, store


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    store._conn = None
    store._get()
    yield
    store._conn = None
```

- [ ] **Step 4: Add config knobs**

Append to `backend/config.py`:

```python
# WikiSkill rules & wiki tuning
TRACE_WINDOW_SECONDS = 30 * 86400          # prefilter looks back this far
TRACE_RETENTION_DAYS = 90
MIN_ACTIONS_FOR_PATTERN = 3                 # minimum same-sender actions
PROMO_RATIO_FOR_PATTERN = 0.9               # >=90% promos => promo-only rule
PATTERN_RECENT_DAYS = 7                     # at least one action this recent
RECENT_SAMPLE_SIZE = 5                      # latest N messages for promo ratio
KEYWORD_MIN_LEN = 4
FREQ_EMAILS_PER_DAY = 3                     # volume threshold
EVOLVE_MAX_CLUSTERS = 10
PROPOSAL_SUPPRESS_DAYS = 30
```

- [ ] **Step 5: Verify the scaffold (test must FAIL — rules.py doesn't exist yet)**

```bash
.venv/bin/python3.11 -m pip install -q -r requirements-dev.txt
.venv/bin/python3.11 -m pytest backend/tests/test_rules_parser.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.rules'`.

---

### Task 2: `backend/rules.py` — markdown skill parser (frontmatter + `## match`)

**Files:**
- Create: `backend/rules.py`
- Test: `backend/tests/test_rules_parser.py`

- [ ] **Step 1: Add the failing tests (full parser suite)**

Append to `backend/tests/test_rules_parser.py`:

```python
import pytest

from backend.rules import RuleParseError, parse_skill_md


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
    assert any(e["msg"].startswith("skill must start") for e in exc.value.errors)


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
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_rules_parser.py -v
```
Expected: FAIL (module missing / first two tests undefined).

- [ ] **Step 3: Implement `parser`**

Create `backend/rules.py`:

```python
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
            body["sender"] = val
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

    sender = body.get("sender")
    if sender is not None:
        s = sender.strip()
        if "@" not in s and not s.startswith("@"):
            errors.append({"line": 1, "msg": "sender must be an email (a@b.com), @domain, or display name"})

    if not (body.get("sender") or body.get("subject") or body.get("category") or body.get("emails_per_day")):
        errors.append({"line": 1, "msg": "at least one match condition is required (sender | subject | category | emails_per_day)"})

    if errors:
        raise RuleParseError(errors)

    return {
        "name": name,
        "enabled": front.get("enabled", True),
        "action": action,
        "scope": scope,
        "sender": (sender or "").strip() or None,
        "subject": body.get("subject", []) or [],
        "category": body.get("category", []) or [],
        "emails_per_day": body.get("emails_per_day"),
        "about": "\n".join(about_parts).strip(),
    }


def parsed_json(rule: dict) -> str:
    return json.dumps(rule, sort_keys=True)


def rule_md_for_sender(email: str, name: str, promo_only: bool = False) -> str:
    scope = "promo_only" if promo_only else "all_mail"
    noun = name or email
    title = f"{'Promos from ' if promo_only else 'Block '}{noun}"
    about = (
        "Automatically delete promo emails from this sender."
        if promo_only
        else "Automatically delete all email from this sender."
    )
    return (
        "---\n"
        f"name: {title}\n"
        "enabled: true\n"
        "action: trash\n"
        f"scope: {scope}\n"
        "---\n"
        "## match\n"
        f"sender: {email}\n"
        "\n"
        "## about\n"
        f"{about}\n"
    )
```

- [ ] **Step 4: Run the parser tests, confirm pass**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_rules_parser.py -v
```
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add requirements-dev.txt backend/tests backend/config.py backend/rules.py
git commit -m "feat(rules): markdown skill parser + dev test scaffold"
```

---

### Task 3: `backend/rules.py` — matcher (`match_item`)

**Files:**
- Create: `backend/tests/test_rules_matcher.py`
- Modify: `backend/rules.py` (append matcher)

- [ ] **Step 1: Write failing matcher tests**

`backend/tests/test_rules_matcher.py`:

```python
from backend.rules import match_item

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
```

- [ ] **Step 2: Run tests, confirm fail**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_rules_matcher.py -v
```
Expected: FAIL — `ImportError: cannot import name 'match_item'`.

- [ ] **Step 3: Implement matcher**

Append to `backend/rules.py`:

```python
def match_item(rule: dict, item: dict, ctx: dict | None = None) -> bool:
    """First-match-wins predicate. ctx = {"frequency": {email: emails_per_day}}."""
    if not rule.get("enabled", True):
        return False
    if rule.get("scope") == "promo_only" and not item.get("promo"):
        return False

    sender = rule.get("sender")
    email = (item.get("sender_email") or "").strip().lower()
    name = (item.get("sender_name") or "").strip()
    if sender:
        s = sender.strip()
        if s.startswith("@"):
            if not email.endswith(s.lower()):
                return False
        elif "@" in s:
            if email != s.lower():
                return False
        else:
            if name.lower() != s.lower() and email != s.lower():
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
```

- [ ] **Step 4: Run matcher tests, confirm pass**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_rules_matcher.py -v
```
Expected: 10 passed (9 matcher tests + frequency_map via ctx only; frequency_map has no direct test — add one):

```python
def test_frequency_map_builds_per_day():
    from backend.rules import frequency_map
    traces = [{"sender_email": "a@b.com"}, {"sender_email": "a@b.com"}, {"sender_email": "x@y.com"}]
    out = frequency_map(traces, window_days=10)
    assert out["a@b.com"] == pytest.approx(0.2)
    assert out["x@y.com"] == pytest.approx(0.1)
```
(& `import pytest` at top). Re-run: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/rules.py backend/tests/test_rules_matcher.py
git commit -m "feat(rules): matcher + frequency context"
```

---

### Task 4: `backend/store.py` — rules, traces, wiki, proposals tables + CRUD

**Files:**
- Modify: `backend/store.py` (schema + new functions)
- Create: `backend/tests/test_store_rules.py`

- [ ] **Step 1: Write failing store tests**

`backend/tests/test_store_rules.py`:

```python
import json
import time

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
    store.trim_traces(max_age_days=0)  # nothing older than 0 days
    # use an explicit old ts instead:
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
```

- [ ] **Step 2: Run tests, confirm fail**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_store_rules.py -v
```
Expected: FAIL — functions/columns missing.

- [ ] **Step 3: Extend the schema**

In `backend/store.py`, add to `_SCHEMA` (before the closing `"""`):

```python
CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_md    TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    enabled     INTEGER DEFAULT 1,
    precedence  INTEGER NOT NULL UNIQUE,
    created_at  REAL,
    updated_at  REAL
);

CREATE TABLE IF NOT EXISTS traces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    message_id  TEXT,
    sender_email TEXT,
    sender_name TEXT,
    subject     TEXT,
    promo       INTEGER DEFAULT 0,
    category    TEXT,
    action      TEXT NOT NULL,
    rule_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_traces_sender ON traces(sender_email, action, ts);
CREATE INDEX IF NOT EXISTS idx_traces_rule ON traces(rule_id);

CREATE TABLE IF NOT EXISTS wiki_observations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,
    target         TEXT NOT NULL,
    action         TEXT DEFAULT 'trash',
    summary        TEXT NOT NULL,
    evidence_count INTEGER DEFAULT 0,
    signal         REAL DEFAULT 0,
    first_seen     REAL,
    last_seen      REAL,
    status         TEXT DEFAULT 'open',
    rule_id        INTEGER
);

CREATE TABLE IF NOT EXISTS rule_proposals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source            TEXT NOT NULL,
    label             TEXT NOT NULL,
    summary           TEXT,
    rationale         TEXT,
    downside          TEXT,
    evidence_json     TEXT NOT NULL,
    proposed_skill_md TEXT NOT NULL,
    rule_id           INTEGER,
    status            TEXT DEFAULT 'pending',
    created_at        REAL,
    rejected_until    REAL
);
```

- [ ] **Step 4: Implement store functions**

Append to `backend/store.py`:

```python
# --- Rules (skills) ----------------------------------------------------------

def next_precedence() -> int:
    row = _get().execute("SELECT COALESCE(MAX(precedence), 0) + 1 FROM rules").fetchone()
    return int(row[0])


def add_rule(skill_md: str, parsed_json: str) -> int:
    now = time.time()
    with _lock:
        cur = _get().execute(
            "INSERT INTO rules (skill_md, parsed_json, enabled, precedence, created_at, updated_at)"
            " VALUES (?, ?, 1, ?, ?, ?)",
            (skill_md, parsed_json, next_precedence(), now, now),
        )
        _get().commit()
        return int(cur.lastrowid)


def get_rule(rule_id: int) -> dict | None:
    row = _get().execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["parsed"] = json.loads(d["parsed_json"])
    return d


def list_rules() -> list[dict]:
    rows = _get().execute("SELECT * FROM rules ORDER BY precedence").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["parsed"] = json.loads(d["parsed_json"])
        out.append(d)
    return out


def update_rule(rule_id: int, *, skill_md: str | None = None, parsed_json: str | None = None,
                enabled: int | None = None, precedence: int | None = None) -> None:
    sets, vals = [], []
    if skill_md is not None:
        sets.append("skill_md = ?"); vals.append(skill_md)
    if parsed_json is not None:
        sets.append("parsed_json = ?"); vals.append(parsed_json)
    if enabled is not None:
        sets.append("enabled = ?"); vals.append(1 if enabled else 0)
    if precedence is not None:
        sets.append("precedence = ?"); vals.append(int(precedence))
    if not sets:
        return
    sets.append("updated_at = ?"); vals.append(time.time())
    vals.append(rule_id)
    with _lock:
        _get().execute(f"UPDATE rules SET {', '.join(sets)} WHERE id = ?", vals)
        _get().commit()


def move_rule(rule_id: int, direction: int) -> None:
    rules = list_rules()
    idx = next((i for i, r in enumerate(rules) if r["id"] == rule_id), None)
    if idx is None:
        return
    other = idx + direction
    if other < 0 or other >= len(rules):
        return
    a, b = rules[idx], rules[other]
    with _lock:
        _get().execute("UPDATE rules SET precedence = ? WHERE id = ?", (b["precedence"], a["id"]))
        _get().execute("UPDATE rules SET precedence = ? WHERE id = ?", (a["precedence"], b["id"]))
        _get().commit()


def delete_rule(rule_id: int) -> None:
    with _lock:
        _get().execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        _get().commit()


# --- Traces (raw layer) ------------------------------------------------------

def add_trace(*, message_id, sender_email, sender_name, subject, promo, category,
              action: str, rule_id: int | None = None, ts: float | None = None) -> None:
    with _lock:
        _get().execute(
            "INSERT INTO traces (ts, message_id, sender_email, sender_name, subject, promo, category, action, rule_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts or time.time(), message_id, sender_email, sender_name, subject,
             1 if promo else 0, category, action, rule_id),
        )
        _get().commit()


def traces_since(seconds: int) -> list[dict]:
    since = time.time() - seconds
    rows = _get().execute("SELECT * FROM traces WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    return [dict(r) for r in rows]


def list_traces(limit: int = 50, rule_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM traces"
    params: list = []
    if rule_id is not None:
        sql += " WHERE rule_id = ?"
        params.append(rule_id)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in _get().execute(sql, params).fetchall()]


def trace_counts_for_rule(rule_id: int, recent_days: int = 7) -> dict:
    row = _get().execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS recent "
        "FROM traces WHERE rule_id = ?",
        (time.time() - recent_days * 86400, rule_id),
    ).fetchone()
    return {"total": int(row["total"] or 0), "recent": int(row["recent"] or 0)}


def trim_traces(max_age_days: int = 90) -> int:
    cutoff = time.time() - max_age_days * 86400
    with _lock:
        cur = _get().execute("DELETE FROM traces WHERE ts < ?", (cutoff,))
        _get().commit()
        return cur.rowcount


# --- Wiki observations -------------------------------------------------------

def upsert_observation(*, kind: str, target: str, action: str, summary: str,
                       evidence_count: int, signal: float, last_seen: float,
                       rule_id: int | None = None) -> int:
    now = time.time()
    row = _get().execute(
        "SELECT id, first_seen, rule_id AS old_rule FROM wiki_observations WHERE kind = ? AND target = ?",
        (kind, target),
    ).fetchone()
    with _lock:
        if row:
            _get().execute(
                "UPDATE wiki_observations SET action = ?, summary = ?, evidence_count = ?, signal = ?,"
                " last_seen = ?, rule_id = COALESCE(?, rule_id) WHERE id = ?",
                (action, summary, evidence_count, signal, last_seen, rule_id, row["id"]),
            )
            _get().commit()
            return int(row["id"])
        cur = _get().execute(
            "INSERT INTO wiki_observations (kind, target, action, summary, evidence_count, signal,"
            " first_seen, last_seen, status, rule_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (kind, target, action, summary, evidence_count, signal, now, last_seen, rule_id),
        )
        _get().commit()
        return int(cur.lastrowid)


def list_observations() -> list[dict]:
    rows = _get().execute(
        "SELECT * FROM wiki_observations ORDER BY signal DESC, last_seen DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_observation(obs_id: int) -> dict | None:
    row = _get().execute("SELECT * FROM wiki_observations WHERE id = ?", (obs_id,)).fetchone()
    return dict(row) if row else None


def dismiss_observation(obs_id: int) -> None:
    with _lock:
        _get().execute("UPDATE wiki_observations SET status = 'dismissed' WHERE id = ?", (obs_id,))
        _get().commit()


def mark_observation_converted(obs_id: int, rule_id: int) -> None:
    with _lock:
        _get().execute("UPDATE wiki_observations SET status = 'converted', rule_id = ? WHERE id = ?",
                       (rule_id, obs_id))
        _get().commit()


# --- Proposals ---------------------------------------------------------------

def add_proposal(*, source: str, label: str, summary: str, rationale: str, downside: str,
                 evidence_json: str, proposed_skill_md: str) -> int:
    with _lock:
        cur = _get().execute(
            "INSERT INTO rule_proposals (source, label, summary, rationale, downside,"
            " evidence_json, proposed_skill_md, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (source, label, summary, rationale, downside, evidence_json, proposed_skill_md, time.time()),
        )
        _get().commit()
        return int(cur.lastrowid)


def list_proposals(status: str = "pending") -> list[dict]:
    rows = _get().execute(
        "SELECT * FROM rule_proposals WHERE status = ? ORDER BY created_at DESC", (status,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_proposal(proposal_id: int) -> dict | None:
    row = _get().execute("SELECT * FROM rule_proposals WHERE id = ?", (proposal_id,)).fetchone()
    return dict(row) if row else None


def approve_proposal(proposal_id: int, rule_id: int) -> None:
    with _lock:
        _get().execute("UPDATE rule_proposals SET status = 'approved', rule_id = ? WHERE id = ?",
                       (rule_id, proposal_id))
        _get().commit()


def reject_proposal(proposal_id: int, suppress_days: int = 30) -> None:
    with _lock:
        _get().execute(
            "UPDATE rule_proposals SET status = 'rejected', rejected_until = ? WHERE id = ?",
            (time.time() + suppress_days * 86400, proposal_id),
        )
        _get().commit()


def has_duplicate_proposal(proposed_skill_md: str) -> bool:
    norm = (proposed_skill_md or "").strip().lower()
    if not norm:
        return False
    rows = _get().execute(
        "SELECT proposed_skill_md, status, rejected_until FROM rule_proposals"
    ).fetchall()
    now = time.time()
    for r in rows:
        if (r["proposed_skill_md"] or "").strip().lower() != norm:
            continue
        if r["status"] == "approved":
            return True
        if r["status"] == "pending":
            return True
        if r["status"] == "rejected" and r["rejected_until"] and r["rejected_until"] > now:
            return True
    return False


def identical_rule_exists(parsed_json: str) -> bool:
    return any(r["parsed_json"] == parsed_json for r in list_rules())
```

- [ ] **Step 5: Run store tests, confirm pass**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_store_rules.py -v
```
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/store.py backend/tests/test_store_rules.py
git commit -m "feat(store): rules, traces, wiki, proposals persistence"
```

---

### Task 5: Legacy blocked/promo migration

**Files:**
- Modify: `backend/store.py` (migration function)
- Modify: `main.py` (startup hook)
- Create: `backend/tests/test_migration.py`

- [ ] **Step 1: Write failing migration tests**

`backend/tests/test_migration.py`:

```python
from backend import store
from backend.rules import parse_skill_md


def _seed_legacy():
    conn = store._get()
    conn.execute("CREATE TABLE blocked (email TEXT PRIMARY KEY, sender_name TEXT, blocked_at REAL)")
    conn.execute("CREATE TABLE promo_blocked (email TEXT PRIMARY KEY, sender_name TEXT, promo_blocked_at REAL)")
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
```

- [ ] **Step 2: Run tests, confirm fail**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_migration.py -v
```
Expected: FAIL — `migrate_legacy_blocked` missing.

- [ ] **Step 3: Implement migration**

Append to `backend/store.py` (import rules at top of file first; change line 12 area):

At top of `store.py`, after existing imports add:

```python
from .rules import parse_skill_md, parsed_json, rule_md_for_sender
```

Then append:

```python
def migrate_legacy_blocked() -> int:
    """Convert legacy blocked/promo_blocked tables into rules, then drop them."""
    try:
        _get().execute("SELECT COUNT(*) FROM blocked").fetchone()
    except sqlite3.OperationalError:
        return 0
    rows = _get().execute("SELECT email, sender_name FROM blocked").fetchall()
    promo_rows = _get().execute("SELECT email, sender_name FROM promo_blocked").fetchall()
    made = 0
    for r in rows:
        md = rule_md_for_sender(r["email"], r["sender_name"], promo_only=False)
        add_rule(md, parsed_json(parse_skill_md(md)))
        made += 1
    for r in promo_rows:
        md = rule_md_for_sender(r["email"], r["sender_name"], promo_only=True)
        add_rule(md, parsed_json(parse_skill_md(md)))
        made += 1
    with _lock:
        _get().execute("DROP TABLE IF EXISTS blocked")
        _get().execute("DROP TABLE IF EXISTS promo_blocked")
        _get().commit()
    return made
```

- [ ] **Step 4: Add startup hook in `main.py`**

In `main.py`, after `app = FastAPI(...)`:

```python
@app.on_event("startup")
def _startup_migrate_legacy():
    try:
        made = store.migrate_legacy_blocked()
        if made:
            logger.info("Migrated %d legacy blocked/promo senders into rules", made)
    except Exception:
        logger.exception("Legacy blocked migration failed")
```

(Circular-import note: `backend.store` imports `backend.rules`; `main.py` imports both — no cycle since `rules` imports nothing from `main/store`.)

- [ ] **Step 5: Run migration tests, confirm pass**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_migration.py -v
```
Expected: 2 passed. Then verify the app still imports:
```bash
.venv/bin/python3.11 -c "import main; print('main OK')"
```

- [ ] **Step 6: Commit**

```bash
git add backend/store.py backend/tests/test_migration.py main.py
git commit -m "feat(store): migrate legacy blocked/promo lists into rules"
```

---

### Task 6: Rule engine in `/api/queue` (auto trash/star/skip)

**Files:**
- Modify: `main.py` (api_queue + helpers)
- Create: `backend/tests/test_engine.py`

- [ ] **Step 1: Write failing engine tests**

`backend/tests/test_engine.py`:

```python
from backend import config, gmail_service, store
from backend.rules import rule_md_for_sender
from main import _apply_rules


def item(**kw):
    base = {"id": "m1", "sender_email": "Shop@amazon.com", "sender_name": "Amazon",
            "subject": "Deals", "promo": True, "category": "Offer/Deal", "bundle_key": "k"}
    base.update(kw)
    return base


def test_trash_rule_applied_to_inbox(monkeypatch):
    md = rule_md_for_sender("shop@amazon.com", "Amazon", promo_only=True)
    store.add_rule(md, _parsed(md))
    trashed = []

    def fake_bulk(client, ids, fn):
        trashed.extend(ids)
        return []

    monkeypatch.setattr(gmail_service, "bulk_execute", fake_bulk)

    items = [item(id="m1", promo=True), item(id="m2", promo=False)]
    kept, n_trash, n_star, n_skip = _apply_rules(None, items)
    assert trashed == ["m1"]
    assert n_trash == 1 and n_star == 0 and n_skip == 0
    assert [i["id"] for i in kept] == ["m2"]


def test_star_and_skip_rules_fire(monkeypatch):
    store.add_rule("---\nname: star promos\naction: star\n---\n## match\nsender: @amazon.com\nscope: promo_only\n", _parsed("noop"))
    store.add_rule("---\nname: skip finance\naction: skip\n---\n## match\ncategory: [Finance/Bill]\n", _parsed("noop"))
    skipped = []

    def fake_bulk(client, ids, fn):
        return []

    def fake_add_skipped(it):
        skipped.append(it["id"])

    monkeypatch.setattr(gmail_service, "bulk_execute", fake_bulk)
    monkeypatch.setattr(store, "add_skipped", fake_add_skipped)
    items = [item(id="m1", promo=True), item(id="m2", promo=False, category="Finance/Bill")]
    kept, n_trash, n_star, n_skip = _apply_rules(None, items)
    assert n_star == 1 and n_skip == 1 and len(kept) == 0


def test_in_precedence_order_first_match_wins(monkeypatch):
    store.add_rule("---\nname: broad\naction: skip\n---\n## match\nsender: @amazon.com\n", _parsed("n"))
    store.add_rule("---\nname: narrow\naction: trash\n---\n## match\nsender: @amazon.com\nscope: promo_only\n", _parsed("n"))
    monkeypatch.setattr(gmail_service, "bulk_execute", lambda c, ids, fn: [])
    monkeypatch.setattr(store, "add_skipped", lambda it: None)
    items = [item(id="m1", promo=True)]
    _, n_trash, n_star, n_skip = _apply_rules(None, items)
    assert n_skip == 1 and n_trash == 0  # broad rule matched first


def test_failed_trash_counts_not_incremented(monkeypatch):
    store.add_rule("---\nname: t\naction: trash\n---\n## match\nsender: @amazon.com\n", _parsed("n"))
    monkeypatch.setattr(gmail_service, "bulk_execute", lambda c, ids, fn: ids)  # all fail
    items = [item(id="m1", promo=False)]
    kept, n_trash, _, _ = _apply_rules(None, items)
    assert n_trash == 0
    assert [i["id"] for i in kept] == ["m1"]


def _parsed(_md):
    from backend.rules import parse_skill_md, parsed_json
    return parsed_json(parse_skill_md(_md))
```

- [ ] **Step 2: Run tests, confirm fail**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_engine.py -v
```
Expected: FAIL — `cannot import name '_apply_rules' from main`.

- [ ] **Step 3: Implement `_apply_rules` + rewrite api_queue auto-trash**

Replace the blocked/promo block inside `main.py::api_queue` (the section from `# Auto-delete:` through `items = kept`) with:

```python
    kept, auto_trashed, auto_starred, auto_skipped = _apply_rules(service, items)
    items = kept
```

Add these helpers in `main.py` (place near `_run_bulk`):

```python
def _apply_rules(service, items: list[dict]) -> tuple[list[dict], int, int, int]:
    """Apply enabled rules in precedence order (first match wins) to a queue
    batch. Trash/star go through Gmail in bulk; skip hides locally. Auto-actions
    leave a trace row so rules stay anchored to the mail they acted on."""
    from backend.rules import frequency_map, match_item, parsed_json
    from backend import wiki as wiki_module

    enabled = [r for r in store.list_rules() if r["enabled"]]
    if not enabled:
        return items, 0, 0, 0
    enabled.sort(key=lambda r: r["precedence"])
    freq = frequency_map(store.traces_since(wiki_module.TRACE_WINDOW_SECONDS))
    ctx = {"frequency": freq}

    kept, trash_ids, star_ids, skip_items = [], [], [], []
    fired = []  # (item, rule)
    for it in items:
        hit = next((r for r in enabled if match_item(r["parsed"], it, ctx)), None)
        if hit is None:
            kept.append(it)
            continue
        fired.append((it, hit))
        if hit["parsed"]["action"] == "trash":
            trash_ids.append(it["id"])
        elif hit["parsed"]["action"] == "star":
            star_ids.append(it["id"])
        else:
            skip_items.append(it)

    failed_trash = gmail_service.bulk_execute(service, trash_ids, gmail_service.trash) if trash_ids else []
    trash_done = [i for i in trash_ids if i not in failed_trash]
    failed_star = gmail_service.bulk_execute(service, star_ids, gmail_service.star) if star_ids else []
    star_done = [i for i in star_ids if i not in failed_star]
    for it in skip_items:
        store.add_skipped(it)

    for it, rule in fired:
        action = rule["parsed"]["action"]
        done = (
            (action == "trash" and it["id"] in trash_done)
            or (action == "star" and it["id"] in star_done)
            or action == "skip"
        )
        store.add_trace(
            message_id=it["id"],
            sender_email=it.get("sender_email"),
            sender_name=it.get("sender_name"),
            subject=it.get("subject"),
            promo=bool(it.get("promo")),
            category=it.get("category"),
            action=f"auto_{action}",
            rule_id=rule["id"],
        )

    return (kept, len(trash_done), len(star_done), len(skip_items))
```

Update the api_queue return dict (drop `auto_deleted`/`blocked_count`):

```python
    return {
        "total": len(items),
        "items": items,
        "bundles": bundles,
        "queried_count": max_results,
        "cache_count": store.cache_count(),
        "next_page_token": next_page_token,
        "auto_trashed": auto_trashed,
        "auto_starred": auto_starred,
        "auto_skipped": auto_skipped,
    }
```

- [ ] **Step 4: Run engine tests, confirm pass**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_engine.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Sanity-import main**

```bash
.venv/bin/python3.11 -c "import main; print('main OK')"
```

- [ ] **Step 6: Commit**

```bash
git add main.py backend/tests/test_engine.py
git commit -m "feat(engine): apply skill rules in api_queue auto trash/star/skip"
```

---

### Task 7: Trace ingestion on user actions

**Files:**
- Modify: `main.py` (action endpoints record traces)
- Modify: `backend/gmail_service.py` (metadata lookup helper reuse)
- Create: `backend/tests/test_traces_ingest.py`

- [ ] **Step 1: Write failing ingestion test**

`backend/tests/test_traces_ingest.py`:

```python
from backend import store
from main import _trace_item_for_message


def test_trace_item_from_cache():
    store.save_message({"id": "m1", "sender_email": "a@b.com", "sender_name": "A",
                        "subject": "hi", "promo": 1, "category": "Offer/Deal"})
    it = _trace_item_for_message(None, "m1")
    assert it["sender_email"] == "a@b.com"
    assert it["promo"] is True
    assert it["category"] == "Offer/Deal"
```

- [ ] **Step 2: Run, confirm fail**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_traces_ingest.py -v
```
Expected: FAIL — missing function.

- [ ] **Step 3: Implement trace helper + wire into endpoint**

In `main.py`, add helper and a shared `_record_trace` near `_undo_payload`:

```python
def _trace_item_for_message(service, message_id: str) -> dict | None:
    """Best-effort metadata for trace rows: cache first, then Gmail metadata."""
    cached = store.load_message(message_id)
    if cached and cached.get("sender_email"):
        return {
            "sender_email": cached.get("sender_email"),
            "sender_name": cached.get("sender_name"),
            "subject": cached.get("subject"),
            "promo": bool(cached.get("promo")),
            "category": cached.get("category"),
        }
    try:
        meta = gmail_service.get_metadata(service, message_id)
        return {
            "sender_email": meta.get("sender_email"),
            "sender_name": meta.get("sender_name"),
            "subject": meta.get("subject"),
            "promo": bool(meta.get("promo")),
            "category": meta.get("category"),
        }
    except Exception:
        return None


def _record_trace(service, message_id: str, action: str, rule_id: int | None = None) -> None:
    info = _trace_item_for_message(service, message_id)
    if info:
        store.add_trace(message_id=message_id, action=action, rule_id=rule_id, **info)
```

Wire into the action endpoints (add one line each):

- `api_trash`: after `_run(...)`: `_record_trace(service, message_id, "trashed")`
- `api_archive`: `_record_trace(service, message_id, "archived")`
- `api_star`: `_record_trace(service, message_id, "starred")`
- `api_skip_add` (`/api/skipped/add`): `_record_trace(service, message_id, "skipped")` — use the id from the request body; read the current handler to get the field name (`SkipAddRequest`) before editing.
- `api_message`: after generating/caching the summary: `_record_trace(service, message_id, "reviewed")` (wrapped in try/except so a trace failure never blocks detail view).
- `_run_bulk`: after success, `for mid in req.message_ids: _record_trace(service, mid, action)` (action already passed in).

- [ ] **Step 4: Run ingestion test, confirm pass**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_traces_ingest.py -v
```
Expected: 1 passed. Then:
```bash
.venv/bin/python3.11 -c "import main; print('main OK')"
```

- [ ] **Step 5: Commit**

```bash
git add main.py backend/tests/test_traces_ingest.py
git commit -m "feat(traces): record user actions and reads into raw layer"
```

---

### Task 8: `backend/wiki.py` — deterministic prefilter (candidate clusters)

**Files:**
- Create: `backend/wiki.py`
- Create: `backend/tests/test_wiki_prefilter.py`

- [ ] **Step 1: Write failing prefilter tests**

`backend/tests/test_wiki_prefilter.py`:

```python
from backend import config, store
from backend.wiki import prefilter


def tr(message_id, email="a@b.com", name="A", subject="Offers", promo=0, category="Offer/Deal",
       action="trashed", ts=1_700_000_000.0, rule_id=None):
    return {"message_id": message_id, "sender_email": email, "sender_name": name,
            "subject": subject, "promo": promo, "category": category,
            "action": action, "ts": ts, "rule_id": rule_id}


def test_sender_cluster_when_consistent():
    for i in range(3):
        store.add_trace(**{k: v for k, v in tr(f"m{i}", action="trashed").items() if k != "ts"}, ts=tr(f"m{i}")["ts"])
    clusters = prefilter()
    sender_clusters = [c for c in clusters if c["kind"] == "sender"]
    assert any(c["target"].lower() == "a@b.com" and c["dominant_action"] == "trash" for c in sender_clusters)


def test_promo_only_when_heavy_promos():
    for i in range(4):
        store.add_trace(**{k: v for k, v in tr(f"p{i}", promo=1, action="trashed").items() if k != "ts"}, ts=tr(f"p{i}")["ts"])
    clusters = prefilter()
    promo = [c for c in clusters if c["kind"] == "sender" and c.get("promo_only")]
    assert any(c["target"].lower() == "a@b.com" for c in promo)


def test_keyword_cluster_across_senders():
    for i, email in enumerate(["x@1.com", "y@2.com", "z@3.com"]):
        store.add_trace(**{k: v for k, v in tr(f"k{i}", email=email, subject="Your invoice ready", action="trashed").items() if k != "ts"}, ts=tr(f"k{i}")["ts"])
    clusters = prefilter()
    kw = [c for c in clusters if c["kind"] == "keyword"]
    assert any("invoice" in c["target"].lower() for c in kw)


def test_deduplicates_by_target():
    for i in range(3):
        store.add_trace(**{k: v for k, v in tr(f"m{i}", action="trashed").items() if k != "ts"}, ts=tr(f"m{i}")["ts"])
    clusters = prefilter()
    sender_targets = {c["target"] for c in clusters if c["kind"] == "sender"}
    assert len(sender_targets) == 1
```

- [ ] **Step 2: Run, confirm fail**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_wiki_prefilter.py -v
```
Expected: FAIL — `No module named 'backend.wiki'`.

- [ ] **Step 3: Implement prefilter**

Create `backend/wiki.py`:

```python
"""WikiSkill loop: deterministic prefilter + two local Ollama agent roles."""
from __future__ import annotations

import logging
import re

import requests

from . import config, store

logger = logging.getLogger("gmailer.wiki")

TRACE_WINDOW_SECONDS = config.TRACE_WINDOW_SECONDS
_WORD_RE = re.compile(r"[a-zA-Z0-9]{4,}")
_USER_ACTIONS = {"trashed", "starred", "skipped", "kept", "archived", "reviewed"}


def _dominant(actions: list[str]) -> tuple[str | None, float]:
    if not actions:
        return None, 0.0
    counts: dict[str, int] = {}
    for a in actions:
        counts[a] = counts.get(a, 0) + 1
    best, n = max(counts.items(), key=lambda kv: (kv[1], -0 if kv[0] == "trashed" else 0))
    return best, n / len(actions)


def prefilter(window_seconds: int | None = None) -> list[dict]:
    traces = store.traces_since(window_seconds or TRACE_WINDOW_SECONDS)
    clusters: list[dict] = []
    seen: set[tuple] = set()
    _add = lambda c: clusters.append(c) if c else None

    _add(_sender_clusters(traces))
    for c in _keyword_clusters(traces):
        _add(c)
    for c in _category_clusters(traces):
        _add(c)
    return clusters


def _sender_clusters(traces: list[dict]) -> dict | None:
    by_sender: dict[str, list[dict]] = {}
    for t in traces:
        e = (t.get("sender_email") or "").strip().lower()
        if e:
            by_sender.setdefault(e, []).append(t)
    for email, rows in by_sender.items():
        user_rows = [t for t in rows if t.get("action") in _USER_ACTIONS]
        if len(user_rows) < config.MIN_ACTIONS_FOR_PATTERN:
            continue
        recent = any(max(r.get("ts", 0) for r in user_rows) > time() - config.PATTERN_RECENT_DAYS * 86400)
        if not recent:
            continue
        dom, ratio = _dominant([r["action"] for r in user_rows])
        if dom not in ("trashed", "skipped", "starred") or ratio < 0.9:
            continue
        # promo ratio across the latest sample
        latest = sorted(rows, key=lambda r: r.get("ts", 0))[-config.RECENT_SAMPLE_SIZE:]
        promo_ratio = sum(1 for r in latest if r.get("promo")) / max(len(latest), 1)
        promo_only = promo_ratio >= config.PROMO_RATIO_FOR_PATTERN
        name = max((r.get("sender_name") or "" for r in latest), default="")
        action = dom if dom != "skipped" else "skip"
        label = f"{name or email} — {len(user_rows)}/{len(user_rows)} {dom}"
        return {
            "kind": "sender", "target": email, "label": label,
            "dominant_action": action, "promo_only": promo_only,
            "emails_per_day": None, "items": rows, "count": len(user_rows),
        }
    return None


def _keyword_clusters(traces: list[dict]) -> list[dict]:
    trashed = [t for t in traces if t.get("action") == "trashed"]
    by_word: dict[str, set[str]] = {}
    items_by_word: dict[str, list[dict]] = {}
    for t in trashed:
        words = {w.lower() for w in _WORD_RE.findall(t.get("subject") or "")}
        for w in words:
            by_word.setdefault(w, set()).add((t.get("sender_email") or "").lower())
            items_by_word.setdefault(w, []).append(t)
    out = []
    for w, senders in by_word.items():
        if len(senders) >= config.MIN_ACTIONS_FOR_PATTERN:
            out.append({"kind": "keyword", "target": w, "label": f'"{w}" in subject',
                        "dominant_action": "trash", "promo_only": False,
                        "emails_per_day": None, "items": items_by_word[w],
                        "count": len(items_by_word[w])})
    return out


def _category_clusters(traces: list[dict]) -> list[dict]:
    trashed = [t for t in traces if t.get("action") == "trashed" and t.get("category")]
    by_cat: dict[str, list[dict]] = {}
    for t in trashed:
        by_cat.setdefault(t["category"], []).append(t)
    out = []
    for cat, rows in by_cat.items():
        if cat.lower() in ("unclear", "other"):
            continue
        if len(rows) >= config.MIN_ACTIONS_FOR_PATTERN:
            out.append({"kind": "category", "target": cat, "label": cat,
                        "dominant_action": "trash", "promo_only": False,
                        "emails_per_day": None, "items": rows, "count": len(rows)})
    return out


def _frequency_cluster(traces: list[dict]) -> dict | None:
    """A single high-volume sender that is mostly trashed."""
    counts: dict[str, list[dict]] = {}
    for t in traces:
        e = (t.get("sender_email") or "").strip().lower()
        counts.setdefault(e, []).append(t)
    for email, rows in counts.items():
        user_actions = [t for t in rows if t.get("action") in _USER_ACTIONS]
        if len(user_actions) < config.MIN_ACTIONS_FOR_PATTERN // 2:
            continue
        dom, ratio = _dominant([r["action"] for r in user_actions])
        em_day = len(rows) / (TRACE_WINDOW_SECONDS / 86400)
        if dom in ("trashed", "skipped") and em_day >= config.FREQ_EMAILS_PER_DAY:
            return {"kind": "frequency", "target": email, "label": f"{email} ~{em_day:.0f}/day",
                    "dominant_action": "trash", "promo_only": False,
                    "emails_per_day": em_day, "items": rows, "count": len(rows)}
    return None
```

Note: `time()` is used above — add `from time import time` at the top of `backend/wiki.py`.

- [ ] **Step 4: Run prefilter tests, confirm pass**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_wiki_prefilter.py -v
```
Expected: 4 passed. (If the frequency cluster duplicates a sender cluster in tests, dedup is handled at evolve/inference time, not in these unit tests.)

- [ ] **Step 5: Commit**

```bash
git add backend/wiki.py backend/tests/test_wiki_prefilter.py
git commit -m "feat(wiki): deterministic prefilter for candidate clusters"
```

---

### Task 9: `backend/wiki.py` — agent roles (Wiki Maintainer + Skill Proposer)

**Files:**
- Modify: `backend/wiki.py` (append agents + run_evolve)
- Create: `backend/tests/test_wiki_agents.py`

- [ ] **Step 1: Write failing agent tests (LLM mocked)**

`backend/tests/test_wiki_agents.py`:

```python
import json
import time

from backend import store
from backend.rules import parse_skill_md
from backend.wiki import maintain_cluster, propose_rule, run_evolve


class FakeLLM:
    def __init__(self, text): self.text, self.calls = text, []
    def __call__(self, system, user):
        self.calls.append((system, user)); return self.text


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
    (maintain_cluster(cluster(), llm))
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
```

- [ ] **Step 2: Run, confirm fail**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_wiki_agents.py -v
```
Expected: FAIL — names missing.

- [ ] **Step 3: Implement agent roles + run_evolve**

Append to `backend/wiki.py`:

```python
# --- Agent roles -------------------------------------------------------------

def _ollama_agent(system: str, user: str) -> str | None:
    if not config.OLLAMA_MODEL:
        return None
    payload = {
        "model": config.OLLAMA_MODEL,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0.4, "num_predict": 600},
    }
    try:
        resp = requests.post(
            config.OLLAMA_URL.rstrip("/") + "/api/chat",
            json=payload,
            timeout=config.SUMMARY_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return (resp.json().get("message") or {}).get("content", "")
    except Exception as exc:
        logger.warning("Ollama agent unavailable: %s", exc)
        return None


def _cluster_brief(cluster: dict) -> dict:
    items = sorted(cluster.get("items", []), key=lambda r: r.get("ts", 0))[:20]
    return {
        "kind": cluster["kind"], "target": cluster.get("target"),
        "label": cluster.get("label"), "dominant_action": cluster.get("dominant_action"),
        "promo_only": cluster.get("promo_only"),
        "emails_per_day": cluster.get("emails_per_day"),
        "sample_traces": [{k: r.get(k) for k in ("sender_email", "sender_name", "subject", "promo", "category", "action")} for r in items],
    }


def maintain_cluster(cluster: dict, llm_fn=None) -> dict:
    brief = _cluster_brief(cluster)
    summary = None
    if llm_fn:
        try:
            summary = (llm_fn(
                "You are the Wiki Maintainer in a personal email triage agent. "
                "The user hates noise; observations are concise factual notes "
                "about repeated behavior. Respond with one sentence.",
                json.dumps(brief),
            ) or "").strip() or None
        except Exception:
            summary = None
    if not summary:
        summary = (f"{brief['label']}: {cluster['count']} matching "
                   f"{cluster['dominant_action']} actions observed.")
    last_seen = max((r.get("ts") or 0 for r in cluster.get("items", [])), default=time())
    signal = min(1.0, 0.5 + min(cluster.get("count", 0), 10) / 10)
    return store.upsert_observation(
        kind=cluster["kind"], target=str(cluster["target"]),
        action=cluster["dominant_action"], summary=summary,
        evidence_count=cluster.get("count", 0), signal=signal, last_seen=last_seen,
    )


def _proposal_md(cluster: dict) -> str:
    action = cluster.get("dominant_action") or "trash"
    scope = "promo_only" if cluster.get("promo_only") else "all_mail"
    for i, word in enumerate(cluster.get("items", []) or [{"sender_email": cluster.get("target")}]):
        pass
    lines = ["---",
             f"name: {cluster.get('label')}",
             "enabled: true",
             f"action: {action}",
             f"scope: {scope}",
             "---",
             "## match"]
    kind = cluster.get("kind")
    if kind in ("sender", "frequency"):
        lines.append(f"sender: {cluster.get('target')}")
    elif kind == "keyword":
        lines.append(f'subject: ["{cluster.get("target")}"]')
    elif kind == "category":
        lines.append(f'category: ["{cluster.get("target")}"]')
    if cluster.get("emails_per_day"):
        lines.append(f"emails_per_day: {int(cluster.get('emails_per_day'))}")
    lines += ["", "## about", "Proposed by the Skill Proposer agent from the wiki."]
    return "\n".join(lines) + "\n"


def propose_rule(cluster: dict, observation: dict, llm_fn=None) -> dict | None:
    from .rules import parse_skill_md, parsed_json

    md = _proposal_md(cluster)
    try:
        parsed = parse_skill_md(md)
    except Exception as exc:
        logger.warning("proposer produced invalid skill: %s", exc)
        return None
    kj = parsed_json(parsed)
    if store.identical_rule_exists(kj):
        return None
    if store.has_duplicate_proposal(md):
        return None

    summary = rationale = downside = None
    if llm_fn:
        try:
            raw = llm_fn(
                "You are the Skill Proposer for a personal email triage agent. "
                "Propose ONE rule change as JSON with keys: summary, rationale, downside. "
                "Answer ONLY with that JSON object.",
                json.dumps({"observation": observation, "cluster": _cluster_brief(cluster), "proposed_skill": md}),
            )
            data = json.loads((raw or "").strip())
            summary = str(data.get("summary") or "") or None
            rationale = str(data.get("rationale") or "") or None
            downside = str(data.get("downside") or "") or None
        except Exception:
            pass
    if not summary:
        summary = f"Auto-{parsed['action']} mail matching the pattern from {cluster.get('label')}."
    if not rationale:
        rationale = "Matches repeated behavior in your trace log."
    if not downside:
        downside = "May also match a message you meant to keep — review before approving."

    pid = store.add_proposal(
        source="proposer", label=cluster.get("label") or parsed["name"],
        summary=summary, rationale=rationale, downside=downside,
        evidence_json=json.dumps([r.get("message_id") for r in cluster.get("items", [])]),
        proposed_skill_md=md,
    )
    return {"id": pid, "proposed_skill_md": md}


def run_evolve(llm_fn=None) -> dict:
    clusters = prefilter()
    if not clusters:
        return {"created_observations": 0, "created_proposals": 0, "skipped": []}
    cap = config.EVOLVE_MAX_CLUSTERS
    created_obs = created_prop = 0
    skipped: list[str] = []
    for cluster in clusters[:cap]:
        try:
            obs = maintain_cluster(cluster, llm_fn)
            created_obs += 1
            prop = propose_rule(cluster, obs, llm_fn)
            if prop:
                created_prop += 1
            else:
                skipped.append(f"{cluster['kind']}:{cluster['target']} (duplicate)")
        except Exception as exc:
            logger.warning("evolve cluster failed: %s", exc)
            skipped.append(f"{cluster['kind']}:{cluster['target']} (error)")
    return {"created_observations": created_obs, "created_proposals": created_prop, "skipped": skipped}
```

(Also add `from time import time` if not already imported, and `import json` — add to the existing imports at the top of `backend/wiki.py`.)

- [ ] **Step 4: Run agent tests, confirm pass**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_wiki_agents.py -v
```
Expected: 5 passed. Then `python3.11 -c "import main"` still OK.

- [ ] **Step 5: Commit**

```bash
git add backend/wiki.py backend/tests/test_wiki_agents.py
git commit -m "feat(wiki): Wiki Maintainer + Skill Proposer agents, run_evolve"
```

---

### Task 10: New API endpoints (rules / wiki / proposals / traces / apply-now / evolve)

**Files:**
- Modify: `main.py` (new request models + endpoints; remove blocked/promo endpoints)
- Create: `backend/tests/test_api_rules.py`

- [ ] **Step 1: Write failing endpoint tests (direct calls + monkeypatch)**

`backend/tests/test_api_rules.py`:

```python
import pytest

from backend import store
from main import (api_rules_create, api_rules_update, api_rule_apply, api_rule_toggle,
                  api_rule_move, api_evolve, api_proposal_approve, api_proposal_reject)
from backend.rules import parse_skill_md, parsed_json


MD = "---\nname: N\naction: trash\n---\n## match\nsender: a@b.com\n"


def test_create_rule_rejects_invalid_md():
    with pytest.raises(Exception):
        api_rules_create({"skill_md": "no frontmatter"})


def test_create_rule_and_update():
    res = api_rules_create({"skill_md": MD})
    rid = res["id"]
    assert store.get_rule(rid)["skill_md"] == MD
    api_rules_update(rid, {"skill_md": MD.replace("N", "N2")})
    assert store.get_rule(rid)["parsed"]["name"] == "N2"


def test_toggle_and_move():
    a = api_rules_create({"skill_md": MD})["id"]
    b = api_rules_create({"skill_md": MD.replace("sender: a@b.com", "sender: c@d.com")})["id"]
    api_rules_update(b, {"enabled": False})
    assert store.get_rule(b)["enabled"] == 0
    api_rule_move(b, {"dir": "up"})
    rules = store.list_rules()
    assert rules[0]["id"] == b
    api_rule_toggle(a)
    assert store.get_rule(a)["enabled"] == 0


def test_apply_candidate_ids(monkeypatch):
    store.save_message({"id": "m1", "sender_email": "a@b.com", "sender_name": "A",
                        "subject": "hi", "promo": 0, "category": "Newsletter"})
    rid = api_rules_create({"skill_md": MD})["id"]
    acted = []
    monkeypatch.setattr("main.gmail_service.bulk_execute", lambda c, ids, fn: (acted.extend(ids), [])[1])
    res = api_rule_apply(rid, {"candidate_ids": ["m1"]})
    assert res["trash"] == 1
    assert "m1" in acted


def test_evolve_creates_proposal(monkeypatch):
    for i in range(3):
        store.add_trace(message_id=f"m{i}", sender_email="a@b.com", sender_name="A",
                        subject="hi", promo=0, category="Newsletter", action="trashed")
    res = api_evolve()
    assert res["created_proposals"] >= 1
    assert store.list_proposals("pending")


def test_proposal_approve_and_reject():
    pid = store.add_proposal(source="proposer", label="L", summary="s", rationale="r",
                             downside="d", evidence_json="[]", proposed_skill_md=MD)
    api_proposal_approve(pid, {"apply_now": False})
    p = store.get_proposal(pid)
    assert p["status"] == "approved" and p["rule_id"]
    pid2 = store.add_proposal(source="proposer", label="L2", summary="s", rationale="r",
                              downside="d", evidence_json="[]", proposed_skill_md=MD.replace("N", "P2"))
    api_proposal_reject(pid2)
    assert store.get_proposal(pid2)["status"] == "rejected"
```

- [ ] **Step 2: Run, confirm fail**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_api_rules.py -v
```
Expected: FAIL — names missing.

- [ ] **Step 3: Add request models + rules endpoints to `main.py`**

Add request models near the others:

```python
# --- Rules & Wiki (WikiSkill engine) -----------------------------------------

class RuleCreateRequest(BaseModel):
    skill_md: str


class RuleUpdateRequest(BaseModel):
    skill_md: str | None = None
    enabled: bool | None = None


class RuleMoveRequest(BaseModel):
    dir: str


class RuleApplyRequest(BaseModel):
    candidate_ids: list[str] | None = None


class ApprovalRequest(BaseModel):
    skill_md: str | None = None
    apply_now: bool = False
    candidate_ids: list[str] | None = None


def _parse_or_400(skill_md: str) -> dict:
    from backend.rules import RuleParseError, parse_skill_md
    try:
        return parse_skill_md(skill_md)
    except RuleParseError as exc:
        raise HTTPException(status_code=400, detail={"ok": False, "errors": exc.errors})
```

Add the endpoints (place after the Categories section or at file end before the static mount):

```python
@app.get("/api/rules")
def api_rules_list():
    rules = []
    for r in store.list_rules():
        r["counts"] = store.trace_counts_for_rule(r["id"])
        rules.append(r)
    return {"items": rules}


@app.post("/api/rules")
def api_rules_create(req: RuleCreateRequest):
    rule = _parse_or_400(req.skill_md)
    rid = store.add_rule(req.skill_md, parsed_json(rule))
    return {"ok": True, "id": rid}


@app.put("/api/rules/{rule_id}")
def api_rules_update(rule_id: int, req: RuleUpdateRequest):
    parsed = None
    if req.skill_md is not None:
        parsed = _parse_or_400(req.skill_md)
    store.update_rule(
        rule_id,
        skill_md=req.skill_md,
        parsed_json=parsed_json(parsed) if parsed is not None else None,
        enabled=(1 if req.enabled else 0) if req.enabled is not None else None,
    )
    return {"ok": True}


@app.post("/api/rules/{rule_id}/toggle")
def api_rule_toggle(rule_id: int):
    r = store.get_rule(rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="rule not found")
    store.update_rule(rule_id, enabled=0 if r["enabled"] else 1)
    return {"ok": True, "enabled": 0 if r["enabled"] else 1}


@app.post("/api/rules/{rule_id}/move")
def api_rule_move(rule_id: int, req: RuleMoveRequest):
    store.move_rule(rule_id, 1 if req.dir == "down" else -1)
    return {"ok": True}


@app.post("/api/rules/{rule_id}/apply")
def api_rule_apply(rule_id: int, req: RuleApplyRequest):
    service = require_service()
    from backend.rules import match_item
    from backend import wiki as wiki_module
    from backend.rules import frequency_map

    r = store.get_rule(rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="rule not found")
    candidates = _candidates_for_rule(service, r, req.candidate_ids or [])
    freq = frequency_map(store.traces_since(wiki_module.TRACE_WINDOW_SECONDS))
    ctx = {"frequency": freq}
    trash_ids, star_ids, skip_items = [], [], []
    for it in candidates:
        if match_item(r["parsed"], it, ctx):
            if r["parsed"]["action"] == "trash":
                trash_ids.append(it["id"])
            elif r["parsed"]["action"] == "star":
                star_ids.append(it["id"])
            else:
                skip_items.append(it)
    failed = gmail_service.bulk_execute(service, trash_ids, gmail_service.trash) if trash_ids else []
    trash_done = len(trash_ids) - len(failed)
    failed_s = gmail_service.bulk_execute(service, star_ids, gmail_service.star) if star_ids else []
    star_done = len(star_ids) - len(failed_s)
    for it in skip_items:
        store.add_skipped(it)
        _record_trace(service, it["id"], "auto_skip", rule_id=rule_id)
    for mid in (trash_ids if trash_done else []):
        pass
    if trash_done or star_done:
        for mid in trash_ids:
            if mid not in failed:
                _record_trace(service, mid, "auto_trash", rule_id=rule_id)
        for mid in star_ids:
            if mid not in failed_s:
                _record_trace(service, mid, "auto_star", rule_id=rule_id)
    return {"ok": True, "trash": trash_done, "star": star_done, "skip": len(skip_items), "failed": len(failed) + len(failed_s)}


def _candidates_for_rule(service, rule, candidate_ids: list[str]) -> list[dict]:
    """Gmail unread scan for sender rules + the client-supplied queue ids."""
    seen: dict[str, dict] = {}
    sender = rule["parsed"].get("sender")
    if sender:
        queries = []
        if "@" in sender:
            queries.append(sender.lstrip("@"))
        else:
            queries.append(sender.removeprefix("@"))
        for q in queries:
            try:
                ids = gmail_service.list_messages_page(service, q=f"from:{q} is:unread",
                                                       max_results=config.MAX_BATCH)[0]
                for m in gmail_service.get_metadata_batch(service, ids):
                    seen[m["id"]] = m
            except Exception:
                continue
    for mid in candidate_ids:
        it = _trace_item_for_message(service, mid)
        if it:
            seen[mid] = {"id": mid, **it}
    return list(seen.values())
```

Add helper `parsed_json` import to main.py (`from backend.rules import parsed_json` at module import top). Add wiki/proposals/traces/evolve endpoints:

```python
@app.get("/api/wiki")
def api_wiki():
    return {"items": store.list_observations()}


@app.post("/api/wiki/{obs_id}/dismiss")
def api_wiki_dismiss(obs_id: int):
    store.dismiss_observation(obs_id)
    return {"ok": True}


@app.post("/api/wiki/{obs_id}/create-rule")
def api_wiki_create_rule(obs_id: int):
    from backend.wiki import _cluster_for_observation
    obs = store.get_observation(obs_id)
    if not obs:
        raise HTTPException(status_code=404, detail="observation not found")
    cluster = _cluster_for_observation(obs)
    prop = propose_rule(cluster, obs, None)
    if not prop:
        raise HTTPException(status_code=409, detail="duplicate proposal already exists")
    return {"ok": True, "id": prop["id"]}


@app.get("/api/traces")
def api_traces(limit: int = 50, rule_id: int | None = None):
    return {"items": store.list_traces(limit=min(limit, 200), rule_id=rule_id)}


@app.post("/api/evolve")
def api_evolve():
    res = run_evolve()
    return {"ok": True, **res}


@app.get("/api/proposals")
def api_proposals():
    return {"items": store.list_proposals("pending")}


@app.post("/api/proposals/{proposal_id}/approve")
def api_proposal_approve(proposal_id: int, req: ApprovalRequest):
    from backend.rules import parse_skill_md, parsed_json
    p = store.get_proposal(proposal_id)
    if not p:
        raise HTTPException(status_code=404, detail="proposal not found")
    md = req.skill_md or p["proposed_skill_md"]
    rule = _parse_or_400(md)
    rid = store.add_rule(md, parsed_json(rule))
    store.approve_proposal(proposal_id, rid)
    counts = {}
    if req.apply_now:
        counts = api_rule_apply(rid, RuleApplyRequest(candidate_ids=req.candidate_ids))
    return {"ok": True, "rule_id": rid, "apply": counts}


@app.post("/api/proposals/{proposal_id}/reject")
def api_proposal_reject(proposal_id: int):
    store.reject_proposal(proposal_id, suppress_days=config.PROPOSAL_SUPPRESS_DAYS)
    return {"ok": True}
```

`run_evolve`, `propose_rule` need importing in main.py: add to existing import line `from backend import ai_summary, auth, config, gmail_service, store` → also `from backend.wiki import propose_rule, run_evolve` at top. And `parsed_json`.

Add `_cluster_for_observation` to `backend/wiki.py` (used by create-rule endpoint):

```python
def _cluster_for_observation(obs: dict) -> dict:
    """Rebuild a minimal cluster from a stored observation so the user can
    turn any wiki row into a proposal."""
    kind = obs["kind"]
    target = obs["target"]
    action = obs.get("action") or "trash"
    return {"kind": kind, "target": target, "label": obs.get("summary") or f"{target}",
            "dominant_action": action, "promo_only": kind == "sender" and action == "trash",
            "emails_per_day": None, "items": [], "count": obs.get("evidence_count") or 0}
```

(Adjust `promo_only` heuristic: for `kind == "sender"`, keep promo_only from obs if the observation summary mentions promos — acceptable MVP heuristic.)

- [ ] **Step 4: Remove obsolete blocked/promo endpoints**

Delete from `main.py`: `api_blocked`, `api_blocked_add`, `api_blocked_remove`, `api_blocked_clear`, `api_promo_blocked`, `api_promo_blocked_add`, `api_promo_blocked_remove`, and the `BlockedAddRequest`/`BlockedRemoveRequest` models. The frontend will stop calling them in Task 13/14.

- [ ] **Step 5: Run endpoint tests, confirm pass**

```bash
.venv/bin/python3.11 -m pytest backend/tests/test_api_rules.py -v
```
Expected: 7 passed. Then:
```bash
.venv/bin/python3.11 -m pytest backend/tests -v
```
Expected: all backend tests pass. Then `python3.11 -c "import main"`.

- [ ] **Step 6: Commit**

```bash
git add main.py backend/wiki.py backend/tests/test_api_rules.py
git commit -m "feat(api): rules/wiki/proposals/traces/evolve endpoints; drop blocked APIs"
```

---

### Task 11: Frontend shell — sidebar Rules & Wiki panel (HTML + CSS + state)

**Files:**
- Modify: `static/index.html` (sidebar markup)
- Modify: `static/app.css` (minor: editor pane, rule rows)
- Modify: `static/app.js` (state, element map, data loading)

- [ ] **Step 1: Replace the Blocked/Promo chips in the sidebar**

In `static/index.html`, replace the two chip blocks (`blockedBtn`/`blockedList` and `promoBlockedBtn`/`promoBlockedList`) with the Rules & Wiki panel:

```html
<button id="rulesBtn" class="mt-2 hidden rounded-lg border border-violet-500/40 px-2 py-1 text-[11px] text-violet-700 transition hover:border-violet-500/70 hover:text-violet-600 dark:border-violet-500/30 dark:text-violet-300 dark:hover:text-violet-200">
  Rules &amp; Wiki <span id="proposalBadge" class="hidden ml-1 rounded-full bg-red-500 px-1.5 text-[10px] text-white align-middle">0</span>
</button>
<div id="rulesPanel" class="mt-2 hidden">
  <div class="flex items-center gap-1 mb-1.5">
    <button id="rulesTabBtn" class="px-2 py-0.5 rounded text-[11px] font-bold bg-violet-500/20 text-violet-700 dark:text-violet-300">Rules</button>
    <button id="wikiTabBtn" class="px-2 py-0.5 rounded text-[11px] font-bold text-slate-500 hover:text-violet-600 dark:text-slate-400">Wiki</button>
    <button id="suggestBtn" class="ml-auto px-2 py-0.5 rounded text-[11px] font-bold border border-violet-500/40 text-violet-700 hover:bg-violet-500/20 dark:text-violet-300">Suggest</button>
  </div>
  <div id="rulesTab" class="space-y-2"></div>
  <div id="wikiTab" class="hidden space-y-2"></div>
  <div id="rulesFooter" class="mt-2 text-[10px] text-slate-500 dark:text-slate-600"></div>
</div>
<ul id="blockedList" class="hidden"></ul>
```

Also add the editor pane (hidden) right after the `main` content area opening tag (so it can overlay the middle pane):

```html
<div id="editorPane" class="hidden flex-1 min-h-0 flex-col gap-3 p-4 overflow-y-auto bg-slate-100 dark:bg-slate-950">
  <div class="flex items-center gap-2 flex-wrap">
    <h2 id="editorTitle" class="text-sm font-bold text-slate-800 dark:text-slate-200">Edit rule</h2>
    <span id="editorMeta" class="text-[11px] text-slate-500"></span>
  </div>
  <textarea id="editorMd" spellcheck="false" class="w-full flex-1 min-h-[300px] rounded-xl border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 p-3 font-mono text-xs text-slate-800 dark:text-slate-200 outline-none focus:border-violet-500"></textarea>
  <div id="editorErrors" class="hidden rounded-lg bg-red-500/10 border border-red-500/30 p-2 text-[11px] text-red-700 dark:text-red-300"></div>
  <div class="flex items-center gap-2 flex-wrap">
    <label class="flex items-center gap-1.5 text-[11px] text-slate-600 dark:text-slate-400 cursor-pointer">
      <input id="editorApplyNow" type="checkbox" class="accent-violet-600"> apply to existing mail now
    </label>
    <button id="editorSave" class="rounded bg-violet-600 px-3 py-1 text-[11px] font-bold text-white hover:bg-violet-700 transition">Save</button>
    <button id="editorCancel" class="rounded bg-slate-500/15 px-3 py-1 text-[11px] font-bold text-slate-600 dark:text-slate-300 hover:bg-slate-500/30 transition">Cancel</button>
    <button id="editorDelete" class="ml-auto rounded bg-red-500/15 px-3 py-1 text-[11px] font-bold text-red-700 dark:text-red-300 hover:bg-red-500/30 transition">Delete rule</button>
  </div>
</div>
```

Update the footer legend line `P`/`B` text stays; add nothing new.

- [ ] **Step 2: `app.js` state + element map + initial data load**

In the `state` object add:

```js
  rules: [],                 // [{...rule, counts:{total,recent}}]
  proposals: [],             // pending proposals
  wikiObs: [],               // wiki observations
  showRulesPanel: false,
  rulesTab: "rules",         // "rules" | "wiki"
  editingRuleId: null,       // rule id being edited, or "new:{skill_md}"
```

Element map additions:

```js
  rulesBtn: $("rulesBtn"), proposalBadge: $("proposalBadge"), rulesPanel: $("rulesPanel"),
  rulesTabBtn: $("rulesTabBtn"), wikiTabBtn: $("wikiTabBtn"), suggestBtn: $("suggestBtn"),
  rulesTab: $("rulesTab"), wikiTab: $("wikiTab"), rulesFooter: $("rulesFooter"),
  editorPane: $("editorPane"), editorTitle: $("editorTitle"), editorMeta: $("editorMeta"),
  editorMd: $("editorMd"), editorErrors: $("editorErrors"), editorApplyNow: $("editorApplyNow"),
  editorSave: $("editorSave"), editorCancel: $("editorCancel"), editorDelete: $("editorDelete"),
```

Remove `blockedBtn/blockedCount/blockedList/promoBlockedBtn/promoBlockedCount/promoBlockedList` from the map and the old sidebar render checks (the `nBlock`/`nPromo` blocks in `renderSidebar`).

- [ ] **Step 3: Load rules/proposals/wiki at boot**

In `loadQueue`, replace the blocked/promo fetches:

```js
    const [data, skippedRes, rulesRes, proposalsRes, wikiRes] = await Promise.all([
      api(`/api/queue?max_results=${BATCH}`),
      api("/api/skipped").catch(() => ({ items: [] })),
      api("/api/rules").catch(() => ({ items: [] })),
      api("/api/proposals").catch(() => ({ items: [] })),
      api("/api/wiki").catch(() => ({ items: [] })),
    ]);
```
and replace `state.blockedSenders = ...` / `state.promoBlockedSenders = ...` with:

```js
    state.rules = rulesRes.items || [];
    state.proposals = proposalsRes.items || [];
    state.wikiObs = wikiRes.items || [];
```

Update the auto-delete toasts in `loadQueue`/`loadMore` to the new counts (see Task 13).

- [ ] **Step 4: Verify frontend invariants**

```bash
node --check static/app.js && echo JS_OK
```
Also run the el-map check (must be clean after Task 12 wires everything).

- [ ] **Step 5: Commit**

```bash
git add static/index.html static/app.css static/app.js
git commit -m "feat(ui): sidebar Rules & Wiki panel shell + editor pane"
```

---

### Task 12: Frontend — render sidebar panel (proposals, rules, wiki)

**Files:**
- Modify: `static/app.js` (`renderRulesPanel`, helpers)

- [ ] **Step 1: Implement render logic**

Add after `renderSidebar` (replacing the old chip blocks entirely):

```js
function escr(v) { return v == null ? "" : esc(String(v)); }

function proposalRow(p) {
  return `<li class="rounded-lg border border-red-500/40 bg-red-500/5 p-2 text-[11px]">
    <div class="font-bold text-red-700 dark:text-red-300">PROPOSAL — ${escr(p.label)}</div>
    <p class="mt-1 text-slate-600 dark:text-slate-300">${escr(p.summary)}</p>
    <div class="mt-1 flex flex-wrap gap-2 text-[10px] text-slate-500 dark:text-slate-400">
      <button data-proposalact="why" data-id="${escr(p.id)}" class="hover:text-violet-600 underline">why</button>
      <button data-proposalact="downside" data-id="${escr(p.id)}" class="hover:text-violet-600 underline">what could go wrong</button>
      <span>${Array.isArray(p.evidence ? [] : []) ? "" : ""}</span>
    </div>
    <div class="mt-1.5 flex items-center gap-1.5">
      <button data-proposalact="approve" data-id="${escr(p.id)}" class="rounded bg-emerald-500/15 px-2 py-0.5 text-[10px] font-bold text-emerald-700 dark:text-emerald-300 hover:bg-emerald-500/30">approve</button>
      <button data-proposalact="edit" data-id="${escr(p.id)}" class="rounded bg-violet-500/15 px-2 py-0.5 text-[10px] font-bold text-violet-700 dark:text-violet-300 hover:bg-violet-500/30">edit skill</button>
      <button data-proposalact="reject" data-id="${escr(p.id)}" class="rounded bg-slate-500/15 px-2 py-0.5 text-[10px] font-bold text-slate-600 dark:text-slate-400 hover:bg-slate-500/30">reject</button>
    </div>
    <div id="proposaldetail-${escr(p.id)}" class="hidden mt-1 text-slate-600 dark:text-slate-300"></div>
  </li>`;
}

function ruleRow(r) {
  const c = r.counts || { total: 0, recent: 0 };
  const action = r.parsed && r.parsed.action ? r.parsed.action : "trash";
  const scope = r.parsed && r.parsed.scope === "promo_only" ? "promos only" : "all mail";
  return `<li class="rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900/60 p-2 text-[11px]" data-rule="${escr(r.id)}">
    <div class="flex items-center gap-1.5">
      <span class="min-w-0 flex-1 truncate font-bold text-slate-800 dark:text-slate-200">${escr(r.parsed.name)}</span>
      <button data-ruleact="toggle" data-id="${escr(r.id)}" class="rounded-full px-2 py-0.5 text-[10px] font-bold ${r.enabled ? "bg-emerald-500/20 text-emerald-700 dark:text-emerald-300" : "bg-slate-500/15 text-slate-500"}">${r.enabled ? "on" : "off"}</button>
    </div>
    <div class="mt-0.5 text-[10px] text-slate-500 dark:text-slate-400">
      <span class="rounded bg-slate-500/15 px-1 py-0.5">${action}</span>
      <span class="ml-1 rounded bg-slate-500/15 px-1 py-0.5">${scope}</span>
      <span class="ml-1">${c.total} ${action}d · ${c.recent} recent</span>
    </div>
    <div class="mt-1.5 flex items-center gap-1.5">
      <button data-ruleact="edit" data-id="${escr(r.id)}" class="rounded bg-violet-500/15 px-2 py-0.5 text-[10px] font-bold text-violet-700 dark:text-violet-300 hover:bg-violet-500/30">edit skill</button>
      <button data-ruleact="apply" data-id="${escr(r.id)}" class="rounded bg-amber-500/15 px-2 py-0.5 text-[10px] font-bold text-amber-700 dark:text-amber-300 hover:bg-amber-500/30">apply now</button>
      <button data-ruleact="up" data-id="${escr(r.id)}" class="text-slate-500 hover:text-violet-600">▲</button>
      <button data-ruleact="down" data-id="${escr(r.id)}" class="text-slate-500 hover:text-violet-600">▼</button>
      <button data-ruleact="delete" data-id="${escr(r.id)}" class="ml-auto text-red-500 hover:text-red-700">×</button>
    </div>
    <div class="mt-1.5" id="ruledetail-${escr(r.id)}"></div>
  </li>`;
}

function wikiRow(o) {
  return `<li class="rounded-lg border border-violet-500/30 bg-violet-500/5 p-2 text-[11px]">
    <div class="flex items-center gap-1.5">
      <span class="min-w-0 flex-1 text-slate-800 dark:text-slate-200">${escr(o.summary)}</span>
      <span class="shrink-0 text-[10px] text-violet-600 dark:text-violet-300">${Math.round((o.signal || 0) * 100)}%</span>
    </div>
    <div class="mt-1 text-[10px] text-slate-500 dark:text-slate-400">${o.evidence_count} evidence</div>
    <div class="mt-1.5 flex items-center gap-1.5">
      <button data-wikiract="create" data-id="${escr(o.id)}" class="rounded bg-emerald-500/15 px-2 py-0.5 text-[10px] font-bold text-emerald-700 dark:text-emerald-300 hover:bg-emerald-500/30">turn into rule</button>
      <button data-wikiract="dismiss" data-id="${escr(o.id)}" class="rounded bg-slate-500/15 px-2 py-0.5 text-[10px] font-bold text-slate-600 dark:text-slate-400 hover:bg-slate-500/30">dismiss</button>
    </div>
  </li>`;
}

function renderRulesPanel() {
  const badge = state.proposals.length;
  if (badge) {
    el.proposalBadge.textContent = badge;
    el.proposalBadge.classList.remove("hidden");
  } else {
    el.proposalBadge.classList.add("hidden");
  }
  el.rulesBtn.classList.remove("hidden");
  el.rulesPanel.classList.toggle("hidden", !state.showRulesPanel);
  if (!state.showRulesPanel) return;

  el.rulesTabBtn.className = state.rulesTab === "rules" ? "px-2 py-0.5 rounded text-[11px] font-bold bg-violet-500/20 text-violet-700 dark:text-violet-300" : "px-2 py-0.5 rounded text-[11px] font-bold text-slate-500 dark:text-slate-400";
  el.wikiTabBtn.className = state.rulesTab === "wiki" ? "px-2 py-0.5 rounded text-[11px] font-bold bg-violet-500/20 text-violet-700 dark:text-violet-300" : "px-2 py-0.5 rounded text-[11px] font-bold text-slate-500 dark:text-slate-400";
  el.rulesTab.classList.toggle("hidden", state.rulesTab !== "rules");
  el.wikiTab.classList.toggle("hidden", state.rulesTab !== "wiki");

  const proposalsHTML = state.proposals.map(proposalRow).join("");
  const rulesHTML = state.rules.map(ruleRow).join("");
  el.rulesTab.innerHTML = `${proposalsHTML ? `<ul class="space-y-2">${proposalsHTML}</ul>` : ""}${rulesHTML ? `<ul class="space-y-2">${rulesHTML}</ul>` : ""}`;
  el.rulesFooter.textContent = state.rules.length ? `${state.rules.length} rule${state.rules.length === 1 ? "" : "s"} · first match wins` : "No rules yet — use B (block) or P (promo) on an email to create one.";
  el.wikiTab.innerHTML = state.wikiObs.map(wikiRow).join("");
}
```

Call `renderRulesPanel()` from `renderSidebar()`. Replace the old `nBlock`/`nPromo` blocks with a single call.

- [ ] **Step 2: Wire panel toggles**

Add listeners (near the other `el.*Btn.addEventListener` calls):

```js
el.rulesBtn.addEventListener("click", () => { state.showRulesPanel = !state.showRulesPanel; renderRulesPanel(); });
el.rulesTabBtn.addEventListener("click", () => { state.rulesTab = "rules"; renderRulesPanel(); });
el.wikiTabBtn.addEventListener("click", () => { state.rulesTab = "wiki"; renderRulesPanel(); });
el.suggestBtn.addEventListener("click", async () => { await runEvolve(); });
```

- [ ] **Step 3: Verify syntax + el-map**

```bash
node --check static/app.js && echo JS_OK
```
Run el-map script; verify no `el.*` references are unmapped (the new ids all exist in index.html). Fix any stragglers.

- [ ] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "feat(ui): render proposals, rules, wiki in the Rules & Wiki panel"
```

---

### Task 13: Frontend — proposals/wiki actions + evolve trigger

**Files:**
- Modify: `static/app.js`

- [ ] **Step 1: Implement action handlers**

Add a delegated click handler (extend the existing `document.addEventListener("click", ...)` that handles `[data-cardact]`, or a new block):

```js
document.addEventListener("click", (e) => {
  const p = e.target.closest("[data-proposalact]");
  if (p) {
    const id = p.dataset.id;
    const act = p.dataset.proposalact;
    if (act === "approve" || act === "edit") approveOrEditProposal(id, act === "edit");
    else if (act === "reject") rejectProposal(id);
    else if (act === "why" || act === "downside") {
      const detail = $p(`#proposaldetail-${id}`);
      const body = act === "why" ? psum(id).rationale : psum(id).downside;
      detail.textContent = body || "n/a";
      detail.classList.toggle("hidden");
    }
    return;
  }
  const r = e.target.closest("[data-ruleact]");
  if (r) {
    const id = Number(r.dataset.id);
    const act = r.dataset.ruleact;
    if (act === "edit") openRuleEditor(id);
    else if (act === "toggle") toggleRule(id);
    else if (act === "up" || act === "down") moveRule(id, act === "down" ? "down" : "up");
    else if (act === "apply") applyRule(id);
    else if (act === "delete") deleteRule(id);
    else if (act === "seelatest") showRuleEvidence(Number(r.dataset.rule), id);
    return;
  }
  const w = e.target.closest("[data-wikiract]");
  if (w) {
    const id = Number(w.dataset.id);
    if (w.dataset.wikiract === "create") createRuleFromWiki(id);
    else dismissWiki(id);
  }
});
```

Helper `$p` alias: `const $p = (sel) => document.querySelector(sel);` (add near top). And:

```js
function psum(id) { return state.proposals.find((pp) => String(pp.id) === String(id)) || { rationale: "", downside: "" }; }

async function rejectProposal(id) {
  await api(`/api/proposals/${id}/reject`, { method: "POST", body: "{}" }).catch((err) => toast(`Reject failed: ${err.message}`, "err", { duration: 3000 }));
  await refreshRulesWiki();
  renderRulesPanel();
  toast("Proposal rejected — will not re-offer for 30 days", "info", { duration: 2400 });
}

async function approveOrEditProposal(id, editMode) {
  const p = psum(id);
  if (editMode) {
    openRuleEditor(`proposal:${id}`, p.proposed_skill_md);
    return;
  }
  const res = await api(`/api/proposals/${id}/approve`, { method: "POST", body: JSON.stringify({ apply_now: false }) }).catch((err) => { toast(`Approve failed: ${err.message}`, "err", { duration: 3000 }); return null; });
  if (res) {
    await refreshRulesWiki();
    renderRulesPanel();
    toast("Rule created from proposal", "ok", { duration: 2200 });
  }
}

async function refreshRulesWiki() {
  const [rulesRes, propRes, wikiRes] = await Promise.all([
    api("/api/rules").catch(() => ({ items: [] })),
    api("/api/proposals").catch(() => ({ items: [] })),
    api("/api/wiki").catch(() => ({ items: [] })),
  ]);
  state.rules = rulesRes.items || [];
  state.proposals = propRes.items || [];
  state.wikiObs = wikiRes.items || [];
}

async function runEvolve() {
  el.suggestBtn.disabled = true;
  try {
    const res = await api("/api/evolve", { method: "POST", body: "{}" });
    await refreshRulesWiki();
    renderRulesPanel();
    toast(`Agents found ${res.created_proposals || 0} new proposal${(res.created_proposals || 0) === 1 ? "" : "s"}`, "ok", { duration: 3000 });
  } catch (err) {
    toast(`Agent run failed: ${err.message}`, "err", { duration: 3000 });
  } finally {
    el.suggestBtn.disabled = false;
  }
}

function scheduleEvolve() {
  clearTimeout(state.evolveTimer);
  state.evolveTimer = setTimeout(() => { if (!state.queue.length) return; runEvolve(); }, 15000);
}

async function dismissWiki(id) {
  await api(`/api/wiki/${id}/dismiss`, { method: "POST", body: "{}" }).catch(() => {});
  await refreshRulesWiki(); renderRulesPanel();
}

async function createRuleFromWiki(id) {
  await api(`/api/wiki/${id}/create-rule`, { method: "POST", body: "{}" }).catch((err) => toast(`Failed: ${err.message}`, "err", { duration: 3000 }));
  await refreshRulesWiki(); renderRulesPanel();
}

async function toggleRule(id) {
  const res = await api(`/api/rules/${id}/toggle`, { method: "POST", body: "{}" }).catch(() => null);
  await refreshRulesWiki(); renderRulesPanel();
  toast(res ? `Rule ${res.enabled ? "enabled" : "disabled"}` : "Toggle failed", res ? "info" : "err", { duration: 1800 });
}

async function moveRule(id, dir) {
  await api(`/api/rules/${id}/move`, { method: "POST", body: JSON.stringify({ dir }) }).catch(() => {});
  await refreshRulesWiki(); renderRulesPanel();
}

async function deleteRule(id) {
  await api(`/api/rules/${id}`, { method: "DELETE" }).catch(() => {});
  await refreshRulesWiki(); renderRulesPanel();
  toast("Rule deleted", "info", { duration: 1600 });
}

async function applyRule(id) {
  showLoading("Applying rule to existing mail…");
  const res = await api(`/api/rules/${id}/apply`, { method: "POST", body: JSON.stringify({ candidate_ids: state.queue.map((i) => i.id) }) }).catch((err) => { toast(`Apply failed: ${err.message}`, "err", { duration: 3000 }); return null; });
  hideLoading();
  if (res) {
    toast(`Applied: ${res.trash} trashed · ${res.star} starred · ${res.skip} skipped`, "ok", { duration: 3000 });
    await refreshRulesWiki(); renderRulesPanel();
  }
}

async function showRuleEvidence(ruleId, _unused) {
  const panel = document.getElementById(`ruledetail-${ruleId}`);
  const items = await api(`/api/traces?rule_id=${ruleId}&limit=5`).catch(() => ({ items: [] }));
  const favicon = items.items.length ? "▸ " : "no trace actions yet";
  panel.innerHTML = items.items.length
    ? items.items.map((t) => `<div class="text-[10px] text-slate-500 dark:text-slate-400 truncate">${escr(t.subject || t.message_id)} · ${escr(t.action)}</div>`).join("")
    : '<div class="text-[10px] text-slate-400">no auto-actions yet</div>';
  panel.classList.toggle("hidden");
}
```

- [ ] **Step 2: Trigger evolve after queue loads**

In `loadQueue`, after `renderAll(true)`, add `scheduleEvolve();`. In `loadMore`, after success, add `scheduleEvolve();`. Update the auto-delete toasts:

```js
const at = data.auto_trashed || 0, as = data.auto_starred || 0, ask = data.auto_skipped || 0;
if (at + as + ask) {
  toast(`Auto: ${at} trashed · ${as} starred · ${ask} skipped by rules`, "info", { duration: 3000 });
}
```
(apply same to `loadMore`).

- [ ] **Step 3: Verify**

```bash
node --check static/app.js && echo JS_OK
```

- [ ] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "feat(ui): proposal/wiki/rule actions + auto evolve trigger"
```

---

### Task 14: Frontend — raw markdown editor + quick-create (Block/P)

**Files:**
- Modify: `static/app.js`
- Modify: `static/index.html` (editor pane already added in Task 11 — wire buttons here)

- [ ] **Step 1: Implement editor open/save**

```js
function openRuleEditor(ruleIdOrKey, presetMd) {
  state.editingRuleId = ruleIdOrKey;
  const rule = typeof ruleIdOrKey === "number" ? state.rules.find((r) => r.id === ruleIdOrKey) : null;
  el.editorTitle.textContent = rule ? `Edit: ${rule.parsed.name}` : (String(ruleIdOrKey).startsWith("proposal:") ? "Proposal — edit skill" : "New rule");
  const counts = rule ? (rule.counts || {}) : {};
  el.editorMeta.textContent = counts.total ? `${counts.total} auto-actions · ${counts.recent} recent — edits apply immediately once saved` : "";
  el.editorMd.value = presetMd || (rule ? rule.skill_md : "---\nname: Untitled rule\nenabled: true\naction: trash\n---\n## match\nsender: \n\n## about\n");
  el.editorErrors.classList.add("hidden");
  el.editorErrors.textContent = "";
  el.editorApplyNow.checked = false;
  el.editorDelete.classList.toggle("hidden", !rule);
  el.editorPane.classList.remove("hidden");
  // hide the group view while editing
  const gv = document.getElementById("groupView");
  if (gv) gv.classList.add("hidden");
}

function closeEditor() {
  el.editorPane.classList.add("hidden");
  const gv = document.getElementById("groupView");
  if (gv) gv.classList.remove("hidden");
  renderGroupView();
}
```

Editor Save handler:

```js
el.editorSave.addEventListener("click", async () => {
  const md = el.editorMd.value;
  const isNew = typeof state.editingRuleId !== "number" && !String(state.editingRuleId).startsWith("proposal:");
  try {
    if (isNew) {
      await api("/api/rules", { method: "POST", body: JSON.stringify({ skill_md: md }) });
    } else if (String(state.editingRuleId).startsWith("proposal:")) {
      const pid = Number(String(state.editingRuleId).split(":")[1]);
      await api(`/api/proposals/${pid}/approve`, { method: "POST", body: JSON.stringify({ skill_md: md, apply_now: el.editorApplyNow.checked, candidate_ids: state.queue.map((i) => i.id) }) });
    } else {
      await api(`/api/rules/${state.editingRuleId}`, { method: "PUT", body: JSON.stringify({ skill_md: md }) });
    }
    if (!String(state.editingRuleId).startsWith("proposal:") && !isNew && el.editorApplyNow.checked) {
      await applyRule(Number(state.editingRuleId));
    }
    closeEditor();
    await refreshRulesWiki(); renderRulesPanel();
    toast("Rule saved", "ok", { duration: 1600 });
  } catch (err) {
    const detail = err.detail || {};
    if (detail.errors && Array.isArray(detail.errors)) {
      el.editorErrors.textContent = detail.errors.map((e) => `line ${e.line}: ${e.msg}`).join("\n");
      el.editorErrors.classList.remove("hidden");
    } else {
      toast(`Save failed: ${err.message}`, "err", { duration: 3000 });
    }
  }
});

el.editorCancel.addEventListener("click", closeEditor);
el.editorDelete.addEventListener("click", async () => {
  await api(`/api/rules/${state.editingRuleId}`, { method: "DELETE" }).catch(() => {});
  closeEditor();
  await refreshRulesWiki(); renderRulesPanel();
  toast("Rule deleted", "info", { duration: 1600 });
});
```

Note: `api()` must propagate the error body detail. Update `api()` (if it doesn't already surface `err.detail`) so validation errors reach the editor. Confirm current implementation; if it throws only on `!res.ok` with text, extend it to parse JSON and set `err.detail`.

- [ ] **Step 2: Quick-create — rewrite `blockSender`/`promoBlockSender` to create rules**

Replace both functions' bodies with a shared creator:

```js
async function createSenderRule(email, senderName, promoOnly) {
  if (state.busy) return;
  state.busy = true;
  const scope = promoOnly ? "promo_only" : "all_mail";
  const title = `${promoOnly ? "Promos from " : "Block "}${(senderName || email).replace(/[|]/g, "")}`;
  const md = `---\nname: ${title}\nenabled: true\naction: trash\nscope: ${scope}\n---\n## match\nsender: ${email}\n\n## about\nAuto-${promoOnly ? "delete promos" : "block"} this sender.\n`;
  try {
    await api("/api/rules", { method: "POST", body: JSON.stringify({ skill_md: md }) });
    const doomedIds = new Set(state.queue.filter((i) =>
      (i.sender_email || "").toLowerCase() === String(email).toLowerCase() && (!promoOnly || i.promo)
    ).map((i) => i.id));
    state.queue = state.queue.filter((i) => !doomedIds.has(i.id));
    invalidateGroups();
    renderSidebar();
    await refreshRulesWiki(); renderRulesPanel();
    toast(`${promoOnly ? "Promo auto-delete on" : "Blocked"} ${senderName || email} — edit the skill in Rules & Wiki to refine`, "ok", { duration: 3200 });
    if (state.queue.length === 0) { showEmpty(); return; }
    renderGroupView();
  } catch (e) {
    toast(`Failed: ${e.message}`, "err", { duration: 4000 });
  } finally {
    state.busy = false;
  }
}

async function blockSender(email, senderName) { await createSenderRule(email, senderName, false); }
async function promoBlockSender(email, senderName) { await createSenderRule(email, senderName, true); }
```

Keep `unblockSender`/`unpromoBlockSender` deleted (no more legacy lists). Remove `state.blockedSenders`/`state.promoBlockedSenders` usages and the old sidebar chip render code (already removed in Task 11).

- [ ] **Step 3: Verify**

```bash
node --check static/app.js && echo JS_OK
```
Run el-map script — zero used-unmapped.

- [ ] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "feat(ui): markdown rule editor + Block/P quick-create rules"
```

---

### Task 15: Full verification + server restart + docs

**Files:**
- Modify: `static/index.html` (legend cleanup: update P/B wording)
- Run: full test suite + lint-style checks + smoke test

- [ ] **Step 1: Update legend text**

In `static/index.html`, update legend line `P` from `Promo auto-delete` to `Promo quick rule`:
```html
<span class="key"><b class="k amber">P</b> Promo quick rule</span>
```

- [ ] **Step 2: Run the whole backend test suite**

```bash
.venv/bin/python3.11 -m pytest backend/tests -v
```
Expected: ALL pass (89 tests: 12 parser + 10 matcher + 4 store + 2 migration + 5 engine + 1 trace ingest + 4 prefilter + 5 agents + 7 api + 4 fixtures helpers).

- [ ] **Step 3: Frontend consistency checks**

```bash
node --check static/app.js && echo JS_OK
rg -L "blockedBtn|promoBlockedBtn|blockedSenders|promoBlockedSenders|/api/blocked|/api/promo-blocked" static/app.js static/index.html
```
Expected: no files listed (all legacy references purged), or if flagged, remove those remnants now.

- [ ] **Step 4: Restart the server**

```bash
pkill -f "uvicorn main:app" 2>/dev/null; sleep 1
nohup .venv/bin/python3.11 -m uvicorn main:app --host 0.0.0.0 --port 8000 >> /tmp/gmailer.log 2>&1 &
sleep 3
curl -s http://localhost:8000/api/status
curl -s http://localhost:8000/api/rules
curl -s http://localhost:8000/api/proposals
curl -s http://localhost:8000/api/wiki
curl -s http://localhost:8000/api/traces?limit=5
```
Expected: each returns JSON (empty lists fine); `api/rules` may contain migrated rules from the earlier blocked/promo entries.

- [ ] **Step 5: Manual browser smoke**

Open `http://localhost:8000`:
1. Sidebar shows "Rules & Wiki" with any migrated rules + proposal badge if proposals exist.
2. Click a rule → `edit skill` opens the markdown editor in the middle pane; make an invalid edit (e.g. `action: explode`) → Save shows line-number errors inline.
3. `P` on a promo card creates a promo rule instantly; `B` creates a block rule.
4. `Suggest` runs the agents and populates wiki/proposals after 15s idle (or immediately on button).
5. New mail falling under a rule disappears automatically; the queue toasts the auto counts.

- [ ] **Step 6: Commit final docs state**

```bash
git add static/index.html
git commit -m "docs: finalize Rules & Wiki wiring; update legend"
```
(Anything from Step 3 cleanup included in this commit; if no changes, skip commit.)

---

## Self-Review Notes (run after writing, then remove this section)

1. **Spec coverage cross-check** — every spec section maps to a task: §3 rule format → Task 2; §4 tables → Task 4; §5 engine → Tasks 6–7; §6 agents/loop → Tasks 8–9, endpoints → Task 10; §7 API → Task 10; §8 frontend → Tasks 11–14; §9 errors → Tasks 10/14 (inline errors); §10 testing → throughout; §11 out-of-scope → respected (no archive/no regex/scheduling).
2. **Type consistency** — `parsed_json(rule)` returns `str` and stored in `rules.parsed_json`; `match_item(rule, item, ctx)` with `ctx.frequency` built by `frequency_map(traces)`; proposals store `proposed_skill_md` re-parseable by `parse_skill_md`. `run_evolve`, `propose_rule`, `maintain_cluster` names consistent across tasks. Endpoint names in Task 10 match the imports in `test_api_rules.py`.
3. **Placeholders** — none; all code blocks are concrete.