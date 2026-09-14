"""Local SQLite cache for fetched messages.

Purpose: survive page reloads. The queue is always re-fetched live from Gmail
(so it reflects deletes/archives done elsewhere), but bodies and AI summaries
we already pulled are stored here and reused instead of hitting Gmail/Ollama
again. "Reload" re-pulls metadata but hydrates instantly from cache; "Clear
cache" wipes the table so everything re-fetches from scratch.

Thread-safety: the Gmail API handler runs on FastAPI's threadpool, so writes
go through a single lock. Not a cache for multi-user data — single-user tool.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

from . import config
from .rules import parse_skill_md, parsed_json, rule_md_for_sender

logger = logging.getLogger("gmailer.store")

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    sender_name     TEXT,
    sender_email    TEXT,
    subject         TEXT,
    snippet         TEXT,
    internal_date_ms INTEGER,
    label_ids       TEXT,
    promo           INTEGER DEFAULT 0,
    body_text       TEXT,
    body_truncated  INTEGER DEFAULT 0,
    summary         TEXT,
    category        TEXT,
    fetched_at      REAL
);

CREATE TABLE IF NOT EXISTS categories (
    name        TEXT PRIMARY KEY,
    created_at  REAL
);

CREATE TABLE IF NOT EXISTS skipped (
    id          TEXT PRIMARY KEY,
    item        TEXT NOT NULL,
    skipped_at  REAL
);

CREATE TABLE IF NOT EXISTS blocked (
    email       TEXT PRIMARY KEY,
    sender_name TEXT,
    blocked_at  REAL
);

CREATE TABLE IF NOT EXISTS promo_blocked (
    email       TEXT PRIMARY KEY,
    sender_name TEXT,
    promo_blocked_at REAL
);

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
    observation_id    INTEGER,
    rule_id           INTEGER,
    status            TEXT DEFAULT 'pending',
    created_at        REAL,
    rejected_until    REAL
);
"""

_DEFAULT_CATEGORIES = [
    "Newsletter",
    "Share/Stock",
    "School",
    "Offer/Deal",
    "News",
    "Finance/Bill",
    "Personal",
    "Unclear",
    "Other",
]


def _get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
        try:
            # Older databases created before the column existed.
            _conn.execute("ALTER TABLE messages ADD COLUMN category TEXT")
        except (sqlite3.OperationalError, sqlite3.ProgrammingError):
            pass  # column already present (new schema or a prior run)
        try:
            _conn.execute("ALTER TABLE rule_proposals ADD COLUMN observation_id INTEGER")
        except (sqlite3.OperationalError, sqlite3.ProgrammingError):
            pass  # column already present (new schema or a prior run)
        _seed_default_categories()
        _conn.commit()
    return _conn


def _seed_default_categories() -> None:
    """Insert the default category set if the table is empty."""
    try:
        count = _get().execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        if count == 0:
            _get().executemany(
                "INSERT OR IGNORE INTO categories (name, created_at) VALUES (?, ?)",
                [(name, time.time()) for name in _DEFAULT_CATEGORIES],
            )
            _get().commit()
    except Exception as exc:
        logger.warning("store._seed_default_categories failed: %s", exc)


def save_message(msg: dict, summary: dict | None = None) -> None:
    """Persist a full message (body + labels + optional summary + category)."""
    mid = msg.get("id")
    if not mid:
        return
    summary = summary if summary is not None else msg.get("summary")
    labels = msg.get("label_ids") or []
    category = msg.get("category")
    if not category and isinstance(summary, dict):
        category = summary.get("category")
    try:
        with _lock:
            _get().execute(
                """INSERT INTO messages
                   (id, sender_name, sender_email, subject, snippet,
                    internal_date_ms, label_ids, promo, body_text,
                    body_truncated, summary, category, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     sender_name=excluded.sender_name,
                     sender_email=excluded.sender_email,
                     subject=excluded.subject,
                     snippet=excluded.snippet,
                     internal_date_ms=excluded.internal_date_ms,
                     label_ids=excluded.label_ids,
                     promo=excluded.promo,
                     body_text=excluded.body_text,
                     body_truncated=excluded.body_truncated,
                     summary=excluded.summary,
                     category=excluded.category,
                     fetched_at=excluded.fetched_at""",
                (
                    mid,
                    msg.get("sender_name"),
                    msg.get("sender_email"),
                    msg.get("subject"),
                    msg.get("snippet"),
                    msg.get("internal_date_ms"),
                    json.dumps(labels),
                    1 if _is_promo(labels) else 0,
                    msg.get("body_text"),
                    1 if msg.get("body_truncated") else 0,
                    json.dumps(summary, ensure_ascii=False) if summary else None,
                    category if category else None,
                    time.time(),
                ),
            )
            _get().commit()
    except Exception as exc:
        logger.warning("store.save_message failed: %s", exc)


