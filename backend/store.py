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


# --- Blocked senders (auto-delete) ---------------------------------------------
# Blocked sender emails are persisted here and matched against the live queue on
# every pull; any unread message from a blocked sender is trashed immediately, so
# new mail from them never surfaces again.

def add_blocked(email: str, sender_name: str | None = None) -> None:
    if not email:
        return
    try:
        with _lock:
            _get().execute(
                "INSERT OR REPLACE INTO blocked (email, sender_name, blocked_at) VALUES (?,?,?)",
                (email, sender_name or "", time.time()),
            )
            _get().commit()
    except Exception as exc:
        logger.warning("store.add_blocked failed: %s", exc)


def remove_blocked(email: str) -> None:
    try:
        with _lock:
            _get().execute("DELETE FROM blocked WHERE email = ?", (email,))
            _get().commit()
    except Exception as exc:
        logger.warning("store.remove_blocked failed: %s", exc)


def list_blocked() -> list[dict]:
    try:
        with _lock:
            rows = _get().execute(
                "SELECT email, sender_name, blocked_at FROM blocked ORDER BY blocked_at"
            ).fetchall()
    except Exception as exc:
        logger.warning("store.list_blocked failed: %s", exc)
        return []
    return [
        {"email": r["email"], "sender_name": r["sender_name"], "blocked_at": r["blocked_at"]}
        for r in rows
    ]


def clear_blocked() -> int:
    cleared = 0
    try:
        with _lock:
            row = _get().execute("SELECT COUNT(*) FROM blocked").fetchone()
            if row:
                cleared = int(row[0])
            _get().execute("DELETE FROM blocked")
            _get().commit()
    except Exception as exc:
        logger.warning("store.clear_blocked failed: %s", exc)
    return cleared


# --- Promo auto-delete (promo-only senders) -----------------------------------
# Like blocked senders, but only PROMO-labelled mail from the vendor is trashed;
# regular (non-promo) mail keeps flowing. Matched on every queue pull.

def add_promo_blocked(email: str, sender_name: str | None = None) -> None:
    if not email:
        return
    try:
        with _lock:
            _get().execute(
                "INSERT OR REPLACE INTO promo_blocked (email, sender_name, promo_blocked_at) VALUES (?,?,?)",
                (email, sender_name or "", time.time()),
            )
            _get().commit()
    except Exception as exc:
        logger.warning("store.add_promo_blocked failed: %s", exc)


def remove_promo_blocked(email: str) -> None:
    try:
        with _lock:
            _get().execute("DELETE FROM promo_blocked WHERE email = ?", (email,))
            _get().commit()
    except Exception as exc:
        logger.warning("store.remove_promo_blocked failed: %s", exc)


def list_promo_blocked() -> list[dict]:
    try:
        with _lock:
            rows = _get().execute(
                "SELECT email, sender_name, promo_blocked_at FROM promo_blocked ORDER BY promo_blocked_at"
            ).fetchall()
    except Exception as exc:
        logger.warning("store.list_promo_blocked failed: %s", exc)
        return []
    return [
        {"email": r["email"], "sender_name": r["sender_name"], "promo_blocked_at": r["promo_blocked_at"]}
        for r in rows
    ]