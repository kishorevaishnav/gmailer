from __future__ import annotations

import json
import logging
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import ai_summary, auth, config, gmail_service, store
from backend import queue as queue_module
from backend import wiki as wiki_module
from backend.rules import RuleParseError, parse_skill_md

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("gmailer")

app = FastAPI(title="Gmailer", version="0.1.0")


@app.on_event("startup")
def _startup_migrate_legacy():
    try:
        made = store.migrate_legacy_blocked()
        if made:
            logger.info("Migrated %d legacy blocked/promo senders into rules", made)
    except Exception:
        logger.exception("Legacy blocked migration failed")


# --- Auth --------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/auth")
def start_auth():
    if not auth.credentials_exists():
        raise HTTPException(
            status_code=500,
            detail="credentials.json missing — follow the README to create it.",
        )
    url, _state = auth.start_flow()
    return RedirectResponse(url)


@app.get("/auth/callback")
def auth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    logger.info("OAuth callback: error=%r code_present=%s state_present=%s", error, bool(code), bool(state))
    if error:
        logger.error("Google OAuth returned error: %s", error)
        return RedirectResponse(f"/?auth_error=Google denied access ({error})")
    if not code or not state:
        return RedirectResponse("/?auth_error=callback missing code or state")
    try:
        auth.finish_flow(state, code)
    except Exception as exc:
        logger.exception("OAuth token exchange failed")
        return RedirectResponse(f"/?auth_error={quote(str(exc))}")
    logger.info("OAuth success — token saved")
    return RedirectResponse("/")


# --- Guards / helpers ---------------------------------------------------------

def require_service():
    if not auth.token_exists():
        raise HTTPException(status_code=401, detail="Not authenticated")
    return gmail_service.build_service()


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Gmail API call failed")
        raise HTTPException(status_code=502, detail=str(exc))


# --- Status ------------------------------------------------------------------

@app.get("/api/status")
def api_status():
    authenticated = auth.token_exists()
    email = None
    if authenticated:
        try:
            service = gmail_service.build_service()
            email = gmail_service.get_profile_email(service)
        except Exception:
            authenticated = False
    return {
        "authenticated": authenticated,
        "email": email,
        "credentials_found": auth.credentials_exists(),
        "default_batch": config.DEFAULT_BATCH,
    }


@app.post("/api/auth/logout")
def api_logout():
    auth.clear_token()
    return {"ok": True}


# --- Queue -------------------------------------------------------------------

@app.get("/api/queue")
def api_queue(max_results: int = config.DEFAULT_BATCH, page_token: str | None = None):
    service = require_service()
    max_results = max(1, min(max_results, config.MAX_BATCH))
    items, bundles, next_page_token = _run(queue_module.build_queue, service, max_results, page_token)

    kept, auto_trashed, auto_starred, auto_skipped, _rule_stats = _apply_rules(service, items)
    items = kept
    for item in items:
        # Hydrate cached summaries (and bodies) so reloads and group views
        # render instantly without re-hitting Gmail/Ollama.
        cached = store.load_message(item["id"])
        if cached:
            if cached.get("summary"):
                item["summary"] = cached["summary"]
            if cached.get("body_text"):
                item["body_cached"] = True
            if cached.get("category"):
                item["category"] = cached["category"]
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


@app.get("/api/messages/{message_id}")
def api_message(message_id: str):
    def safe_review(service):
        try:
            _record_trace(service, message_id, "reviewed")
        except Exception:
            pass

    cached = store.load_message(message_id)
    if cached and cached.get("summary"):
        safe_review(None)
        cached["from_cache"] = True
        return cached
    if cached and cached.get("body_text"):
        # Body already local; only the AI summary is stale/absent.
        cached["summary"] = ai_summary.generate_summary(cached)
        store.save_message(cached)
        safe_review(None)
        cached["from_cache"] = True
        return cached
    service = require_service()
    msg = _run(gmail_service.get_full, service, message_id)
    msg["summary"] = ai_summary.generate_summary(msg)
    store.save_message(msg)
    safe_review(service)
    return msg


@app.post("/api/cache/clear")
def api_cache_clear():
    cleared = store.clear_cache()
    ai_summary.clear_in_memory_caches()
    return {"ok": True, "cleared": cleared}


# --- Local DB viewer (no OAuth required) --------------------------------------
# Read-only listing of every cached message, so the local email viewer can
# render real data straight from the SQLite cache without an auth session.

@app.get("/api/messages")
def api_messages(limit: int = 500, offset: int = 0):
    limit = max(1, min(int(limit), 2000))
    offset = max(0, int(offset))
    items = store.list_messages(limit=limit, offset=offset)
    return {"items": items, "count": len(items), "total": store.cache_count()}


# --- Skipped-for-now ---------------------------------------------------------

class SkipAddRequest(BaseModel):
    item: dict


