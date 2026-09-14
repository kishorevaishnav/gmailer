# Gmailer — Keyboard-Driven Inbox Triage

A local web app that connects to your Gmail via OAuth 2.0 and lets you mass-triage
unread email at keyboard speed. No mouse. No clutter. Just: **D** delete, **E** archive, **S** star, **→** next.

## What it does

- Pulls the latest **200 unread** messages from your inbox into a queue.
- **Bundles senders** — if LinkedIn sent you 23 emails, you see one banner:
  *"You have 23 emails from LinkedIn"* with **Bulk Delete All** / **Bulk Archive All**.
  Bulk senders are surfaced first so you can clear entire mailing lists in a click.
- One email at a time, full focus, with an **AI TL;DR Summary** on top (mock/placeholder for now — drop in a real LLM call later).
- Pre-fetches the next 2 emails in the background → zero lag between actions.
- Lives in a dark, high-contrast, zero-mouse interface.

### Hotkeys

| Key | Action |
|---|---|
| `D` / `Backspace` | Trash email and advance |
| `E` / `A` | Archive email and advance |
| `S` | Star email and advance |
| `→` / `←` | Skip / go back |
| `U` | Undo last action |
| `⏎` | Reply composer *(Phase 2 — reserved)* |

## File structure

```
gmailer/
├── requirements.txt        # Python dependencies
├── main.py                 # FastAPI server + all API routes
├── backend/
│   ├── config.py           # Paths, scopes, batch sizes
│   ├── auth.py             # OAuth 2.0 flow + token storage/refresh
│   ├── gmail_service.py    # Google API client (fetch, trash, archive, star, undo)
│   ├── queue.py            # Batch fetch + sender bundling + ordering
│   └── ai_summary.py       # AI TL;DR layer (mock → plug your LLM here)
└── static/
    ├── index.html          # Single-page UI (sidebar + triage viewport)
    ├── app.css             # Dark theme, slide animations, hotkey styling
    └── app.js              # Queue state, prefetch, actions, keyboard handling
```

## 1. One-time Google Cloud setup (~5 min)

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and **create a project** (or pick an existing one).
2. Open **APIs & Services → Library** and enable the **Gmail API**.
3. Go to **APIs & Services → OAuth consent screen**:
   - Choose **External** (or Internal if you have a Workspace org), fill the app name + your email, and save.
   - Under *Scopes*, add `https://www.googleapis.com/auth/gmail.modify`.
   - Under *Test users*, add your own Gmail address (while the app is in "Testing" mode).
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Name it `gmailer` — **leave redirect URIs blank** (Desktop clients auto-accept loopback).
   - Download the JSON and save it as **`credentials.json`** in this project's root folder.
5. *(Safety note)* This is a local app. Your `token.json` grants modify access to your
   inbox — keep both files out of version control and delete `token.json` when done.

> **Troubleshooting:** if Google rejects the redirect after sign-in
> (`redirect_uri_mismatch`), re-create the client with "Web application" type and
> add `http://localhost:8000/auth/callback` as an Authorized redirect URI.

## 2. Run it

```bash
# inside this folder
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --port 8000
```

Open **http://localhost:8000**, click **Sign in with Google**, approve, and you land in the triage view.

> `python3` needs to be ≥ 3.10 (e.g. `/opt/homebrew/bin/python3.11`). If your default is 3.9,
> use the full path.

## 3. API surface

| Endpoint | Purpose |
|---|---|
| `GET  /auth` | Start OAuth flow (redirects to Google) |
| `GET  /auth/callback` | OAuth callback → saves `token.json` |
| `GET  /api/status` | Auth state, account email |
| `GET  /api/queue?max_results=200` | Fetch + bundle unread inbox (max 500) |
| `GET  /api/messages/{id}` | Full email body + AI summary |
| `POST /api/messages/{id}/trash` | Move to trash |
| `POST /api/messages/{id}/archive` | Remove `INBOX` label |
| `POST /api/messages/{id}/star` | Add `STARRED` label |
| `POST /api/bundles/{sender}/trash` | Bulk trash `{"message_ids": [...]}` |
| `POST /api/bundles/{sender}/archive` | Bulk archive `{"message_ids": [...]}` |
| `POST /api/undo` | Inverse of `{"action", "message_ids"}` |
| `POST /api/auth/logout` | Delete saved token |
| `GET  /rules` | Rules & Wiki management page |
| `GET  /api/rules` | List all rules |
| `POST /api/rules` | Create rule (skill markdown) |
| `PUT  /api/rules/{id}` | Update rule (skill markdown) |
| `DELETE /api/rules/{id}` | Delete rule |
| `POST /api/rules/{id}/toggle` | Enable/disable |
| `POST /api/rules/{id}/move` | Change precedence |
| `POST /api/rules/{id}/apply` | Apply to existing mail |
| `GET  /api/wiki` | List wiki observations |
| `POST /api/wiki/{id}/dismiss` | Dismiss observation |
| `POST /api/wiki/{id}/create-rule` | Create rule from observation |
| `GET  /api/proposals` | List pending proposals |
| `POST /api/proposals/{id}/approve` | Approve proposal |
| `POST /api/proposals/{id}/reject` | Reject proposal |
| `POST /api/evolve` | Run WikiSkill evolution loop |
| `GET  /api/traces` | List recent traces |
| `GET  /api/categories` | List categories |
| `POST /api/categories/add` | Add category |
| `POST /api/categories/remove` | Remove category |
| `POST /api/skipped/add` | Skip for now |
| `POST /api/skipped/remove` | Restore skipped |
| `POST /api/skipped/clear` | Restore all skipped |
| `GET  /api/skipped` | List skipped |
| `GET  /api/blocked` | List blocked senders |
| `GET  /api/promo-blocked` | List promo-blocked senders |
| `POST /api/blocked/add` | Block sender |
| `POST /api/blocked/remove` | Unblock sender |
| `POST /api/promo-blocked/add` | Enable promo auto-delete |
| `POST /api/promo-blocked/remove` | Disable promo auto-delete |
| `POST /api/cache/clear` | Clear local cache |
| `GET  /api/groups/{sender}/summarize` | Summarize group |

Scope used: `gmail.modify` — read, trash, archive, star, label. No permanent deletion, no sends.

## 4. Where's the AI summary?

`backend/ai_summary.py` ships a deterministic mock (1 sentence + 3 bullets). To make it real,
set an `LLM_API_KEY` and implement `_llm_summarize()` — the frontend already renders whatever
`{one_liner, bullets}` you return.

## Phase 2 (not built yet)

- Quick-reply composer (`⏎` reserved).
- Sender allow/block lists + auto-thrash thresholds.