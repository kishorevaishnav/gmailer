from __future__ import annotations

"""AI TL;DR summary layer.

Real summaries come from a local Ollama model (fast, private, no API key).
If Ollama is unreachable or returns unusable output, we fall back to the
deterministic mock so the UI keeps working.

Payload shape stays stable for the frontend:
  {
    "one_liner": str,
    "bullets": [str, ...],
    "category": str,
    "action_needed": "pay|respond|review|nothing",
    "money": str,
    "is_politics": bool,
    "opinion_bias": "neutral|positive|negative|biased",
    "ai_eng_relevance": str,
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
import threading
import time

import requests

from . import config, store

logger = logging.getLogger("gmailer.ai_summary")

_CACHE: dict[str, dict] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 300

_SYSTEM_PROMPT = (
    "You are a precise email triage assistant. You will receive one email. "
    "Extract ONLY facts that are actually written in the email text: "
    "main topics, the sender's real ask or call to action, promotions, "
    "deadlines, prices, and dates. Never invent, infer, assume, or repeat "
    "details that are not in the text. "
    "Facts only. One short factual one_liner (max 20 words). Max 3 short bullet points.\n"
    "Classify each email into exactly one category from the allowed list.\n"
    'If category is unclear from content, use "unclear".\n'
    '\n'
    "Fill action_needed: \"pay\" if a payment/bill is due, \"respond\" if a reply is needed, "
    '"review" if something needs attention, "nothing" if informational.\n'
    "\n"
    "money: describe the offer/payment/bill if any — do NOT invent amounts not in the email.\n"
    "is_politics: true if political content — in that case set one_liner to \"Politics — ignored\" "
    "and bullets to empty array.\n"
    "\n"
    'opinion_bias: "negative" or "biased" if the content contains clearly negative or '
    'biased editorial opinions; otherwise "neutral" or "positive". '
    "For negative/biased, add a short explanation in bullets.\n"
    "\n"
    "ai_eng_relevance: short note if content relates to AI, ML, software engineering, "
    "or the AI engineering field — otherwise empty string.\n"
    "Output ONLY valid JSON. Never use markdown fences, never add commentary "
    "outside the JSON."
)

_DEFAULT_SYSTEM_PROMPT = _SYSTEM_PROMPT

_FALLBACK_CATEGORIES = [
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

_ACTION_NEEDED_VALUES = {"pay", "respond", "review", "nothing"}
_BIAS_VALUES = {"neutral", "positive", "negative", "biased"}

_PROMO_HINT_RE = re.compile(
    r"\b(offer|offers|deal|deals|sale|sales|discounts?|cash\s*back|earn|"
    r"reward|rewards|coupon|promo|promotions?|clearance)\b",
    re.IGNORECASE,
)


def _setting(key: str, default: str) -> str:
    try:
        val = store.get_setting(key)
        if val:
            return val
    except Exception:
        pass
    return default


def get_system_prompt() -> str:
    return _setting("ai_system_prompt", _SYSTEM_PROMPT)


def get_group_system_prompt() -> str:
    return _setting("ai_group_system_prompt", _GROUP_SYSTEM_PROMPT)


def get_category_system_prompt() -> str:
    return _setting("ai_category_system_prompt", _CATEGORY_SYSTEM_PROMPT)


def _looks_promo(msg: dict) -> bool:
    if msg.get("promo"):
        return True
    return bool(_PROMO_HINT_RE.search(msg.get("subject") or ""))


def _find_category(categories: list[str], wanted: str) -> str | None:
    for a in categories or _FALLBACK_CATEGORIES:
        if a.lower() == wanted.lower():
            return a
    return None


def sender_mapped_category(msg: dict, categories: list[str]) -> str | None:
    try:
        mappings = store.get_sender_map()
    except Exception:
        return None
    email = (msg.get("sender_email") or "").strip().lower()
    name = (msg.get("sender_name") or "").strip().lower()
    for m in mappings:
        pat = (m.get("pattern") or "").lower()
        if not pat:
            continue
        if "@" in pat:
            hit = bool(email) and pat in email
        else:
            hit = (bool(email) and pat in email) or (bool(name) and pat in name)
        if not hit:
            continue
        conds = [c.strip() for c in (m.get("subject_contains") or "").lower().split(",") if c.strip()]
        if conds and not any(c in (msg.get("subject") or "").lower() for c in conds):
            continue
        target = _find_category(categories, m.get("category") or "")
        if not target:
            continue
        if m.get("promo_sensitive") and _looks_promo(msg):
            promos = _find_category(categories, "promos")
            if promos:
                return promos
        return target
    return None
    promos = _find_category(categories, "promos")
    if _looks_promo(msg) and promos:
        return promos
    banks = _bank_category(categories)
    if banks and (category or "").lower() != "banks":
        return banks
    return category


def canonical_category(value: str, categories: list[str]) -> str | None:
    v = (value or "").strip()
    for a in categories or []:
        if a.lower() == v.lower():
            return a
    return None


def _llm_category_only(msg: dict, categories: list[str]) -> str | None:
    if not config.OLLAMA_MODEL or not categories:
        return None
    sender = msg.get("sender_name") or msg.get("sender_email") or "Unknown sender"
    subject = msg.get("subject") or "(no subject)"
    snippet = (msg.get("snippet") or "").strip()[:600]
    prompt = (f"From: {sender}\nSubject: {subject}\n\n{snippet}\n\n"
              f"Classify into exactly one of: {', '.join(categories)}. "
              "Reply with ONLY the category name, nothing else.")
    try:
        resp = requests.post(
            config.OLLAMA_URL.rstrip("/") + "/api/chat",
            json={"model": config.OLLAMA_MODEL,
                  "messages": [{"role": "user", "content": prompt}],
                  "stream": False, "keep_alive": "30m",
                  "options": {"temperature": 0, "num_predict": 30}},
            timeout=min(config.SUMMARY_TIMEOUT_SECONDS, 30),
        )
        resp.raise_for_status()
        text = ((resp.json().get("message") or {}).get("content", "") or "").strip()
    except Exception as exc:
        logger.warning("Ollama category-only unavailable: %s", exc)
        return None
    text = re.sub(r"```|\"|'", "", text).strip()
    return canonical_category(text, categories)


def recategorize_message(msg: dict, categories: list[str], force: bool = False) -> str:
    cats = categories or _allowed_categories()
    current = (msg.get("category") or "").strip()
    if msg.get("category_locked") and not force:
        return current
    mapped = sender_mapped_category(msg, cats)
    if mapped is not None:
        return mapped
    hit = canonical_category(current, cats)
    if hit:
        return hit
    return _llm_category_only(msg, cats) or current or "Unclear"


def _allowed_categories() -> list[str]:
    """Current allowed categories from the DB, falling back to defaults."""
    try:
        cats = store.get_categories()
        if cats:
            return cats
    except Exception:
        pass
    return list(_FALLBACK_CATEGORIES)


def _schema_hint(categories: list[str]) -> str:
    categories = categories or _FALLBACK_CATEGORIES
    return (
        'Output ONLY this exact JSON (no markdown, no text outside it): '
        '{"one_liner": "one concise factual sentence (max 20 words)", '
        '"bullets": ["up to 3 short factual points"], '
        f'"category": "one of: {", ".join(categories)} | unclear", '
        '"action_needed": "pay | respond | review | nothing", '
        '"money": "describe payment/offer/bill if any, else empty string", '
        '"is_politics": false, '
        '"opinion_bias": "neutral | positive | negative | biased", '
        '"ai_eng_relevance": "short note or empty string"}'
    )


def _normalize_category(value, categories: list[str]) -> str:
    v = (str(value or "").strip() or "Unclear")
    for a in categories or _FALLBACK_CATEGORIES:
        if a.lower() == v.lower():
            return a
    return "Unclear"


def _normalize_action_needed(value) -> str:
    v = (str(value or "")).strip().lower()
    return v if v in _ACTION_NEEDED_VALUES else "nothing"


def _normalize_bias(value) -> str:
    v = (str(value or "")).strip().lower()
    return v if v in _BIAS_VALUES else "neutral"


def _normalize_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return (str(value or "").strip().lower()) in ("1", "true", "yes", "y")


def _finalize_summary(d: dict) -> dict:
    """Enforce the politics handling: ignored one_liner + empty bullets."""
    if d.get("is_politics"):
        d["one_liner"] = "Politics — ignored"
        d["bullets"] = []
    return d


def generate_summary(msg: dict, force: bool = False) -> dict | None:
    mid = msg.get("id") or ""
    sender = (msg.get("sender_email") or "").strip().lower()
    if not force and sender and store.is_summary_skipped(sender):
        return None
    if not force and mid:
        with _CACHE_LOCK:
            if mid in _CACHE:
                return dict(_CACHE[mid])

    categories = _allowed_categories()
    summary = _ollama_summarize(msg, categories)
    if summary is None:
        summary = _mock_summarize(msg)
    summary["category"] = sender_mapped_category(msg, categories) or summary.get("category", "")

    if mid:
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAX:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[mid] = summary
        store.save_message(msg, summary)
    return summary


# --- Ollama-backed summarizer -------------------------------------------------

def _ollama_summarize(msg: dict, categories: list[str] | None = None) -> dict | None:
    if not config.OLLAMA_MODEL:
        return None

    body = (msg.get("body_text") or msg.get("snippet") or "").strip()
    body = body[: config.SUMMARY_MAX_BODY_CHARS]
    sender = msg.get("sender_name") or msg.get("sender_email") or "Unknown sender"
    subject = msg.get("subject") or "(no subject)"

    prompt = (
        f"From: {sender}\nSubject: {subject}\n\n{body}\n\n"
        f"{_schema_hint(categories or _allowed_categories())}"
    )

    payload = {
        "model": config.OLLAMA_MODEL,
        "system": get_system_prompt(),
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
    parsed = _extract_summary_json(content, categories or _allowed_categories())
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


def _extract_summary_json(text: str, categories: list[str] | None = None) -> dict | None:
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
    return _finalize_summary({
        "one_liner": one_liner[:260],
        "bullets": bullets[:3],
        "category": _normalize_category(data.get("category"), categories or _FALLBACK_CATEGORIES),
        "action_needed": _normalize_action_needed(data.get("action_needed")),
        "money": (str(data.get("money") or "")).strip()[:400],
        "is_politics": _normalize_bool(data.get("is_politics")),
        "opinion_bias": _normalize_bias(data.get("opinion_bias")),
        "ai_eng_relevance": (str(data.get("ai_eng_relevance") or "")).strip()[:300],
    })


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
        "category": "Unclear",
        "action_needed": "nothing",
        "money": "",
        "is_politics": False,
        "opinion_bias": "neutral",
        "ai_eng_relevance": "",
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
    "You are a precise email triage assistant. You receive several emails "
    "that all come from the same sender. For EACH email produce exactly one "
    "summary object with these keys: one_liner (one concise factual sentence, "
    "max 20 words), bullets (up to 3 short factual points), category (one from "
    "the allowed list, or 'unclear'), action_needed ('pay' if a payment/bill is "
    "due, 'respond' if a reply is needed, 'review' if something needs attention, "
    "'nothing' if informational), money (describe the offer/payment/bill if any — "
    "do NOT invent amounts), is_politics (true only for political content — then "
    "set one_liner to 'Politics — ignored' and bullets to []), opinion_bias "
    "('neutral'|'positive'|'negative'|'biased'; for negative/biased add a short "
    "explanation in bullets), ai_eng_relevance (short note if related to AI, ML, "
    "software engineering or the AI engineering field — otherwise empty string). "
    "Base everything ONLY on each email's own text — never invent, infer, or copy "
    "details from another email. Also produce: overview (one synthesized paragraph "
    "describing what these emails collectively are about) and flags (short "
    "observations like how many need payment, how many are offers, politics "
    "ignored, negative opinions, AI/engineering relevance). Output ONLY valid JSON."
)


def _normalize_email_summary(it, categories: list[str]) -> dict:
    """Coerce one raw model item into the shared per-email summary shape."""
    if isinstance(it, str):
        it = {"one_liner": it, "bullets": []}
    elif not isinstance(it, dict):
        it = {}
    bullets = it.get("bullets") or []
    if isinstance(bullets, str):
        bullets = [bullets]
    one = (it.get("one_liner") or "").strip()[:300]
    return _finalize_summary({
        "one_liner": one,
        "bullets": [str(b).strip()[:200] for b in bullets if str(b).strip()][:3],
        "category": _normalize_category(it.get("category"), categories or _FALLBACK_CATEGORIES),
        "action_needed": _normalize_action_needed(it.get("action_needed")),
        "money": (str(it.get("money") or "")).strip()[:400],
        "is_politics": _normalize_bool(it.get("is_politics")),
        "opinion_bias": _normalize_bias(it.get("opinion_bias")),
        "ai_eng_relevance": (str(it.get("ai_eng_relevance") or "")).strip()[:300],
    })


def _build_flags(sums: list[dict]) -> list[str]:
    """Deterministic flags for the fallback path."""
    flags = []
    if not sums:
        return flags
    pay = sum(1 for s in sums if s.get("action_needed") == "pay")
    offers = sum(1 for s in sums if s.get("category") and "offer" in str(s.get("category")).lower())
    politics = sum(1 for s in sums if s.get("is_politics"))
    neg = sum(1 for s in sums if s.get("opinion_bias") in ("negative", "biased"))
    ai = sum(1 for s in sums if s.get("ai_eng_relevance"))
    if pay:
        flags.append(f"{pay} need payment" if pay > 1 else "1 needs payment")
    if offers:
        flags.append(f"{offers} are offers" if offers > 1 else "1 is an offer")
    if politics:
        flags.append("politics emails ignored")
    if neg:
        flags.append("-ve opinions noted")
    if ai:
        flags.append("relevant to AI/eng")
    return flags

_GROUP_CACHE: dict[tuple[str, str], dict] = {}
_GROUP_CACHE_MAX = 50


_CATEGORY_SYSTEM_PROMPT = (
    "You are a high-level email category analyst. You receive a list of emails "
    "grouped by their assigned category. For EACH category produce ONE summary "
    "that describes what those emails are collectively about. "
    "Write a multi-line summary — be descriptive and thorough, more verbose "
    "than a single email one-liner. Cover the main themes, recurring topics, "
    "types of requests, and patterns across the emails in that category. "
    "Do NOT break the summary into subcategories or sub-groups — keep it as "
    "a single flowing description per category. "
    "Output ONLY valid JSON. Never use markdown fences, never add commentary "
    "outside the JSON."
)


def _normalize_category_summary(value) -> str:
    v = (str(value or "").strip())
    return v if v else "No summary available."


def categorize_and_summarize(messages: list[dict]) -> list[dict]:
    """Group messages by category and produce a multi-line summary per category.

    Does NOT summarize subcategories — each category gets one flowing
    multi-line summary covering all emails in that category.

    Returns list of {"category": str, "count": int, "summary": str}.
    """
    if not messages:
        return []

    groups: dict[str, list[dict]] = {}
    for m in messages:
        cat = (m.get("summary") or {}).get("category") or m.get("category") or "Unclear"
        groups.setdefault(cat, []).append(m)

    results = []
    for cat, msgs in groups.items():
        summary = _ollama_category_summary(cat, msgs)
        if summary is None:
            summary = _mock_category_summary(cat, msgs)
        results.append({"category": cat, "count": len(msgs), "summary": summary})
    return results


def _ollama_category_summary(category: str, messages: list[dict]) -> str | None:
    if not config.OLLAMA_MODEL:
        return None

    cat_msgs = messages[: config.GROUP_MAX_EMAILS]
    parts = [f"Category: {category}", f"Number of emails: {len(cat_msgs)}", ""]
    for i, e in enumerate(cat_msgs, 1):
        subject = (e.get("subject") or "(no subject)").strip()[:200]
        sender = e.get("sender_name") or e.get("sender_email") or "Unknown"
        one_liner = (e.get("summary") or {}).get("one_liner", "")
        body = (e.get("body_text") or e.get("snippet") or "").strip()
        body = body[: config.SUMMARY_MAX_BODY_CHARS]
        parts.append(f"Email {i} — From: {sender}, Subject: {subject}")
        if one_liner:
            parts.append(f"  Summary: {one_liner}")
        if body:
            parts.append(f"  Body: {body}")
        parts.append("")

    prompt = "\n".join(parts)
    prompt += (
        "\nWrite a multi-line summary for this category. Cover the main themes, "
        "recurring topics, types of requests, and patterns. Be thorough — this "
        "summary can span multiple lines. Do NOT split into subcategories."
    )

    payload = {
        "model": config.OLLAMA_MODEL,
        "system": get_category_system_prompt(),
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": "30m",
        "format": "json",
        "options": {"temperature": 0.3, "num_predict": 800},
    }

    try:
        resp = requests.post(
            config.OLLAMA_URL.rstrip("/") + "/api/chat",
            json=payload,
            timeout=config.SUMMARY_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        content = (resp.json().get("message") or {}).get("content", "")
    except Exception as exc:
        logger.warning("Ollama category summarization unavailable: %s", exc)
        return None

    parsed = _extract_category_summary_text(content)
    if parsed is None:
        logger.warning("Ollama category summary unparseable: %.160s", content)
        return None
    return parsed


def _extract_category_summary_text(text: str) -> str | None:
    """Extract the summary text from the category summary response."""
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text, flags=re.IGNORECASE).strip()
    # Try JSON first
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            data = json.loads(text[s : e + 1])
            if isinstance(data, dict):
                for key in ("summary", "category_summary", "description", "text"):
                    if isinstance(data.get(key), str) and data[key].strip():
                        return data[key].strip()
                # If it's an object with a nested summary, look deeper
                for v in data.values():
                    if isinstance(v, str) and len(v) > 20:
                        return v.strip()
        except (json.JSONDecodeError, ValueError):
            pass
    # Fall back: treat the whole text as the summary (stripped of markdown)
    if text and len(text.strip()) > 5:
        return text.strip()
    return None


def _mock_category_summary(category: str, messages: list[dict]) -> str:
    subjects = []
    for m in messages:
        s = (m.get("subject") or "(no subject)").strip()[:100]
        subjects.append(s)
    bullet_points = "\n".join(f"• {s}" for s in subjects[:10])
    if len(subjects) > 10:
        bullet_points += f"\n• ... and {len(subjects) - 10} more"
    return (
        f"{category} — {len(messages)} email{'s' if len(messages) != 1 else ''}.\n"
        f"Key subjects:\n{bullet_points}"
    )


def clear_in_memory_caches() -> None:
    """Drop per-message + group summary caches (used on 'Clear cache')."""
    with _CACHE_LOCK:
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
                "category": s.get("category", "Unclear"),
                "action_needed": s.get("action_needed", "nothing"),
                "money": s.get("money", ""),
                "is_politics": bool(s.get("is_politics")),
                "opinion_bias": s.get("opinion_bias", "neutral"),
                "ai_eng_relevance": s.get("ai_eng_relevance", ""),
                "preview": (e.get("body_text") or e.get("snippet") or "").strip()[: config.GROUP_PREVIEW_CHARS],
                "promo": bool(e.get("promo")),
                "mock": bool(s.get("mock")),
            })
            store.save_message(e, s)
        result = {
            "summaries": sums,
            "overview": (
                f"{label} sent {len(emails)} email{'s' if len(emails) != 1 else ''} "
                "from this sender; individual summaries are shown below."
            ),
            "flags": _build_flags(sums),
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
    categories = _allowed_categories()
    parts = [f"Sender: {label or 'unknown'}", f"You have {len(group)} emails from this sender.\n"]
    for i, e in enumerate(group, 1):
        subject = (e.get("subject") or "(no subject)").strip()[:200]
        body = (e.get("body_text") or e.get("snippet") or "").strip()
        body = body[: config.GROUP_SUMMARY_BODY_CHARS]
        parts.append(f"Email {i} — Subject: {subject}\n{body}\n")

    parts.append(
        "Output ONLY this exact JSON object shape (no markdown, no text outside it):\n"
        "{\n"
        '  "overview": "one synthesized paragraph about what these emails collectively are about",\n'
        '  "flags": ["short observation string", "..."] ,\n'
        '  "summaries": [\n'
    )
    cat_str = ", ".join(categories)
    obj_template = (
        "    {\"one_liner\": \"one concise factual sentence (max 20 words)\", "
        "\"bullets\": [\"up to 3 short factual points\"], "
        f"\"category\": \"one of: {cat_str} | unclear\", "
        "\"action_needed\": \"pay | respond | review | nothing\", "
        "\"money\": \"describe payment/offer/bill if any, else empty string\", "
        "\"is_politics\": false, "
        "\"opinion_bias\": \"neutral | positive | negative | biased\", "
        "\"ai_eng_relevance\": \"short note or empty string\"}"
    )
    parts.append(",\n".join([obj_template] * len(group)))
    parts.append("  ]\n}")
    parts.append(
        f"Exactly {len(group)} objects inside summaries, one per email, in the same "
        "order you received them. Do not skip any email, do not add text outside the JSON."
    )
    prompt = "\n\n".join(parts)

    payload = {
        "model": config.OLLAMA_MODEL,
        "system": get_group_system_prompt(),
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": "30m",
        "format": "json",
        "options": {"temperature": 0.2, "num_predict": 400 + 200 * len(group)},
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
    parsed = _extract_group_json(content, len(group))
    if parsed is None:
        logger.warning("Ollama group summary unparseable: %.160s", content)
        return None

    sums = []
    items = parsed["summaries"]
    for idx, e in enumerate(group):
        core = _normalize_email_summary(items[idx] if idx < len(items) else {}, categories)
        core["category"] = sender_mapped_category(e, categories) or core.get("category", "")
        summary = {
            "id": e.get("id", ""),
            "subject": str(e.get("subject") or "(no subject)"),
            **core,
            "preview": (e.get("body_text") or e.get("snippet") or "").strip()[: config.GROUP_PREVIEW_CHARS],
            "promo": bool(e.get("promo")),
            "mock": False,
        }
        sums.append(summary)
        if core["one_liner"]:
            saved = dict(core)
            saved.update(mock=False, model=f"ollama/{config.OLLAMA_MODEL}",
                         note="summary from grouped call", tokens_in=0,
                         tokens_out=0, latency_ms=0)
            store.save_message(e, saved)

    latency_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "group_key": "",
        "label": label,
        "count": len(group),
        "overview": parsed.get("overview", ""),
        "flags": parsed.get("flags", []),
        "summaries": sums,
        "model": f"ollama/{config.OLLAMA_MODEL}",
        "elapsed_ms": latency_ms,
        "tokens_in": int(data.get("prompt_eval_count") or 0),
        "tokens_out": int(data.get("eval_count") or 0),
        "mock_any": False,
        "truncated": len(emails) > len(group),
    }


def _extract_group_json(text: str, expected: int) -> dict | None:
    """Parse the grouped-summary JSON, tolerating fences, the structured object
    shape, a bare summary array (legacy), or an object wrapping the array.

    Returns {"summaries": [..], "overview": str, "flags": [..]} or None.
    """
    text = re.sub(r"```(?:json)?\s*|\s*```", "", text, flags=re.IGNORECASE).strip()
    # New shape: an object carrying summaries/overview/flags.
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e > s:
        try:
            data = json.loads(text[s : e + 1])
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, dict):
            items = data.get("summaries")
            if isinstance(items, list):
                return {
                    "summaries": items,
                    "overview": (str(data.get("overview") or "")).strip(),
                    "flags": [
                        str(f).strip()[:160]
                        for f in (data.get("flags") or [])
                        if str(f).strip()
                    ],
                }
            # Legacy fallback: an object that wraps the list under some key.
            for v in data.values():
                if isinstance(v, list):
                    return {"summaries": v, "overview": "", "flags": []}
    # Fall back: a bare array (possibly bare-string one-liners).
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, list):
            return {"summaries": data, "overview": "", "flags": []}
    return None