class SkipRemoveRequest(BaseModel):
    id: str


@app.get("/api/skipped")
def api_skipped():
    return {"items": store.list_skipped()}


@app.post("/api/skipped/add")
def api_skipped_add(req: SkipAddRequest):
    store.add_skipped(req.item)
    itm = req.item or {}
    if itm.get("id"):
        store.add_trace(
            message_id=itm["id"], action="skipped", rule_id=None,
            sender_email=itm.get("sender_email"), sender_name=itm.get("sender_name"),
            subject=itm.get("subject"), promo=bool(itm.get("promo")), category=itm.get("category"),
        )
    return {"ok": True}


@app.post("/api/skipped/remove")
def api_skipped_remove(req: SkipRemoveRequest):
    store.remove_skipped(req.id)
    return {"ok": True}


@app.post("/api/skipped/clear")
def api_skipped_clear():
    cleared = store.clear_skipped()
    return {"ok": True, "cleared": cleared}


# --- Rules (auto-delete / auto-star engine) -------------------------------------

class RuleCreateRequest(BaseModel):
    markdown: str
    enabled: bool = True


class RuleUpsertRequest(BaseModel):
    markdown: str | None = None
    enabled: bool | None = None
    precedence: int | None = None


class RuleReorderRequest(BaseModel):
    rule_ids: list[int]


class RuleMoveRequest(BaseModel):
    direction: str


class RuleConvertRequest(BaseModel):
    rule_id: int


class ProposalApproveRequest(BaseModel):
    skill_md: str | None = None
    apply_now: bool = False


class ProposalCreateRequest(BaseModel):
    source: str = "wiki"
    label: str
    summary: str | None = None
    rationale: str | None = None
    downside: str | None = None
    proposed_skill_md: str
    observation_id: int | None = None


@app.get("/api/rules")
def api_rules():
    return {"items": store.list_rules()}


@app.post("/api/rules")
def api_rules_create(req: RuleCreateRequest):
    md = (req.markdown or "").strip()
    if not md:
        raise HTTPException(status_code=400, detail="markdown required")
    try:
        parsed = parse_skill_md(md)
    except RuleParseError as exc:
        raise HTTPException(status_code=400, detail={"errors": exc.errors})
    rid = store.add_rule(md, json.dumps(parsed, sort_keys=True))
    if not req.enabled:
        store.update_rule(rid, enabled=False)
    return store.get_rule(rid)


@app.patch("/api/rules/{rule_id}")
def api_rule_update(rule_id: int, req: RuleUpsertRequest):
    existing = store.get_rule(rule_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="rule not found")
    updates: dict = {}
    if req.markdown is not None:
        md = req.markdown.strip()
        if not md:
            raise HTTPException(status_code=400, detail="markdown required")
        try:
            parsed = parse_skill_md(md)
        except RuleParseError as exc:
            raise HTTPException(status_code=400, detail={"errors": exc.errors})
        updates["skill_md"] = md
        updates["parsed_json"] = json.dumps(parsed, sort_keys=True)
    if req.enabled is not None:
        updates["enabled"] = 1 if req.enabled else 0
    if req.precedence is not None:
        updates["precedence"] = req.precedence
    if updates:
        store.update_rule(rule_id, **updates)
    return store.get_rule(rule_id)


@app.post("/api/rules/{rule_id}/move")
def api_rule_move(rule_id: int, req: RuleMoveRequest):
    if store.get_rule(rule_id) is None:
        raise HTTPException(status_code=404, detail="rule not found")
    store.move_rule(rule_id, 1 if req.direction == "down" else -1)
    return {"ok": True}


@app.delete("/api/rules/{rule_id}")
def api_rule_delete(rule_id: int):
    if store.get_rule(rule_id) is None:
        raise HTTPException(status_code=404, detail="rule not found")
    store.delete_rule(rule_id)
    return {"ok": True, "id": rule_id}


@app.post("/api/rules/reorder")
def api_rules_reorder(req: RuleReorderRequest):
    store.reorder_rules([int(i) for i in req.rule_ids])
    return {"ok": True}


@app.post("/api/rules/{rule_id}/apply-now")
def api_rule_apply_now(rule_id: int):
    service = require_service()
    if store.get_rule(rule_id) is None:
        raise HTTPException(status_code=404, detail="rule not found")
    items, _bundles, _nxt = _run(queue_module.build_queue, service, config.DEFAULT_BATCH, None)
    _kept, _t, _s, _k, rule_stats = _apply_rules(service, items)
    stats = rule_stats.get(rule_id, {"trash": 0, "star": 0, "skip": 0})
    return {"ok": True, "rule_id": rule_id, "matched": sum(stats.values()), **stats}


