from __future__ import annotations

"""AI TL;DR summary layer.

Real summaries come from a local Ollama model (fast, private, no API key).
If Ollama is unreachable or returns unusable output, we fall back to the
deterministic mock so the UI keeps working.

Payload shape stays stable for the frontend:
  {
    "one_liner": str,
    "bullets": [str, ...],
    "mock": bool,
    "model": str,
    "note": str,
    "tokens_in": int,      # real only
    "tokens_out": int,     # real only
    "latency_ms": int,     # real only
  }
"""

import json
import logging
import re
import time

import requests

from . import config, store

logger = logging.getLogger("gmailer.ai_summary")

_CACHE: dict[str, dict] = {}
_CACHE_MAX = 300

_SYSTEM_PROMPT = (
    "You are a precise email triage assistant. You will receive one email. "
    "Extract ONLY facts that are actually written in the email text: "
    "main topics, the sender's real ask or call to action, promotions, "
    "deadlines, prices, and dates. Never invent, infer, assume, or repeat "
    "details that are not in the text. "
    "Return the summary as: one_liner (one concise factual sentence under 25 "
    "words) and bullets (up to 3 short, specific facts). "
    "Output ONLY valid JSON. Never use markdown fences, never add commentary "
    "outside the JSON."
)

_SCHEMA_HINT = (
    'Output ONLY this exact JSON (no markdown, no text outside it): '
    '{"one_liner": "one concise sentence", '
    '"bullets": ["short bullet", "short bullet", "short bullet"]}'
)


def generate_summary(msg: dict) -> dict:
    mid = msg.get("id") or ""
    if mid and mid in _CACHE:
        return dict(_CACHE[mid])

    summary = _ollama_summarize(msg)
    if summary is None:
        summary = _mock_summarize(msg)

    if mid:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[mid] = summary
    return summary


# --- Ollama-backed summarizer -------------------------------------------------

