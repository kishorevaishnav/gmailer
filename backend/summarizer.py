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
_thread: threading.Thread | None = None


def _load_priority_senders() -> list[dict]:
    try:
        return store.get_sender_map()
    except Exception:
        return []


def _sender_matches(msg: dict, pattern: str) -> bool:
    pat = (pattern or "").lower()
    if not pat or "@" not in pat:
        email = (msg.get("sender_email") or "").strip().lower()
        name = (msg.get("sender_name") or "").strip().lower()
        return pat in email or pat in name
    return pat in (msg.get("sender_email") or "").strip().lower()


def _is_priority(msg: dict, mappings: list[dict]) -> bool:
    cat = (msg.get("category") or "").strip().lower()
    if cat in _CATEGORY_PRIORITY:
        return True
    sum_cat = ((msg.get("summary") or {}).get("category") or "").strip().lower()
    if sum_cat in _CATEGORY_PRIORITY:
        return True
    email = (msg.get("sender_email") or "").strip().lower()
    if email:
        for m in mappings:
            if (m.get("category") or "").strip().lower() in _CATEGORY_PRIORITY and _sender_matches(msg, m.get("pattern", "")):
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
    want: list[dict] = []
    for m in store.list_messages(limit=config.MAX_BATCH):
        if m.get("summary"):
            continue
        if not m.get("body_text"):
            continue
        sender = (m.get("sender_email") or "").strip().lower()
        if sender and store.is_summary_skipped(sender):
            continue
        want.append(m)

    want.sort(key=lambda m: (
        0 if _is_priority(m, mappings) else 1,
        -(m.get("internal_date_ms") or 0),
    ))

    done = 0
    for m in want[:limit]:
        if _stop.is_set():
            break
        summary = ai_summary.generate_summary(m)
        if summary:
            done += 1
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
        _stop.wait(_INTERVAL_SECONDS)


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