@app.post("/api/proposals")
def api_proposals_create(req: ProposalCreateRequest):
    try:
        parsed = parse_skill_md(req.proposed_skill_md)
    except RuleParseError as exc:
        raise HTTPException(status_code=400, detail={"errors": exc.errors})
    pid = store.add_proposal(
        source=req.source,
        label=req.label,
        summary=req.summary,
        rationale=req.rationale,
        downside=req.downside,
        evidence_json="[]",
        proposed_skill_md=req.proposed_skill_md,
        observation_id=req.observation_id,
    )
    return store.get_proposal(pid)


@app.post("/api/proposals/{proposal_id}/approve")
def api_proposal_approve(proposal_id: int, req: ProposalApproveRequest = ProposalApproveRequest()):
    prop = store.get_proposal(proposal_id)
    if prop is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if prop["status"] != "pending":
        raise HTTPException(status_code=409, detail="proposal already reviewed")
    md = (req.skill_md or prop["proposed_skill_md"]).strip()
    try:
        parsed = parse_skill_md(md)
    except RuleParseError as exc:
        raise HTTPException(status_code=400, detail={"errors": exc.errors})
    rid = store.add_rule(md, json.dumps(parsed, sort_keys=True))
    if prop.get("observation_id"):
        store.mark_observation_converted(prop["observation_id"], rid)
    store.approve_proposal(proposal_id, rid)
    result = {"ok": True, "rule": store.get_rule(rid), "proposal": store.get_proposal(proposal_id)}
    if req.apply_now:
        service = require_service()
        items, _bundles, _nxt = _run(queue_module.build_queue, service, config.DEFAULT_BATCH, None)
        _kept, _t, _s, _k, rule_stats = _apply_rules(service, items)
        stats = rule_stats.get(rid, {"trash": 0, "star": 0, "skip": 0})
        result["applied"] = {"matched": sum(stats.values()), **stats}
    return result


# --- Traces (raw evidence log) -------------------------------------------------

@app.get("/api/traces")
def api_traces(limit: int = 50, rule_id: int | None = None):
    limit = max(1, min(limit, 200))
    return {"items": store.list_traces(limit=limit, rule_id=rule_id)}


# --- Wiki observations ---------------------------------------------------------

@app.get("/api/wiki/observations")
def api_wiki_observations():
    return {"items": store.list_observations()}


@app.post("/api/wiki/observations/{obs_id}/dismiss")
def api_wiki_observation_dismiss(obs_id: int):
    if store.get_observation(obs_id) is None:
        raise HTTPException(status_code=404, detail="observation not found")
    store.dismiss_observation(obs_id)
    return {"ok": True}


@app.post("/api/wiki/observations/{obs_id}/convert")
def api_wiki_observation_convert(obs_id: int, req: RuleConvertRequest):
    if store.get_observation(obs_id) is None:
        raise HTTPException(status_code=404, detail="observation not found")
    if store.get_rule(req.rule_id) is None:
        raise HTTPException(status_code=404, detail="rule not found")
    store.mark_observation_converted(obs_id, req.rule_id)
    return {"ok": True}


# --- Proposals ----------------------------------------------------------------

@app.get("/api/proposals")
def api_proposals(status: str = "pending"):
    return {"items": store.list_proposals(status or "pending")}


@app.post("/api/proposals/{proposal_id}/reject")
def api_proposal_reject(proposal_id: int):
    prop = store.get_proposal(proposal_id)
    if prop is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if prop["status"] != "pending":
        raise HTTPException(status_code=409, detail="proposal already reviewed")
    store.reject_proposal(proposal_id, config.PROPOSAL_SUPPRESS_DAYS)
    return {"ok": True, "proposal": store.get_proposal(proposal_id)}


# --- WikiSkill evolve ---------------------------------------------------------

@app.post("/api/evolve")
def api_evolve():
    try:
        result = wiki_module.run_evolve(llm_fn=wiki_module._ollama_agent)
    except Exception as exc:
        logger.exception("evolve failed")
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, **result}


# --- Categories ------------------------------------------------------------

class CategoryAddRequest(BaseModel):
    name: str


class CategoryRemoveRequest(BaseModel):
    name: str


@app.get("/api/categories")
def api_categories():
    return {"items": store.get_categories()}


@app.post("/api/categories/add")
def api_category_add(req: CategoryAddRequest):
    items = store.add_category(req.name)
    return {"ok": True, "items": items}


@app.post("/api/categories/remove")
def api_category_remove(req: CategoryRemoveRequest):
    items = store.remove_category(req.name)
    return {"ok": True, "items": items}


# --- Grouped-sender summaries -------------------------------------------------

class GroupSummarizeRequest(BaseModel):
    message_ids: list[str]
    label: str | None = None


@app.post("/api/groups/{sender_key}/summarize")
def api_group_summarize(sender_key: str, req: GroupSummarizeRequest):
    service = require_service()
    result = _run(ai_summary.summarize_group, service, sender_key, req.label or sender_key, req.message_ids)
    result["group_key"] = sender_key
    return result


