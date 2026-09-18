from __future__ import annotations

import logging
import threading
import time

from . import ai_summary, auth, config, store

logger = logging.getLogger("gmailer.summarizer")

_INTERVAL_SECONDS = 60
_PACE_SECONDS = 1.0

# Categories (and the sender_map categories that imply them) whose messages are
# summarized first by the background worker. Kept in sync with the plan's
# CATEGORY_PRIORITY: Finance/Bill always leads.
_CATEGORY_PRIORITY = {"finance/bill", "banks", "services"}

_stop = threading.Event()
_wake = threading.Event()
_thread: threading.Thread | None = None

_URGENT_MAX = 200
_urgent: set[str] = set()


def mark_urgent(message_id: str) -> None:
    if not message_id:
        return
    _urgent.add(message_id)
    if len(_urgent) > _URGENT_MAX:
        for mid in list(_urgent)[: len(_urgent) - _URGENT_MAX]:
            _urgent.discard(mid)


def nudge() -> None:
    _wake.set()


def _load_priority_senders() -> list[dict]:
    try:
        return store.get_sender_map()
    except Exception:
        return []


def _load_highlight_categories() -> set[str]:
    raw = ""
    try:
        raw = store.get_setting("highlight_categories", "Finance/Bill")
    except Exception:
        pass
    extra = {c.strip().lower() for c in (raw or "").split(",") if c.strip()}
    return _CATEGORY_PRIORITY | extra


def _sender_matches(msg: dict, pattern: str) -> bool:
    pat = (pattern or "").lower()
    if not pat or "@" not in pat:
        email = (msg.get("sender_email") or "").strip().lower()
        name = (msg.get("sender_name") or "").strip().lower()
        return pat in email or pat in name
    return pat in (msg.get("sender_email") or "").strip().lower()


def _is_priority(msg: dict, mappings: list[dict], highlight: set[str]) -> bool:
    cat = (msg.get("category") or "").strip().lower()
    if cat in highlight:
        return True
    sum_cat = ((msg.get("summary") or {}).get("category") or "").strip().lower()
    if sum_cat in highlight:
        return True
    email = (msg.get("sender_email") or "").strip().lower()
    if email:
        for m in mappings:
            if (m.get("category") or "").strip().lower() in highlight and _sender_matches(msg, m.get("pattern", "")):
                return True
    return False


def summarize_needy(limit: int | None = None) -> int:
    """Summarize up to `limit` cached messages that lack a summary.

    Runs only when the user is authenticated (a saved token exists) and
    QUEUE_SUMMARIZE_BATCH is enabled. Finance/Bill-priority messages (and
    bank/service senders) are summarized first. Each message is summarized
    sequentially; already-summarized, body-less, or skip-AI senders are
    left alone. Returns the number summarized this cycle.
    """
    if not auth.token_exists():
        return 0
    if limit is None:
        limit = config.QUEUE_SUMMARIZE_BATCH
    if limit <= 0:
        return 0

    mappings = _load_priority_senders()
    highlight = _load_highlight_categories()
    want: list[dict] = []
    for m in store.list_messages(limit=config.MAX_BATCH):
        if ai_summary.summary_is_real(m.get("summary")):
            _urgent.discard(m.get("id") or "")
            continue
        if not m.get("body_text"):
            continue
        sender = (m.get("sender_email") or "").strip().lower()
        if sender and store.is_summary_skipped(sender):
            continue
        want.append(m)

    want.sort(key=lambda m: (
        0 if (m.get("id") or "") in _urgent else 1,
        0 if _is_priority(m, mappings, highlight) else 1,
        -(m.get("internal_date_ms") or 0),
    ))

    done = 0
    for m in want[:limit]:
        if _stop.is_set():
            break
        summary = ai_summary.generate_summary(m)
        if summary:
            done += 1
            _urgent.discard(m.get("id") or "")
        time.sleep(_PACE_SECONDS)
    if done:
        logger.info("background summarizer produced %d summary/s", done)
    return done


def _loop() -> None:
    while not _stop.is_set():
        try:
            summarize_needy()
        except Exception:
            logger.exception("background summarizer cycle failed")
        _wake.wait(_INTERVAL_SECONDS)
        _wake.clear()


def start() -> None:
    global _thread
    if _thread is not None:
        return
    if config.QUEUE_SUMMARIZE_BATCH <= 0:
        logger.info("background summarizer disabled (QUEUE_SUMMARIZE_BATCH <= 0)")
        return
    _thread = threading.Thread(target=_loop, name="bg-summarizer", daemon=True)
    _thread.start()
    logger.info(
        "background summarizer started (batch=%d, interval=%ds)",
        config.QUEUE_SUMMARIZE_BATCH,
        _INTERVAL_SECONDS,
    )


def stop() -> None:
    _stop.set()
    _wake.set()