def _ollama_summarize(msg: dict) -> dict | None:
    if not config.OLLAMA_MODEL:
        return None

    body = (msg.get("body_text") or msg.get("snippet") or "").strip()
    body = body[: config.SUMMARY_MAX_BODY_CHARS]
    sender = msg.get("sender_name") or msg.get("sender_email") or "Unknown sender"
    subject = msg.get("subject") or "(no subject)"

    prompt = f"From: {sender}\nSubject: {subject}\n\n{body}\n\n{_SCHEMA_HINT}"

    payload = {
        "model": config.OLLAMA_MODEL,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": "30m",
        "format": "json",
        "options": {"temperature": 0.2, "num_predict": 400},
    }

    t0 = time.perf_counter()
    try:
        resp = requests.post(
            config.OLLAMA_URL.rstrip("/") + "/api/chat",
            json=payload,
            timeout=config.SUMMARY_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Ollama summarization unavailable: %s", exc)
        return None

    content = (data.get("message") or {}).get("content", "")
    parsed = _extract_summary_json(content)
    if parsed is None:
        logger.warning("Ollama returned unparseable summary: %.160s", content)
        return None

    latency_ms = int((time.perf_counter() - t0) * 1000)
    tokens_in = int(data.get("prompt_eval_count") or 0)
    tokens_out = int(data.get("eval_count") or 0)
    parsed["mock"] = False
    parsed["model"] = f"ollama/{config.OLLAMA_MODEL}"
    parsed["tokens_in"] = tokens_in
    parsed["tokens_out"] = tokens_out
    parsed["latency_ms"] = latency_ms
    parsed["note"] = (
        f"Local {config.OLLAMA_MODEL} · {tokens_in} tokens in · "
        f"{tokens_out} out · {latency_ms / 1000:.1f}s"
    )
    return parsed


def _extract_summary_json(text: str) -> dict | None:
    """Tolerate markdown fences and duplicate keys from small local models."""
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text, flags=re.IGNORECASE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    one_liner = (data.get("one_liner") or "").strip()
    if not one_liner:
        one_liner = (data.get("summary") or data.get("response") or "").strip()
    bullets = data.get("bullets") or []
    if isinstance(bullets, str):
        bullets = [bullets]
    bullets = [str(b).strip()[:200] for b in bullets if str(b).strip()]
    if not one_liner and not bullets:
        return None
    return {"one_liner": one_liner[:260], "bullets": bullets[:3]}


# --- Deterministic fallback ---------------------------------------------------

def _mock_summarize(msg: dict) -> dict:
    sender = msg.get("sender_name") or msg.get("sender_email") or "Unknown sender"
    subject = msg.get("subject") or "(no subject)"
    snippet = (msg.get("snippet") or "").strip() or "No preview available."
    noun = "newsletter-style message" if msg.get("in_bundle") else "email"

    return {
        "one_liner": f"{sender} sent a {noun} about “{subject}”.",
        "bullets": [
            f"From: {sender}",
            f"Subject: {subject}",
            f"Snippet: {snippet[:140]}{'…' if len(snippet) > 140 else ''}",
        ],
        "mock": True,
        "model": "heuristic-v1 (mock)",
        "note": "Ollama unavailable — deterministic fallback is running.",
        "tokens_in": 0,
        "tokens_out": 0,
        "latency_ms": 0,
    }


# --- Grouped-sender summaries -------------------------------------------------
# One consolidated Ollama call per sender group, returning one summary per
# email. Falls back to per-email summaries (which themselves may fall back to
# mock) if the consolidated call fails.

_GROUP_SYSTEM_PROMPT = (
    "You are a precise email triage assistant. You receive several emails that "
    "all come from the same sender. For EACH email, produce exactly one summary "
    "object with keys: one_liner (one concise factual sentence, under 25 words) "
    "and bullets (up to 3 short factual points). Base everything ONLY on the "
    "email's own text — never invent, infer, or copy details from another email. "
    "Return the summaries as a JSON array with one object per email, in the same "
    "order you received them. Output ONLY valid JSON."
)

_GROUP_CACHE: dict[tuple[str, str], dict] = {}
_GROUP_CACHE_MAX = 50


def clear_in_memory_caches() -> None:
    """Drop per-message + group summary caches (used on 'Clear cache')."""
    _CACHE.clear()
    _GROUP_CACHE.clear()


def summarize_group(client, group_key: str, label: str, message_ids: list[str]) -> dict:
    """Return per-email summaries for a sender group.

    One Ollama call with the whole group; cached server-side keyed by
    (group_key, sorted ids) so re-expanding the panel is instant.
    """
    import time as _time

    from . import gmail_service

    message_ids = [i for i in (message_ids or []) if i]
    if not message_ids:
        raise ValueError("No message_ids provided")

    cache_key = (group_key, ",".join(sorted(message_ids)))
    if cache_key in _GROUP_CACHE:
        return _GROUP_CACHE[cache_key]

    t0 = _time.perf_counter()
    # Pull bodies from the local cache when we already have them; fetch from
    # Gmail only for messages we've never seen. Saves the summary's re-open
    # and page reloads from hitting Gmail repeatedly.
    emails: list[dict] = []
    for mid in message_ids:
        cached = store.load_message(mid)
        if cached and cached.get("body_text"):
            emails.append(cached)
            continue
        try:
            full = gmail_service.get_full(client, mid)
            store.save_message(full)
            emails.append(full)
        except Exception as exc:
            logger.warning("group fetch failed for %s: %s", mid, exc)
            emails.append({"id": mid})

    result = _ollama_group_summarize(label, emails)
    if result is None:
        # Fall back to one individually-cached summary per email.
        sums, any_mock = [], False
        for e in emails:
            s = generate_summary(e)
            any_mock = any_mock or bool(s.get("mock"))
            sums.append({
                "id": e.get("id", ""),
                "subject": str(e.get("subject") or "(no subject)"),
                "one_liner": s.get("one_liner", ""),
                "bullets": s.get("bullets", []),
                "preview": (e.get("body_text") or e.get("snippet") or "").strip()[: config.GROUP_PREVIEW_CHARS],
                "promo": bool(e.get("promo")),
                "mock": bool(s.get("mock")),
            })
            store.save_message(e, s)
        result = {
            "summaries": sums,
            "model": "per-email fallback",
            "group_key": group_key,
            "label": label,
            "count": len(emails),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "mock_any": any_mock,
            "truncated": False,
        }

    if len(_GROUP_CACHE) >= _GROUP_CACHE_MAX:
        _GROUP_CACHE.pop(next(iter(_GROUP_CACHE)))
    _GROUP_CACHE[cache_key] = result
    return result


def _ollama_group_summarize(label: str, emails: list[dict]) -> dict | None:
    if not config.OLLAMA_MODEL:
        return None

    group = emails[: config.GROUP_MAX_EMAILS]
    parts = [f"Sender: {label or 'unknown'}", f"You have {len(group)} emails from this sender.\n"]
    for i, e in enumerate(group, 1):
        subject = (e.get("subject") or "(no subject)").strip()[:200]
        body = (e.get("body_text") or e.get("snippet") or "").strip()
        body = body[: config.GROUP_SUMMARY_BODY_CHARS]
        parts.append(f"Email {i} — Subject: {subject}\n{body}\n")
    parts.append(
        "Output ONLY this exact JSON array with exactly "
        f"{len(group)} objects, one per email, in the same order:\n"
        '[{"one_liner": "one concise sentence", "bullets": ["short bullet", "...", "..."]}]\n'
        "Do not skip any email, do not add text outside the array."
    )
    prompt = "\n\n".join(parts)

    payload = {
        "model": config.OLLAMA_MODEL,
        "system": _GROUP_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": "30m",
        "format": "json",
        "options": {"temperature": 0.2, "num_predict": 200 + 140 * len(group)},
    }

    t0 = time.perf_counter()
    try:
        resp = requests.post(
            config.OLLAMA_URL.rstrip("/") + "/api/chat",
            json=payload,
            timeout=config.SUMMARY_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Ollama group summarization unavailable: %s", exc)
        return None

    content = (data.get("message") or {}).get("content", "")
    items = _extract_group_json(content, len(group))
    if items is None:
        logger.warning("Ollama group summary unparseable: %.160s", content)
        return None

    sums = []
    for idx, e in enumerate(group):
            it = items[idx] if idx < len(items) else {}
            if isinstance(it, str):           # model returned bare one-liners
                it = {"one_liner": it, "bullets": []}
            elif not isinstance(it, dict):
                it = {}
            bullets = it.get("bullets") or []
            if isinstance(bullets, str):
                bullets = [bullets]
            one = (it.get("one_liner") or "").strip()[:300]
            summary = {
                "id": e.get("id", ""),
                "subject": str(e.get("subject") or "(no subject)"),
                "one_liner": one,
                "bullets": [str(b).strip()[:200] for b in bullets if str(b).strip()][:3],
                "preview": (e.get("body_text") or e.get("snippet") or "").strip()[: config.GROUP_PREVIEW_CHARS],
                "promo": bool(e.get("promo")),
                "mock": False,
            }
            sums.append(summary)
            if one:
                store.save_message(e, {"one_liner": one, "bullets": summary["bullets"],
                                       "mock": False, "model": f"ollama/{config.OLLAMA_MODEL}",
                                       "note": "summary from grouped call", "tokens_in": 0,
                                       "tokens_out": 0, "latency_ms": 0})

    latency_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "group_key": "",
        "label": label,
        "count": len(group),
        "summaries": sums,
        "model": f"ollama/{config.OLLAMA_MODEL}",
        "elapsed_ms": latency_ms,
        "tokens_in": int(data.get("prompt_eval_count") or 0),
        "tokens_out": int(data.get("eval_count") or 0),
        "mock_any": False,
        "truncated": len(emails) > len(group),
    }


def _extract_group_json(text: str, expected: int) -> list | None:
    """Parse a JSON array, tolerating fences or an object wrapping the array."""
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text, flags=re.IGNORECASE).strip()
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
    # Fall back: an object that wraps the list under some key.
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            data = json.loads(text[s : e + 1])
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list):
                    return v
    return None