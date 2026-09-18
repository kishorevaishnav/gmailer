from __future__ import annotations

import base64
import io
import json
import logging
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend import ai_summary, auth, config, gmail_service, store
from backend import gtasks
from backend import queue as queue_module
from backend import summarizer
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


@app.on_event("startup")
def _start_background_summarizer():
    summarizer.start()


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


# --- Settings (prompts, etc.) --------------------------------------------------

class PromptsPatchRequest(BaseModel):
    ai_system_prompt: str | None = None
    ai_group_system_prompt: str | None = None
    ai_category_system_prompt: str | None = None


PROMPT_KEYS = [
    ("ai_system_prompt", "Email summary system prompt", ai_summary.get_system_prompt),
    ("ai_group_system_prompt", "Group sender summary system prompt", ai_summary.get_group_system_prompt),
    ("ai_category_system_prompt", "Category-level summary system prompt", ai_summary.get_category_system_prompt),
]


@app.get("/api/settings/prompts")
def api_settings_prompts():
    items = []
    for key, label, getter in PROMPT_KEYS:
        items.append({"key": key, "label": label, "value": getter()})
    return {"prompts": items}


@app.put("/api/settings/prompts")
def api_settings_prompts_update(req: PromptsPatchRequest):
    data = req.model_dump(exclude_unset=True)
    for key, _label, _getter in PROMPT_KEYS:
        if key in data:
            store.set_setting(key, data[key])
    return {"ok": True}


# --- Queue -------------------------------------------------------------------

@app.get("/api/queue")
def api_queue(max_results: int = config.DEFAULT_BATCH, page_token: str | None = None):
    service = require_service()
    max_results = max(1, min(max_results, config.MAX_BATCH))
    items, bundles, next_page_token = _run(queue_module.build_queue, service, max_results, page_token)

    kept, auto_trashed, auto_starred, auto_skipped, _rule_stats = _apply_rules(service, items)
    items = kept
    skips = set(store.get_summary_skips())
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
            if cached.get("category_locked"):
                item["category_locked"] = True
        sender = (item.get("sender_email") or "").strip().lower()
        item["needs_summary"] = not (item.get("summary") and item["summary"].get("one_liner")) and sender not in skips
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
    if cached and cached.get("summary") and cached.get("body_text"):
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
    if cached and cached.get("summary"):
        # Summary already local; only the body was missing — reuse it.
        msg["summary"] = cached["summary"]
        if cached.get("category") and not msg.get("category"):
            msg["category"] = cached["category"]
    else:
        msg["summary"] = ai_summary.generate_summary(msg)
    store.save_message(msg)
    safe_review(service)
    return msg


@app.post("/api/messages/{message_id}/summarize")
def api_regenerate_summary(message_id: str):
    if message_id in ai_summary._CACHE:
        del ai_summary._CACHE[message_id]

    cached = store.load_message(message_id)
    if cached and cached.get("body_text"):
        msg = cached
    else:
        service = require_service()
        msg = _run(gmail_service.get_full, service, message_id)
        if cached:
            msg["id"] = cached["id"]

    msg["summary"] = ai_summary.generate_summary(msg, force=True)
    store.save_message(msg)
    return {"id": msg["id"], "summary": msg["summary"]}


