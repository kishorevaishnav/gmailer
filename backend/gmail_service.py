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
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from time import sleep
from urllib.parse import quote
from uuid import uuid4

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import auth, config

_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

_BATCH_URL = "https://gmail.googleapis.com/batch/gmail/v1/"

_BATCH_CHUNK = 100

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

    def batch_get_metadata(self, message_ids: list[str]) -> dict[str, dict]:
        """Fetch metadata for many ids via the Gmail multipart batch endpoint.

        Up to 100 sub-requests ride one HTTP POST, so 200 queued ids cost ~2
        round-trips instead of 200. Returns {message_id: parsed_metadata}.
        Sub-requests throttled by Gmail's per-user quota get one retry pass;
        ids that keep failing are omitted (dead/stale messages keep the queue
        moving).
        """
        _RETRY_STATUSES = (408, 429, 500, 502, 503, 504)

        def build_body(chunk_ids: list[str], boundary: str) -> str:
            params = "?format=metadata"
            for h in ("From", "Subject", "Date"):
                params += f"&metadataHeaders={quote(h)}"
            parts = []
            for mid in chunk_ids:
                parts.append(
                    f"--{boundary}\r\n"
                    "Content-Type: application/http\r\n"
                    "Content-Transfer-Encoding: binary\r\n"
                    "\r\n"
                    f"GET /gmail/v1/users/me/messages/{mid}{params}\r\n"
                    "\r\n"
                )
            return "".join(parts) + f"--{boundary}--\r\n"

        def post_batch(token: str, boundary: str, body: str) -> requests.Response:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/mixed; boundary={boundary}",
            }
            return self._session.post(
                _BATCH_URL, data=body, headers=headers, timeout=_READ_TIMEOUT
            )

        results: dict[str, dict] = {}
        pending = [mid for mid in message_ids if mid]
        for attempt in range(2):
            if not pending:
                break
            retry: list[str] = []
            for chunk in _chunks(pending, _BATCH_CHUNK):
                boundary = "gmailer_" + uuid4().hex
                body = build_body(chunk, boundary)
                resp = post_batch(self._token(), boundary, body)
                if resp.status_code in (429, 503) or (
                    resp.status_code == 403 and "quota" in (resp.text or "").lower()
                ):
                    sleep(30.0 if resp.status_code == 403 else 2.0)
                    resp = post_batch(self._token(), boundary, body)
                if resp.status_code == 401:
                    self._creds.refresh(GoogleAuthRequest())
                    resp = post_batch(self._token(), boundary, body)
                if resp.status_code >= 400:
                    continue
                items = _parse_batch_response(
                    resp.content, resp.headers.get("Content-Type")
                )
                if len(items) != len(chunk):
                    retry.extend(chunk)
                    continue
                for idx, (status, raw) in enumerate(items):
                    mid = chunk[idx]
                    if raw is not None:
                        results[mid] = _parse_metadata(
                            raw,
                            mid,
                            raw.get("threadId", ""),
                            raw.get("snippet", ""),
                            raw.get("labelIds", []),
                        )
                    elif status in _RETRY_STATUSES:
                        retry.append(mid)
            pending = retry
            if pending and attempt == 0:
                sleep(2.0)
        return results

    def get_full(self, message_id: str) -> dict:
        return self._request("GET", f"/messages/{message_id}", params=[("format", "full")])

    def get_attachment(self, message_id: str, attachment_id: str) -> dict:
        return self._request("GET", f"/messages/{message_id}/attachments/{attachment_id}")

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


