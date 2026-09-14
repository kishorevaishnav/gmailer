"""Gmail REST client — thread-safe `requests` transport.

Why not googleapiclient/httplib2: googleapi's default httplib2 transport is
NOT thread-safe, and the shared connection's OpenSSL state segfaults
(SIGSEGV in libssl BIO) when the metadata/action batch pools hit it
concurrently. `requests`/urllib3 sessions are safe to share across threads.

Public function names mirror the previous googleapiclient-based API so
callers in main.py and queue.py are unchanged.
"""

from __future__ import annotations

import base64
import email.utils
import html
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from time import sleep

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import auth, config

_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

_PROFILE_EMAIL: str | None = None
_PROFILE_LOCK = threading.Lock()

_READ_TIMEOUT = 30


class GmailClient:
    """Minimal Gmail v1 REST client with automatic auth + retries."""

    def __init__(self, creds):
        self._creds = creds
        self._session = requests.Session()
        retry = Retry(
            total=4,
            connect=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        self._session.mount(
            "https://",
            HTTPAdapter(
                max_retries=retry,
                pool_maxsize=max(config.METADATA_WORKERS, 16) + 8,
            ),
        )

    def _token(self) -> str:
        if not self._creds.valid:
            self._creds.refresh(GoogleAuthRequest())
        return self._creds.token

    def _request(self, method: str, path: str, **kwargs):
        headers = {"Authorization": f"Bearer {self._token()}"}
        headers.update(kwargs.pop("headers", {}))
        try:
            resp = self._session.request(
                method, _API_BASE + path, headers=headers, timeout=_READ_TIMEOUT, **kwargs
            )
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"Gmail API connection failed: {exc}") from exc
        if resp.status_code in (429, 503) or (
            resp.status_code == 403 and "quota" in (resp.text or "").lower()
        ):
            # Gmail per-user quota: sleep briefly and retry once at the session
            # layer, on top of urllib3's backoff inside the adapter. Quota-403s
            # reset on a ~1-minute window, so give them a longer pause.
            sleep(30.0 if resp.status_code == 403 else 2.0)
            resp = self._session.request(
                method, _API_BASE + path, headers=headers, timeout=_READ_TIMEOUT, **kwargs
            )
            if resp.status_code < 400:
                return resp.json()
        if resp.status_code >= 400:
            # A stale access token is the only 401 we can self-heal.
            if resp.status_code == 401:
                self._creds.refresh(GoogleAuthRequest())
                resp = self._session.request(
                    method, _API_BASE + path, headers=headers, timeout=_READ_TIMEOUT, **kwargs
                )
                if resp.status_code < 400:
                    return resp.json()
            raise RuntimeError(
                f"Gmail API {resp.status_code}: {(resp.text or '')[:300]}"
            )
        return resp.json()

    # --- REST operations -------------------------------------------------

    def list_messages(self, q: str, label_ids: list[str], max_results: int) -> list[str]:
        params = [("maxResults", str(max_results))]
        if q:
            params.append(("q", q))
        for label in label_ids:
            params.append(("labelIds", label))
        data = self._request("GET", "/messages", params=params)
        return [m["id"] for m in data.get("messages", [])]

    def list_messages_page(self, q: str, max_results: int, page_token: str | None = None) -> tuple[list[str], str | None]:
        params = [("maxResults", str(max_results)), ("labelIds", "INBOX")]
        if q:
            params.append(("q", q))
        if page_token:
            params.append(("pageToken", page_token))
        data = self._request("GET", "/messages", params=params)
        return [m["id"] for m in data.get("messages", [])], data.get("nextPageToken")

    def get_metadata(self, message_id: str) -> dict:
        params = [("format", "metadata")]
        for h in ("From", "Subject", "Date"):
            params.append(("metadataHeaders", h))
        return self._request("GET", f"/messages/{message_id}", params=params)

    def get_full(self, message_id: str) -> dict:
        return self._request("GET", f"/messages/{message_id}", params=[("format", "full")])

    def get_profile(self) -> dict:
        return self._request("GET", "/profile")

    def trash(self, message_id: str) -> None:
        self._request("POST", f"/messages/{message_id}/trash")

    def untrash(self, message_id: str) -> None:
        self._request("POST", f"/messages/{message_id}/untrash")

    def modify(self, message_id: str, add: list[str] | None = None, remove: list[str] | None = None) -> None:
        body = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        self._request("POST", f"/messages/{message_id}/modify", json=body)

    def list_labels(self) -> list[dict]:
        return self._request("GET", "/labels").get("labels", [])

    def create_label(self, name: str) -> dict:
        return self._request("POST", "/labels", json={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
            "color": {"backgroundColor": "#fad165", "textColor": "#000000"},
        })