@app.get("/api/messages/{message_id}/attachments/{attachment_id}")
def api_attachment(message_id: str, attachment_id: str):
    service = require_service()
    cached = store.load_message(message_id)
    atts = (cached or {}).get("attachments") or []
    att = next((a for a in atts if a.get("attachmentId") == attachment_id), None)
    if att is None:
        full = _run(gmail_service.get_full, service, message_id)
        store.save_attachments(message_id, full.get("attachments") or [])
        att = next((a for a in (full.get("attachments") or []) if a.get("attachmentId") == attachment_id), None)
    if att is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    raw = _run(gmail_service.get_attachment, service, message_id, attachment_id)
    try:
        blob = base64.urlsafe_b64decode((raw.get("data") or "").encode("ascii"))
    except Exception:
        raise HTTPException(status_code=502, detail="attachment decode failed")
    filename = (att.get("filename") or "attachment").replace('"', "").replace("\r", "").replace("\n", "")
    return StreamingResponse(
        io.BytesIO(blob),
        media_type=att.get("mimeType") or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


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


# --- Dev live-reload (browser auto-refresh on code change) --------------------

@app.get("/api/dev-hash")
def api_dev_hash():
    import hashlib

    h = hashlib.sha1()
    files = sorted(config.STATIC_DIR.glob("*")) + [config.BASE_DIR / "main.py"] \
        + sorted((config.BASE_DIR / "backend").glob("*.py"))
    for f in files:
        try:
            st = f.stat()
            h.update(f"{f.name}:{st.st_mtime_ns}:{st.st_size};".encode())
        except OSError:
            continue
    return {"hash": h.hexdigest()}


# --- Search (local cache: subject / sender / snippet / body) ------------------

@app.get("/api/search")
def api_search(q: str = "", limit: int = 100):
    q = (q or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="q required")
    items = store.search_messages(q, limit=limit)
    return {"items": items, "count": len(items), "query": q}


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


class SummarySkipAddRequest(BaseModel):
    sender_email: str


@app.get("/api/summary-skipped")
def api_summary_skipped():
    return {"items": store.get_summary_skips()}


@app.post("/api/summary-skipped")
def api_summary_skipped_add(req: SummarySkipAddRequest):
    store.add_summary_skip(req.sender_email)
    ai_summary.clear_in_memory_caches()
    return {"ok": True}


@app.delete("/api/summary-skipped/{sender_email}")
def api_summary_skipped_remove(sender_email: str):
    store.remove_summary_skip(sender_email)
    return {"ok": True}


# --- TODO list -----------------------------------------------------------------

class TodoAddRequest(BaseModel):
    item: dict
    due_date: str | None = None
    note: str | None = None


class TodoRemoveRequest(BaseModel):
    id: str


class TodoPatchRequest(BaseModel):
    due_date: str | None = None
    note: str | None = None


class TodoMoveRequest(BaseModel):
    direction: str


class TodoReorderRequest(BaseModel):
    ids: list[str]


def _check_due_date(due_date: str | None) -> str | None:
    if due_date is None or due_date == "":
        return None
    import datetime as _dt
    try:
        _dt.date.fromisoformat(due_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="due_date must be YYYY-MM-DD")
    return due_date


@app.get("/api/todos")
def api_todos():
    return {"items": store.list_todos()}


@app.post("/api/todos/add")
def api_todos_add(req: TodoAddRequest):
    if not (req.item or {}).get("id"):
        raise HTTPException(status_code=400, detail="item.id required")
    require_service()
    due = _check_due_date(req.due_date)
    task = _run(gtasks.create_task, req.item, due)
    row = store.add_todo(req.item, due, req.note or "",
                         gtask_id=task.get("id"), gtask_list=gtasks.DEFAULT_LIST)
    itm = req.item or {}
    store.add_trace(
        message_id=itm.get("id"), action="todo_added", rule_id=None,
        sender_email=itm.get("sender_email"), sender_name=itm.get("sender_name"),
        subject=itm.get("subject"), promo=bool(itm.get("promo")), category=itm.get("category"),
    )
    return row


def _drop_google_task(row: dict | None) -> None:
    if not row or not row.get("gtask_id"):
        return
    try:
        gtasks.delete_task(row["gtask_id"], row.get("gtask_list") or gtasks.DEFAULT_LIST)
    except Exception:
        logger.warning("Google Task deletion failed for %s", row.get("id"))


@app.post("/api/todos/remove")
def api_todos_remove(req: TodoRemoveRequest):
    require_service()
    _drop_google_task(store.get_todo(req.id))
    store.remove_todo(req.id)
    return {"ok": True}


@app.post("/api/todos/done")
def api_todos_done(req: TodoRemoveRequest):
    require_service()
    row = store.get_todo(req.id)
    _drop_google_task(row)
    if row:
        store.add_trace(
            message_id=row.get("id"), action="todo_done", rule_id=None,
            sender_email=row.get("sender_email"), sender_name=row.get("sender_name"),
            subject=row.get("subject"), promo=bool(row.get("promo")), category=row.get("category"),
        )
    store.remove_todo(req.id)
    return {"ok": True}


@app.patch("/api/todos/{todo_id}")
def api_todo_update(todo_id: str, req: TodoPatchRequest):
    if store.get_todo(todo_id) is None:
        raise HTTPException(status_code=404, detail="todo not found")
    data = req.model_dump(exclude_unset=True)
    due = _check_due_date(data["due_date"]) if "due_date" in data else None
    row = store.update_todo(todo_id, due_date=due, due_date_set="due_date" in data,
                             note=data.get("note"))
    if row and row.get("gtask_id"):
        try:
            tasklist = row.get("gtask_list") or gtasks.DEFAULT_LIST
            new_task = gtasks.replace_task(row["gtask_id"], row, row.get("due_date"), tasklist)
            store.set_gtask(todo_id, new_task.get("id"), tasklist)
            row = store.get_todo(todo_id)
        except Exception:
            logger.warning("Google Task due-date sync failed for %s", todo_id)
    return row


@app.post("/api/todos/{todo_id}/move")
def api_todo_move(todo_id: str, req: TodoMoveRequest):
    if store.get_todo(todo_id) is None:
        raise HTTPException(status_code=404, detail="todo not found")
    store.move_todo(todo_id, 1 if req.direction == "down" else -1)
    return {"ok": True}


@app.post("/api/todos/reorder")
def api_todos_reorder(req: TodoReorderRequest):
    store.reorder_todos([str(i) for i in req.ids])
    return {"ok": True}


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


def _consolidate_trash_sender(parsed: dict) -> dict | None:
    senders = parsed.get("sender") or []
    if isinstance(senders, str):
        senders = [senders]
    if parsed.get("action") != "trash" or len(senders) != 1 or parsed.get("subject") \
            or parsed.get("category") or parsed.get("emails_per_day"):
        return None
    target = store.find_append_target("trash", parsed.get("scope") or "all_mail")
    if target is None:
        return None
    updated = store.append_rule_sender(target["id"], senders[0])
    if updated is None:
        return None
    updated["appended"] = True
    return updated


@app.post("/api/rules")
def api_rules_create(req: RuleCreateRequest):
    md = (req.markdown or "").strip()
    if not md:
        raise HTTPException(status_code=400, detail="markdown required")
    try:
        parsed = parse_skill_md(md)
    except RuleParseError as exc:
        raise HTTPException(status_code=400, detail={"errors": exc.errors})
    merged = _consolidate_trash_sender(parsed)
    if merged is not None:
        return merged
    rid = store.add_rule(md, json.dumps(parsed, sort_keys=True))
    if not req.enabled:
        store.update_rule(rid, enabled=False)
    result = store.get_rule(rid)
    result["appended"] = False
    return result


class RuleSenderRemoveRequest(BaseModel):
    sender: str


@app.post("/api/rules/{rule_id}/senders/remove")
def api_rule_sender_remove(rule_id: int, req: RuleSenderRemoveRequest):
    if store.get_rule(rule_id) is None:
        raise HTTPException(status_code=404, detail="rule not found")
    try:
        updated = store.remove_rule_sender(rule_id, req.sender)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if updated is None:
        raise HTTPException(status_code=404, detail="sender not on rule")
    return updated


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
    merged = _consolidate_trash_sender(parsed)
    if merged is not None:
        rid = merged["id"]
    else:
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


class SenderMapAddRequest(BaseModel):
    pattern: str
    category: str
    promo_sensitive: bool = True
    subject_contains: str = ""


class SenderMapRemoveRequest(BaseModel):
    pattern: str


@app.get("/api/sender-map")
def api_sender_map():
    return {"items": store.get_sender_map()}


@app.post("/api/sender-map")
def api_sender_map_add(req: SenderMapAddRequest):
    try:
        items = store.add_sender_map(req.pattern, req.category, req.promo_sensitive,
                                     req.subject_contains)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"items": items}