def _chunks(seq: list[str], size: int) -> list[list[str]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _parse_batch_response(content: bytes, content_type: str | None) -> list[tuple[int, dict | None]]:
    """Split a multipart/mixed batch response into (status, json) pairs.

    The Gmail batch response body carries no top-level MIME headers, only its
    own boundary (announced in the Content-Type), so split on that boundary
    manually. Parts keep request order; each embeds an `HTTP/1.1 <code>`
    status line, and 2xx parts carry the message resource to parse. Non-JSON
    parts yield (status, None) so callers can spot throttled/errored ids.
    """
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        return []
    delimiter = b"--" + m.group(1).encode("ascii", "ignore")
    out: list[tuple[int, dict | None]] = []
    for blob in content.split(delimiter):
        blob = blob.strip(b"\r\n")
        if not blob or blob == b"--":
            continue
        _, _, sub = blob.partition(b"\r\n\r\n")
        head, _, payload = sub.strip(b"\r\n").partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        try:
            status = int(status_line.split(b" ", 2)[1] or 0)
        except (IndexError, ValueError):
            status = 0
        parsed = None
        if 200 <= status < 300:
            try:
                candidate = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                candidate = None
            if isinstance(candidate, dict):
                parsed = candidate
        out.append((status, parsed))
    return out


def get_metadata_batch(client: GmailClient, message_ids: list[str]) -> list[dict]:
    """Fetch metadata for a batch via the Gmail multipart batch endpoint.

    One HTTP POST covers up to 100 ids (vs 200 threads before). Skips
    failures silently and preserves the caller's id ordering.
    """
    by_id = client.batch_get_metadata(message_ids)
    return [by_id[mid] for mid in message_ids if mid in by_id]


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

_CID_ATTR_RE = re.compile(r"(?i)(src|poster|background)=([\"']\s*)cid:([^\"'<>\s]+)")
_CID_CSS_RE  = re.compile(r"(?i)(?:url\(\s*)cid:([^\"'()\s]+)")


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


def _strip_html(html_source: str) -> str:
    stripped = _STYLE_SCRIPT_RE.sub(" ", html_source)
    stripped = _COMMENT_RE.sub(" ", stripped)
    return html.unescape(_TAG_RE.sub(" ", stripped)).strip()


def _clean_ws(text: str) -> str:
    combined = text.replace("\r\n", "\n").replace("\r", "\n")
    combined = _MULTI_WS_RE.sub(" ", combined)
    combined = re.sub(r"[ \t]+\n", "\n", combined)  # trailing ws per line
    combined = re.sub(r"\n[ \t]+", "\n", combined)  # leading indent per line
    combined = _BLANK_RUN_RE.sub("\n\n", combined)  # max one blank line
    return combined.strip(" \t\n")


def _extract_text(payload: dict) -> str:
    parts = payload.get("parts") or []
    if not parts:
        decoded = _decode_body(payload)
        if (payload.get("mimeType") or "") == "text/html":
            return _clean_ws(_strip_html(decoded))
        return _clean_ws(decoded)

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

    plain_text = _clean_ws("\n\n".join(plain))
    html_text = _clean_ws(_strip_html(html_source)) if html_source else ""
    if plain_text and html_text:
        if len(html_text) > len(plain_text) * 1.5:
            return html_text
        return plain_text
    return plain_text or html_text


def _flatten_html(parts: list[dict], out: list[str]) -> None:
    for p in parts:
        mime = p.get("mimeType", "")
        if mime == "text/html" and p.get("body", {}).get("data"):
            out.append(_decode_body(p))
        if p.get("parts"):
            _flatten_html(p.get("parts", []), out)


def _extract_html(payload: dict) -> str:
    """Return the raw HTML source of the top body (first text/html part)."""
    parts = payload.get("parts") or []
    if not parts:
        if (payload.get("mimeType") or "").lower() == "text/html":
            return _decode_body(payload)
        return ""
    candidates: list[str] = []
    _flatten_html(parts, candidates)
    return candidates[0] if candidates else ""


def _cid_attachment_map(payload: dict) -> dict[str, str]:
    """Map `cid:key` (Content-ID) → attachmentId so inline figures can render."""
    mapping: dict[str, str] = {}

    def walk(parts: list[dict]) -> None:
        for p in parts or []:
            cid = ""
            for h in p.get("headers") or []:
                if (h.get("name") or "").lower() == "content-id":
                    cid = (h.get("value") or "").strip().strip("<>")
                    break
            if cid and (p.get("body") or {}).get("attachmentId"):
                mapping[cid] = p["body"]["attachmentId"]
            if p.get("parts"):
                walk(p["parts"])

    walk(payload.get("parts") or [])
    return mapping


def _rewrite_cid_urls(html_source: str, message_id: str, mapping: dict[str, str]) -> str:
    """Rewrite `cid:` src/href/background figures to our lazy attachment proxy."""
    if not html_source or not mapping:
        return html_source

    def repl_attr(m: "re.Match") -> str:
        att_id = mapping.get(m.group(3).strip())
        if not att_id:
            return m.group(0)
        return f'{m.group(1)}={m.group(2)}/api/messages/{message_id}/attachments/{att_id}'

    def repl_css(m: "re.Match") -> str:
        att_id = mapping.get(m.group(1).strip().split("/", 1)[0])
        if not att_id:
            return m.group(0)
        return f"url(/api/messages/{message_id}/attachments/{att_id}"

    out = _CID_ATTR_RE.sub(repl_attr, html_source)
    return _CID_CSS_RE.sub(repl_css, out)


def extract_attachments(payload: dict) -> list[dict]:
    """Attachment descriptors (filename/mime/size/attachmentId), recursively."""
    found: list[dict] = []

    def walk(parts: list[dict]) -> None:
        for p in parts or []:
            filename = p.get("filename") or ""
            body = p.get("body", {}) or {}
            if filename and body.get("attachmentId"):
                found.append({
                    "filename": filename,
                    "mimeType": p.get("mimeType", "") or "application/octet-stream",
                    "size": int(body.get("size", 0) or 0),
                    "attachmentId": body["attachmentId"],
                })
            if p.get("parts"):
                walk(p["parts"])

    if (payload.get("filename") or "") and (payload.get("body", {}) or {}).get("attachmentId"):
        walk([payload])
    walk(payload.get("parts", []))
    return found


def get_full(client: GmailClient, message_id: str) -> dict:
    """Full message for rendering (body text + payload for the AI layer)."""
    raw = client.get_full(message_id)
    payload = raw.get("payload", {})
    headers = payload.get("headers", [])
    sender_name, sender_email = parse_sender(_effective_header(headers, "From"))

    body_text = _extract_text(payload)
    truncated = len(body_text) > config.BODY_MAX_CHARS
    body_text = body_text[: config.BODY_MAX_CHARS]

    html_source = _extract_html(payload)
    if html_source:
        html_source = _rewrite_cid_urls(
            html_source, message_id, _cid_attachment_map(payload)
        )

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
        "body_html": html_source,
        "has_html": bool(html_source),
        "attachments": extract_attachments(payload),
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


def get_attachment(client: GmailClient, message_id: str, attachment_id: str) -> dict:
    result = {}

    def fetch():
        result.update(client.get_attachment(message_id, attachment_id))

    _call(fetch)
    return result





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