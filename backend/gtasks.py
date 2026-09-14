"""Google Tasks client — one Task per TODO row.

Each parked email becomes a task (title = subject, notes = sender + Gmail
link, due = chosen date at noon UTC so the calendar day survives US
timezones). Task ids are stored on the todo row so date edits replace the
task and done/remove deletes it.
"""

from __future__ import annotations

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest

from . import auth

_API_BASE = "https://tasks.googleapis.com/tasks/v1"
DEFAULT_LIST = "@default"
_TIMEOUT = 30


def gmail_link(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#inbox/{message_id}"


def due_rfc3339(due_date: str) -> str:
    return f"{due_date}T12:00:00.000Z"


def build_task_payload(item: dict, due_date: str | None = None) -> dict:
    item = item or {}
    title = (item.get("subject") or "(no subject)").strip() or "(no subject)"
    name, email = item.get("sender_name") or "", item.get("sender_email") or ""
    sender = f"{name} <{email}>" if name and email else (name or email or "unknown sender")
    payload = {
        "title": title[:1024],
        "notes": f"From: {sender}\nOpen in Gmail: {gmail_link(item.get('id', ''))}",
    }
    if due_date:
        payload["due"] = due_rfc3339(due_date)
    return payload


def _request(method: str, path: str, body: dict | None = None) -> dict:
    creds = auth.load_credentials()
    if not creds.valid:
        creds.refresh(GoogleAuthRequest())
    resp = requests.request(
        method, _API_BASE + path,
        headers={"Authorization": f"Bearer {creds.token}"},
        json=body, timeout=_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Tasks API {resp.status_code}: {(resp.text or '')[:300]}")
    if resp.status_code == 204 or not (resp.text or "").strip():
        return {}
    return resp.json()


def create_task(item: dict, due_date: str | None = None, tasklist: str = DEFAULT_LIST) -> dict:
    return _request("POST", f"/lists/{tasklist}/tasks", build_task_payload(item, due_date))


def get_task(task_id: str, tasklist: str = DEFAULT_LIST) -> dict:
    return _request("GET", f"/lists/{tasklist}/tasks/{task_id}")


def delete_task(task_id: str, tasklist: str = DEFAULT_LIST) -> None:
    try:
        _request("DELETE", f"/lists/{tasklist}/tasks/{task_id}")
    except RuntimeError as exc:
        if "Tasks API 404" in str(exc):
            return
        raise


def replace_task(task_id: str, item: dict, due_date: str | None = None,
                 tasklist: str = DEFAULT_LIST) -> dict:
    delete_task(task_id, tasklist)
    return create_task(item, due_date, tasklist)
