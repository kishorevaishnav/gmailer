from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import ai_summary, auth, config, gmail_service, store
from backend import queue as queue_module

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

    # Auto-delete: any message from a persisted blocked sender is trashed now and
    # never surfaced, so new mail from them disappears on every pull. Promo-only
    # vendors ("promo auto-delete") are trashed only when the mail is PROMO-labelled.
    blocked = {b["email"] for b in store.list_blocked()}
    promo_blocked = {b["email"] for b in store.list_promo_blocked()}
    kept: list[dict] = []
    doomed: list[dict] = []
    for item in items:
        email = (item.get("sender_email") or "").strip().lower()
        if not email:
            kept.append(item)
            continue
        if email in blocked:
            doomed.append(item)
        elif email in promo_blocked and item.get("promo"):
            doomed.append(item)
        else:
            kept.append(item)
    auto_deleted = 0
    if doomed:
        failed = gmail_service.bulk_execute(service, [d["id"] for d in doomed], gmail_service.trash)
        auto_deleted = len(doomed) - len(failed)
        for d in doomed:
            if d["id"] not in failed:
                store.remove_skipped(d["id"])  # keep "skipped" state coherent

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
        "auto_deleted": auto_deleted,
        "blocked_count": len(blocked),
    }


@app.get("/api/messages/{message_id}")
def api_message(message_id: str):
    cached = store.load_message(message_id)
    if cached and cached.get("summary"):
        cached["from_cache"] = True
        return cached
    if cached and cached.get("body_text"):
        # Body already local; only the AI summary is stale/absent.
        cached["summary"] = ai_summary.generate_summary(cached)
        store.save_message(cached)
        cached["from_cache"] = True
        return cached
    service = require_service()
    msg = _run(gmail_service.get_full, service, message_id)
    msg["summary"] = ai_summary.generate_summary(msg)
    store.save_message(msg)
    return msg


@app.post("/api/cache/clear")
def api_cache_clear():
    cleared = store.clear_cache()
    ai_summary.clear_in_memory_caches()
    return {"ok": True, "cleared": cleared}


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
    return {"ok": True}


@app.post("/api/skipped/remove")
def api_skipped_remove(req: SkipRemoveRequest):
    store.remove_skipped(req.id)
    return {"ok": True}


@app.post("/api/skipped/clear")
def api_skipped_clear():
    cleared = store.clear_skipped()
    return {"ok": True, "cleared": cleared}


# --- Blocked senders (auto-delete) -----------------------------------------------

class BlockedAddRequest(BaseModel):
    sender_email: str
    sender_name: str | None = None


class BlockedRemoveRequest(BaseModel):
    sender_email: str


@app.get("/api/blocked")
def api_blocked():
    return {"items": store.list_blocked()}


@app.post("/api/blocked/add")
def api_blocked_add(req: BlockedAddRequest):
    service = require_service()
    email = (req.sender_email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="sender_email required")
    store.add_blocked(email, req.sender_name)
    # Purge what's already unread from this sender right now (not just future mail).
    ids = _run(gmail_service.list_sender_unread_ids, service, email)
    failed = gmail_service.bulk_execute(service, ids, gmail_service.trash) if ids else []
    for mid in ids:
        if mid not in failed:
            store.remove_skipped(mid)  # a blocked sender can't stay "skipped"
    return {"ok": True, "purged": len(ids) - len(failed), "failed": len(failed)}


@app.post("/api/blocked/remove")
def api_blocked_remove(req: BlockedRemoveRequest):
    email = (req.sender_email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="sender_email required")
    store.remove_blocked(email)
    return {"ok": True}


@app.post("/api/blocked/clear")
def api_blocked_clear():
    cleared = store.clear_blocked()
    return {"ok": True, "cleared": cleared}


# --- Promo auto-delete (blocked-for-promos-only) ---------------------------------

@app.get("/api/promo-blocked")
def api_promo_blocked():
    return {"items": store.list_promo_blocked()}


@app.post("/api/promo-blocked/add")
def api_promo_blocked_add(req: BlockedAddRequest):
    service = require_service()
    email = (req.sender_email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="sender_email required")
    store.add_promo_blocked(email, req.sender_name)
    # Purge already-unread PROMO mail from this vendor right now (not just future mail).
    # Non-promo mail from them is left alone so real messages still surface.
    ids = _run(gmail_service.list_sender_unread_ids, service, email)
    promo_ids: list[str] = []
    if ids:
        metas = _run(gmail_service.get_metadata_batch, service, ids)
        promo_ids = [m["id"] for m in metas if m.get("promo")]
    failed = gmail_service.bulk_execute(service, promo_ids, gmail_service.trash) if promo_ids else []
    for mid in promo_ids:
        if mid not in failed:
            store.remove_skipped(mid)  # a promo-deleted sender can't stay "skipped"
    return {"ok": True, "purged": len(promo_ids) - len(failed), "skipped": len(ids) - len(promo_ids), "failed": len(failed)}


@app.post("/api/promo-blocked/remove")
def api_promo_blocked_remove(req: BlockedRemoveRequest):
    email = (req.sender_email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="sender_email required")
    store.remove_promo_blocked(email)
    return {"ok": True}


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


@app.post("/api/messages/{message_id}/trash")
def api_trash(message_id: str):
    service = require_service()
    _run(gmail_service.trash, service, message_id)
    return {"ok": True, "undo": _undo_payload("trash", message_id)}


@app.post("/api/messages/{message_id}/archive")
def api_archive(message_id: str):
    service = require_service()
    _run(gmail_service.archive, service, message_id)
    return {"ok": True, "undo": _undo_payload("archive", message_id)}


@app.post("/api/messages/{message_id}/star")
def api_star(message_id: str):
    service = require_service()
    _run(gmail_service.star, service, message_id)
    return {"ok": True, "undo": _undo_payload("star", message_id)}


# --- Bundle (bulk) actions ----------------------------------------------------

class BulkRequest(BaseModel):
    message_ids: list[str]


def _run_bulk(service, fn, req: BulkRequest, action: str):
    if not req.message_ids:
        raise HTTPException(status_code=400, detail="No message_ids provided")
    failed = _run(gmail_service.bulk_execute, service, req.message_ids, fn)
    return {
        "ok": True,
        "processed": len(req.message_ids) - len(failed),
        "failed": failed,
        "undo": {"action": action, "message_ids": req.message_ids},
    }


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