# --- Service / profile -------------------------------------------------------


def build_service() -> GmailClient:
    return GmailClient(auth.load_credentials())


def get_profile_email(client: GmailClient) -> str:
    global _PROFILE_EMAIL
    if _PROFILE_EMAIL is None:
        with _PROFILE_LOCK:
            if _PROFILE_EMAIL is None:
                _PROFILE_EMAIL = client.get_profile().get("emailAddress", "unknown")
    return _PROFILE_EMAIL


def list_unread_ids(client: GmailClient, max_results: int) -> list[str]:
    ids, _ = list_unread_page(client, max_results)
    return ids


def list_unread_page(client: GmailClient, max_results: int, page_token: str | None = None) -> tuple[list[str], str | None]:
    """Like list_unread_ids but returns (ids, next_page_token) for pagination."""
    return client.list_messages_page(q="is:unread", max_results=max_results, page_token=page_token)


def list_sender_unread_ids(client: GmailClient, sender_email: str, max_results: int = config.MAX_BATCH) -> list[str]:
    """All currently-unread messages from one sender (used to purge a blocked sender)."""
    ids, _ = client.list_messages_page(q=f"from:{sender_email} is:unread", max_results=max_results)
    return ids


# --- Header / subject helpers ------------------------------------------------

_CLEAN_SUBJECT_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|sv|aw|bls?|enc?|ref)\s*[:：]\s*)+", re.IGNORECASE
)


def clean_subject(subject: str) -> str:
    if not subject:
        return "(no subject)"
    cleaned = subject
    for _ in range(3):
        new = _CLEAN_SUBJECT_RE.sub("", cleaned).strip()
        if not new:
            break
        cleaned = new
    return cleaned or subject


def parse_sender(from_header: str) -> tuple[str, str]:
    name, addr = email.utils.parseaddr(from_header or "")
    addr = (addr or "").strip()
    if not addr and from_header:
        addr = from_header.strip()
    if not name:
        name = addr.split("@")[0] if addr else "Unknown"
    return name.strip(), addr


# --- Message parsing ---------------------------------------------------------

def _effective_header(headers: list[dict], wanted: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == wanted.lower():
            return h.get("value", "") or ""
    return ""


def _is_promo_label(label_ids: list[str]) -> bool:
    return bool("CATEGORY_PROMOTIONS" in (label_ids or []))


def _parse_metadata(raw: dict, message_id: str, thread_id: str, snippet: str, label_ids: list[str]) -> dict:
    headers = raw.get("payload", {}).get("headers", [])
    sender_name, sender_email = parse_sender(_effective_header(headers, "From"))
    return {
        "id": message_id,
        "thread_id": thread_id,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": clean_subject(_effective_header(headers, "Subject")),
        "date_header": _effective_header(headers, "Date"),
        "internal_date_ms": int(raw.get("internalDate", 0) or 0),
        "snippet": snippet or "",
        "label_ids": label_ids,
        "promo": _is_promo_label(label_ids),
    }


def get_metadata(client: GmailClient, message_id: str) -> dict:
    """Cheap header-only fetch — used to build the queue at speed."""
    raw = client.get_metadata(message_id)
    return _parse_metadata(
        raw,
        message_id,
        raw.get("threadId", ""),
        raw.get("snippet", ""),
        raw.get("labelIds", []),
    )


def get_metadata_batch(client: GmailClient, message_ids: list[str]) -> list[dict]:
    """Fetch metadata for a batch concurrently. Skips failures silently."""
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=config.METADATA_WORKERS) as pool:
        futures = {
            pool.submit(get_metadata, client, mid): mid for mid in message_ids
        }
        for fut in futures:
            try:
                results.append(fut.result())
            except Exception:
                pass  # skip dead/stale messages; keep the queue moving
    return results


def _decode_body(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data.encode("ascii"))
    except Exception:
        return ""
    for charset in ("utf-8", "utf-16", "latin-1"):
        try:
            return raw.decode(charset)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_SCRIPT_RE = re.compile(
    r"(?is)<(?:style|script|head|title)[^>]*>.*?</(?:style|script|head|title)>"
)
_COMMENT_RE = re.compile(r"(?s)<!--.*?-->")
_MULTI_WS_RE = re.compile(r"[ \t]+")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _flatten_text(parts: list[dict], texts: list[str]) -> None:
    for p in parts:
        mime = p.get("mimeType", "")
        if mime == "text/plain":
            text = _decode_body(p)
            if text:
                texts.append(text)
        elif mime == "text/html" and p.get("body", {}).get("data"):
            texts.append(("html", _decode_body(p)))
        elif p.get("parts"):
            _flatten_text(p.get("parts", []), texts)