def load_message(message_id: str) -> dict | None:
    """Return a full-message dict (same shape as get_full) if cached."""
    try:
        with _lock:
            row = _get().execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
    except Exception as exc:
        logger.warning("store.load failed: %s", exc)
        return None
    if row is None:
        return None
    msg = {
        "id": row["id"],
        "sender_name": row["sender_name"],
        "sender_email": row["sender_email"],
        "subject": row["subject"],
        "snippet": row["snippet"],
        "internal_date_ms": row["internal_date_ms"],
        "label_ids": json.loads(row["label_ids"] or "[]"),
        "promo": bool(row["promo"]),
        "body_text": row["body_text"] or "",
        "body_truncated": bool(row["body_truncated"]),
        "category": row["category"],
    }
    if row["summary"]:
        try:
            msg["summary"] = json.loads(row["summary"])
        except (json.JSONDecodeError, TypeError):
            pass
    return msg


def list_messages(limit: int = 500, offset: int = 0) -> list[dict]:
    """Return every cached message (DB-backed, no Gmail/OAuth needed).

    Same per-message shape as load_message. Used by the local email viewer
    so it can render real cached data without an authenticated session.
    """
    out: list[dict] = []
    try:
        with _lock:
            rows = _get().execute(
                "SELECT * FROM messages ORDER BY internal_date_ms DESC "
                "LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            ).fetchall()
    except Exception as exc:
        logger.warning("store.list_messages failed: %s", exc)
        return out
    for row in rows:
        msg = {
            "id": row["id"],
            "sender_name": row["sender_name"],
            "sender_email": row["sender_email"],
            "subject": row["subject"],
            "snippet": row["snippet"],
            "internal_date_ms": row["internal_date_ms"],
            "label_ids": json.loads(row["label_ids"] or "[]"),
            "promo": bool(row["promo"]),
            "body_text": row["body_text"] or "",
            "body_truncated": bool(row["body_truncated"]),
            "category": row["category"],
        }
        if row["summary"]:
            try:
                msg["summary"] = json.loads(row["summary"])
            except (json.JSONDecodeError, TypeError):
                pass
        out.append(msg)
    return out


def load_summary(message_id: str) -> dict | None:
    """Fast path: just the cached summary, if any."""
    try:
        with _lock:
            row = _get().execute(
                "SELECT summary FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
    except Exception:
        return None
    if not row or not row["summary"]:
        return None
    try:
        return json.loads(row["summary"])
    except (json.JSONDecodeError, TypeError):
        return None


def cache_count() -> int:
    try:
        with _lock:
            return int(_get().execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    except Exception:
        return 0


def clear_cache() -> int:
    cleared = 0
    try:
        with _lock:
            row = _get().execute("SELECT COUNT(*) FROM messages").fetchone()
            if row:
                cleared = int(row[0])
            _get().execute("DELETE FROM messages")
            _get().commit()
    except Exception as exc:
        logger.warning("store.clear failed: %s", exc)
    return cleared


def _is_promo(labels: list[str]) -> bool:
    return "CATEGORY_PROMOTIONS" in (labels or [])


# --- Email categories --------------------------------------------------------
# User-managed category list used as the enum for AI summaries. Persisted so
# the taxonomy chosen by the user survives reloads.

def seed_default_categories() -> None:
    """Insert the default category set if the table is empty."""
    try:
        with _lock:
            _seed_default_categories()
    except Exception as exc:
        logger.warning("store.seed_default_categories failed: %s", exc)


def get_categories() -> list[str]:
    """Return all category names, sorted."""
    try:
        with _lock:
            rows = _get().execute(
                "SELECT name FROM categories ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [str(r["name"]) for r in rows]
    except Exception as exc:
        logger.warning("store.get_categories failed: %s", exc)
        return list(_DEFAULT_CATEGORIES)


def add_category(name: str) -> list[str]:
    """Add a category (dedupe, case-insensitive). Return updated sorted list."""
    name = (name or "").strip()
    if name:
        try:
            with _lock:
                existing = {
                    str(r["name"]).lower(): str(r["name"])
                    for r in _get().execute("SELECT name FROM categories").fetchall()
                }
                if name.lower() not in existing:
                    _get().execute(
                        "INSERT INTO categories (name, created_at) VALUES (?, ?)",
                        (name, time.time()),
                    )
                    _get().commit()
        except Exception as exc:
            logger.warning("store.add_category failed: %s", exc)
    return get_categories()


def remove_category(name: str) -> list[str]:
    """Remove a category. Return updated sorted list."""
    name = (name or "").strip()
    if name:
        try:
            with _lock:
                _get().execute(
                    "DELETE FROM categories WHERE lower(name) = lower(?)", (name,)
                )
                _get().commit()
        except Exception as exc:
            logger.warning("store.remove_category failed: %s", exc)
    return get_categories()


# --- Skipped-for-now ---------------------------------------------------------
# "Skipped" emails stay in the inbox untouched but are hidden from the batch
# until the user asks to bring them back. Persisted so a page reload respects
# the choice ("don't show it until I request it").

def add_skipped(item: dict) -> None:
    mid = item.get("id")
    if not mid:
        return
    try:
        with _lock:
            _get().execute(
                "INSERT OR REPLACE INTO skipped (id, item, skipped_at) VALUES (?,?,?)",
                (mid, json.dumps(item, ensure_ascii=False), time.time()),
            )
            _get().commit()
    except Exception as exc:
        logger.warning("store.add_skipped failed: %s", exc)


def remove_skipped(message_id: str) -> None:
    try:
        with _lock:
            _get().execute("DELETE FROM skipped WHERE id = ?", (message_id,))
            _get().commit()
    except Exception as exc:
        logger.warning("store.remove_skipped failed: %s", exc)


def list_skipped() -> list[dict]:
    try:
        with _lock:
            rows = _get().execute(
                "SELECT item FROM skipped ORDER BY skipped_at"
            ).fetchall()
    except Exception as exc:
        logger.warning("store.list_skipped failed: %s", exc)
        return []
    items = []
    for row in rows:
        try:
            item = json.loads(row["item"])
            if item.get("id"):
                items.append(item)
        except (json.JSONDecodeError, TypeError):
            continue
    return items


def clear_skipped() -> int:
    cleared = 0
    try:
        with _lock:
            row = _get().execute("SELECT COUNT(*) FROM skipped").fetchone()
            if row:
                cleared = int(row[0])
            _get().execute("DELETE FROM skipped")
            _get().commit()
    except Exception as exc:
        logger.warning("store.clear_skipped failed: %s", exc)
    return cleared


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
    d["enabled"] = bool(d["enabled"])
    return d


def list_rules() -> list[dict]:
    rows = _get().execute("SELECT * FROM rules ORDER BY precedence").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["parsed"] = json.loads(d["parsed_json"])
        d["enabled"] = bool(d["enabled"])
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
        conn = _get()
        conn.execute("UPDATE rules SET precedence = -1 WHERE id = ?", (a["id"],))
        conn.execute("UPDATE rules SET precedence = ? WHERE id = ?", (a["precedence"], b["id"]))
        conn.execute("UPDATE rules SET precedence = ? WHERE id = ?", (b["precedence"], a["id"]))
        conn.commit()


def reorder_rules(ordered_ids: list[int]) -> None:
    """Assign precedence 1..N in the given order, avoiding UNIQUE collisions by
    first parking every rule at a negative precedence."""
    ids = [int(i) for i in ordered_ids]
    if not ids:
        return
    with _lock:
        conn = _get()
        now = time.time()
        visible = [r["id"] for r in list_rules()]
        missing = [r for r in visible if r not in ids]
        ids = ids + missing  # keep anything the client didn't send at the tail
        for i, rid in enumerate(ids):
            conn.execute("UPDATE rules SET precedence = ?, updated_at = ? WHERE id = ?",
                         (-(i + 1), now, rid))
        for i, rid in enumerate(ids):
            conn.execute("UPDATE rules SET precedence = ?, updated_at = ? WHERE id = ?",
                         (i + 1, now, rid))
        conn.commit()


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

def upsert_observation(*, kind: str, target: str, action: str = "trash", summary: str,
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
                 evidence_json: str, proposed_skill_md: str, observation_id: int | None = None) -> int:
    with _lock:
        cur = _get().execute(
            "INSERT INTO rule_proposals (source, label, summary, rationale, downside,"
            " observation_id, evidence_json, proposed_skill_md, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (source, label, summary, rationale, downside, observation_id,
             evidence_json, proposed_skill_md, time.time()),
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