"""WikiSkill loop: deterministic prefilter + two local Ollama agent roles."""
from __future__ import annotations

import json
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


# --- Agent roles -------------------------------------------------------------

def _cluster_for_observation(obs: dict) -> dict:
    """Rebuild a minimal cluster from a stored observation so the user can
    turn any wiki row into a proposal."""
    kind = obs["kind"]
    target = obs["target"]
    action = obs.get("action") or "trash"
    return {"kind": kind, "target": target, "label": obs.get("summary") or f"{target}",
            "dominant_action": action, "promo_only": kind == "sender" and action == "trash",
            "emails_per_day": None, "items": [], "count": obs.get("evidence_count") or 0}


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
    obs_id = store.upsert_observation(
        kind=cluster["kind"], target=str(cluster["target"]),
        action=cluster["dominant_action"], summary=summary,
        evidence_count=cluster.get("count", 0), signal=signal, last_seen=last_seen,
    )
    return store.get_observation(obs_id)


def _proposal_md(cluster: dict) -> str:
    action = cluster.get("dominant_action") or "trash"
    scope = "promo_only" if cluster.get("promo_only") else "all_mail"
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
    senders = parsed.get("sender") or []
    if isinstance(senders, str):
        senders = [senders]
    if len(senders) == 1 and not parsed.get("subject") and not parsed.get("category") \
            and not parsed.get("emails_per_day") \
            and store.rule_covers_sender(parsed.get("action"), parsed.get("scope"), senders[0]):
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
        observation_id=observation.get("id"),
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