def _extract_text(payload: dict) -> str:
    parts = payload.get("parts") or []
    if not parts:
        return _decode_body(payload)

    plain: list[str] = []
    html_source: str | None = None
    stacked: list = []
    _flatten_text(parts, stacked)

    for chunk in stacked:
        if isinstance(chunk, tuple):
            if html_source is None:
                html_source = chunk[1]
        elif chunk:
            plain.append(chunk)

    if not plain and html_source:
        stripped = _STYLE_SCRIPT_RE.sub(" ", html_source)
        stripped = _COMMENT_RE.sub(" ", stripped)
        plain.append(html.unescape(_TAG_RE.sub(" ", stripped)).strip())

    combined = "\n\n".join(plain)
    combined = combined.replace("\r\n", "\n").replace("\r", "\n")
    combined = _MULTI_WS_RE.sub(" ", combined)
    combined = re.sub(r"[ \t]+\n", "\n", combined)  # trailing ws per line
    combined = re.sub(r"\n[ \t]+", "\n", combined)  # leading indent per line
    combined = _BLANK_RUN_RE.sub("\n\n", combined)  # max one blank line
    return combined.strip(" \t\n")


def get_full(client: GmailClient, message_id: str) -> dict:
    """Full message for rendering (body text + payload for the AI layer)."""
    raw = client.get_full(message_id)
    payload = raw.get("payload", {})
    headers = payload.get("headers", [])
    sender_name, sender_email = parse_sender(_effective_header(headers, "From"))

    body_text = _extract_text(payload)
    truncated = len(body_text) > config.BODY_MAX_CHARS
    body_text = body_text[: config.BODY_MAX_CHARS]

    return {
        "id": message_id,
        "thread_id": raw.get("threadId", ""),
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": clean_subject(_effective_header(headers, "Subject")),
        "date_header": _effective_header(headers, "Date"),
        "internal_date_ms": int(raw.get("internalDate", 0) or 0),
        "snippet": raw.get("snippet", "") or "",
        "label_ids": raw.get("labelIds", []),
        "promo": _is_promo_label(raw.get("labelIds", [])),
        "body_text": body_text,
        "body_truncated": truncated,
    }


# --- Actions -----------------------------------------------------------------

def _call(fn):
    """Execute a single Gmail API call and surface the error message."""
    try:
        fn()
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc


def trash(client: GmailClient, message_id: str) -> None:
    _call(lambda: client.trash(message_id))


def untrash(client: GmailClient, message_id: str) -> None:
    _call(lambda: client.untrash(message_id))


def archive(client: GmailClient, message_id: str) -> None:
    _call(lambda: client.modify(message_id, remove=["INBOX"]))


def unarchive(client: GmailClient, message_id: str) -> None:
    _call(lambda: client.modify(message_id, add=["INBOX"]))


def star(client: GmailClient, message_id: str) -> None:
    _call(lambda: client.modify(message_id, add=["STARRED"]))


def unstar(client: GmailClient, message_id: str) -> None:
    _call(lambda: client.modify(message_id, remove=["STARRED"]))


TODO_LABEL_NAME = "TODO"


def ensure_todo_label(client: GmailClient) -> str:
    found = next((l for l in client.list_labels() if (l.get("name") or "").upper() == TODO_LABEL_NAME), None)
    if found:
        return found["id"]
    return client.create_label(TODO_LABEL_NAME)["id"]


def apply_todo_label(client: GmailClient, message_id: str) -> None:
    _call(lambda: client.modify(message_id, add=[ensure_todo_label(client)]))


def remove_todo_label(client: GmailClient, message_id: str) -> None:
    _call(lambda: client.modify(message_id, remove=[ensure_todo_label(client)]))


def bulk_execute(client: GmailClient, message_ids: list[str], fn) -> list[str]:
    """Run an action across ids concurrently. Returns failed ids."""
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=config.METADATA_WORKERS) as pool:
        futures = {pool.submit(fn, client, mid): mid for mid in message_ids}
        for fut, mid in futures.items():
            try:
                fut.result()
            except Exception:
                failed.append(mid)
    return failed