# --- Single-message actions ---------------------------------------------------

def _undo_payload(action: str, message_id: str) -> dict:
    return {"action": action, "message_ids": [message_id]}


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


@app.post("/api/messages/{message_id}/trash")
def api_trash(message_id: str):
    service = require_service()
    _run(gmail_service.trash, service, message_id)
    _record_trace(service, message_id, "trashed")
    return {"ok": True, "undo": _undo_payload("trash", message_id)}


@app.post("/api/messages/{message_id}/archive")
def api_archive(message_id: str):
    service = require_service()
    _run(gmail_service.archive, service, message_id)
    _record_trace(service, message_id, "archived")
    return {"ok": True, "undo": _undo_payload("archive", message_id)}


@app.post("/api/messages/{message_id}/star")
def api_star(message_id: str):
    service = require_service()
    _run(gmail_service.star, service, message_id)
    _record_trace(service, message_id, "starred")
    return {"ok": True, "undo": _undo_payload("star", message_id)}


# --- Bundle (bulk) actions ----------------------------------------------------

class BulkRequest(BaseModel):
    message_ids: list[str]


def _run_bulk(service, fn, req: BulkRequest, action: str):
    if not req.message_ids:
        raise HTTPException(status_code=400, detail="No message_ids provided")
    failed = _run(gmail_service.bulk_execute, service, req.message_ids, fn)
    for mid in req.message_ids:
        if mid not in failed:
            _record_trace(service, mid, action)
    return {
        "ok": True,
        "processed": len(req.message_ids) - len(failed),
        "failed": failed,
        "undo": {"action": action, "message_ids": req.message_ids},
    }


def _apply_rules(service, items: list[dict]) -> tuple[list[dict], int, int, int, dict]:
    """Apply enabled rules in precedence order (first match wins) to a queue
    batch. Trash/star go through Gmail in bulk; skip hides locally. Auto-actions
    leave a trace row so rules stay anchored to the mail they acted on.
    Returns (kept, n_trash, n_star, n_skip, rule_stats) where rule_stats maps
    rule_id -> {"trash": n, "star": n, "skip": n} for successfully applied items."""
    from backend.rules import frequency_map, match_item

    enabled = [r for r in store.list_rules() if r["enabled"]]
    if not enabled:
        return items, 0, 0, 0, {}
    enabled.sort(key=lambda r: r["precedence"])
    freq = frequency_map(store.traces_since(config.TRACE_WINDOW_SECONDS))
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

    # Failed trash/star ops don't get credited and the message stays in the
    # queue instead of silently evaporating.
    id_map = {it["id"]: it for it in items}
    for mid in failed_trash + failed_star:
        if mid in id_map:
            kept.append(id_map[mid])

    rule_stats: dict[int, dict] = {}
    for it, rule in fired:
        action = rule["parsed"]["action"]
        done = (
            (action == "trash" and it["id"] in trash_done)
            or (action == "star" and it["id"] in star_done)
            or action == "skip"
        )
        if not done:
            continue
        stats = rule_stats.setdefault(rule["id"], {"trash": 0, "star": 0, "skip": 0})
        stats[action] += 1
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

    return (kept, len(trash_done), len(star_done), len(skip_items), rule_stats)


@app.post("/api/bundles/{sender_key}/trash")
def api_bundle_trash(sender_key: str, req: BulkRequest):
    service = require_service()
    return _run_bulk(service, gmail_service.trash, req, "trash")


@app.post("/api/bundles/{sender_key}/archive")
def api_bundle_archive(sender_key: str, req: BulkRequest):
    service = require_service()
    return _run_bulk(service, gmail_service.archive, req, "archive")


# --- Undo ---------------------------------------------------------------------

class UndoRequest(BaseModel):
    action: str
    message_ids: list[str]


_INVERSE = {
    "trash": gmail_service.untrash,
    "archive": gmail_service.unarchive,
    "star": gmail_service.unstar,
}


@app.post("/api/undo")
def api_undo(req: UndoRequest):
    service = require_service()
    fn = _INVERSE.get(req.action)
    if fn is None:
        raise HTTPException(status_code=400, detail=f"Unknown undo action: {req.action}")
    if not req.message_ids:
        raise HTTPException(status_code=400, detail="No message_ids provided")
    failed = _run(gmail_service.bulk_execute, service, req.message_ids, fn)
    return {
        "ok": True,
        "restored": len(req.message_ids) - len(failed),
        "failed": failed,
        "action": req.action,
    }


# --- Static -------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Bust stale browser caches for dev assets: always revalidate /static."""
    response = await call_next(request)
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/email-viewer", include_in_schema=False)
def email_viewer():
    return FileResponse(config.STATIC_DIR / "email-viewer.html")