@app.post("/api/sender-map/remove")
def api_sender_map_remove(req: SenderMapRemoveRequest):
    return {"items": store.remove_sender_map(req.pattern)}


class CategoryRemapRequest(BaseModel):
    limit: int = 500


@app.post("/api/categories/remap")
def api_categories_remap(req: CategoryRemapRequest):
    limit = max(1, min(int(req.limit or 500), 2000))
    cats = store.get_categories()
    scanned = changed = locked = 0
    for m in store.list_messages(limit=limit):
        scanned += 1
        if m.get("category_locked"):
            locked += 1
            continue
        new_cat = ai_summary.recategorize_message(m, cats)
        if (new_cat or "") != (m.get("category") or ""):
            summary = m.get("summary")
            if isinstance(summary, dict):
                summary["category"] = new_cat
            m["category"] = new_cat
            store.save_message(m, summary if isinstance(summary, dict) else None)
            changed += 1
    return {"scanned": scanned, "changed": changed, "locked": locked}


class CategorySetRequest(BaseModel):
    id: str
    category: str
    subject_contains: str = ""


@app.post("/api/categories/set")
def api_categories_set(req: CategorySetRequest):
    mid = (req.id or "").strip()
    wanted = (req.category or "").strip()
    if not mid or not wanted:
        raise HTTPException(status_code=400, detail="id and category required")
    canonical = ai_summary.canonical_category(wanted, store.get_categories())
    if not canonical:
        raise HTTPException(status_code=400, detail=f"unknown category: {wanted}")
    row = store.set_message_category(mid, canonical, locked=True)
    if row is None:
        raise HTTPException(status_code=404, detail="message not cached")
    mapping = None
    sender_email = (row.get("sender_email") or "").strip().lower()
    if sender_email:
        try:
            cond = (req.subject_contains or "").strip().lower()
            store.add_sender_map(sender_email, canonical, False, cond)
            mapping = {"pattern": sender_email, "category": canonical, "subject_contains": cond}
        except Exception:
            logger.warning("sender-map learn failed for %s", sender_email)
    row["learned_mapping"] = mapping
    return row


