"""WikiSkill loop: deterministic prefilter + two local Ollama agent roles."""
from __future__ import annotations

import logging
import re
from time import time

import requests

from . import config, store

logger = logging.getLogger("gmailer.wiki")

TRACE_WINDOW_SECONDS = config.TRACE_WINDOW_SECONDS
_WORD_RE = re.compile(r"[a-zA-Z0-9]{4,}")
_USER_ACTIONS = {"trashed", "starred", "skipped", "kept", "archived", "reviewed"}
_NORMALIZE_ACTION = {"trashed": "trash", "skipped": "skip", "starred": "star"}


def _dominant(actions: list[str]) -> tuple[str | None, float]:
    if not actions:
        return None, 0.0
    counts: dict[str, int] = {}
    for a in actions:
        counts[a] = counts.get(a, 0) + 1
    best, n = max(counts.items(), key=lambda kv: kv[1])
    return best, n / len(actions)


def prefilter(window_seconds: int | None = None) -> list[dict]:
    traces = store.traces_since(window_seconds or TRACE_WINDOW_SECONDS)
    clusters: list[dict] = _sender_clusters(traces)
    clusters += _keyword_clusters(traces)
    clusters += _category_clusters(traces)
    return clusters


def _sender_clusters(traces: list[dict]) -> list[dict]:
    by_sender: dict[str, list[dict]] = {}
    for t in traces:
        e = (t.get("sender_email") or "").strip().lower()
        if e:
            by_sender.setdefault(e, []).append(t)
    out: list[dict] = []
    for email, rows in by_sender.items():
        user_rows = [t for t in rows if t.get("action") in _USER_ACTIONS]
        if len(user_rows) < config.MIN_ACTIONS_FOR_PATTERN:
            continue
        recent_ts = max((r.get("ts") or 0) for r in user_rows)
        if recent_ts < time() - config.PATTERN_RECENT_DAYS * 86400:
            continue
        dom, ratio = _dominant([r["action"] for r in user_rows])
        if dom not in ("trashed", "skipped", "starred") or ratio < 0.9:
            continue
        # promo ratio across the latest sample
        latest = sorted(rows, key=lambda r: r.get("ts", 0))[-config.RECENT_SAMPLE_SIZE:]
        promo_ratio = sum(1 for r in latest if r.get("promo")) / max(len(latest), 1)
        promo_only = promo_ratio >= config.PROMO_RATIO_FOR_PATTERN
        name = max((r.get("sender_name") or "" for r in latest), default="")
        action = _NORMALIZE_ACTION.get(dom, dom)
        out.append({
            "kind": "sender", "target": email,
            "label": f"{name or email} — {len(user_rows)} {dom}",
            "dominant_action": action, "promo_only": promo_only,
            "emails_per_day": None, "items": rows, "count": len(user_rows),
        })
    return out


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