class CategoryRemapOneRequest(BaseModel):
    id: str


@app.post("/api/categories/remap-one")
def api_categories_remap_one(req: CategoryRemapOneRequest):
    mid = (req.id or "").strip()
    if not mid:
        raise HTTPException(status_code=400, detail="id required")
    msg = store.load_message(mid)
    if msg is None:
        service = require_service()
        msg = _run(gmail_service.get_full, service, mid)
        msg["summary"] = ai_summary.generate_summary(msg)
        store.save_message(msg)
    old = msg.get("category") or ""
    new_cat = ai_summary.recategorize_message(msg, store.get_categories(), force=True)
    if (new_cat or "") != old or msg.get("category_locked"):
        store.set_message_category(mid, new_cat, locked=False)
    return {"id": mid, "old": old, "category": new_cat, "changed": (new_cat or "") != old}


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


@app.post("/api/domains/{domain}/trash")
def api_domain_trash(domain: str, req: BulkRequest):
    service = require_service()
    result = _run_bulk(service, gmail_service.trash, req, "trash")
    result["removed_from_cache"] = store.remove_messages(
        [mid for mid in req.message_ids if mid not in result["failed"]]
    )
    return result


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
async def collapse_duplicate_slashes(request, call_next):
    path = request.url.path
    if "//" in path:
        clean = re.sub(r"/{2,}", "/", path)
        return RedirectResponse(str(request.url.replace(path=clean)), status_code=307)
    return await call_next(request)


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


@app.get("/rules", include_in_schema=False)
def rules_page():
    return FileResponse(config.STATIC_DIR / "rules.html")


@app.get("/todos", include_in_schema=False)
def todos_page():
    return FileResponse(config.STATIC_DIR / "todos.html")


@app.get("/settings", include_in_schema=False)
def settings_page():
    return FileResponse(config.STATIC_DIR / "settings.html")