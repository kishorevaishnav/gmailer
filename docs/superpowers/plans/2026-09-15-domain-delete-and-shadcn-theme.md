# Domain-Level Delete + shadcn Theme Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-domain Delete button to the email-viewer (Gmail trash + local cache removal, background job) and introduce a shadcn-style token theme (dark/light + Apple) across all four pages.

**Architecture:** (1) New `POST /api/domains/{domain}/trash` endpoint reuses the existing `_run`/`_record_trace`/`gmail_service.bulk_execute` infra plus a new `store.remove_messages()` to purge the local cache. (2) A shadcn HSL CSS-variable token layer (light `:root`, `.dark`) maps into Tailwind CDN config on every page; neutral surfaces migrate to token classes, semantic colors stay. Theme toggle (dark → light → apple) extends to the three standalone pages.

**Tech Stack:** FastAPI, SQLite (`backend/store.py`), vanilla JS, Tailwind CDN (darkMode: class), CSS variables.

---

### Task 1: `store.remove_messages()` + tests

**Files:**
- Modify: `backend/store.py` (add function after `list_messages`, ~line 415)
- Test: `backend/tests/test_store_messages.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_store_messages.py`:

```python
from backend import store


def test_remove_messages_deletes_only_given_ids():
    store.save_message({"id": "m1", "sender_email": "a@x.com", "subject": "s", "internal_date_ms": 1})
    store.save_message({"id": "m2", "sender_email": "b@x.com", "subject": "s", "internal_date_ms": 2})
    store.save_message({"id": "m3", "sender_email": "c@x.com", "subject": "s", "internal_date_ms": 3})

    removed = store.remove_messages(["m1", "m3"])

    assert removed == 2
    ids = [m["id"] for m in store.list_messages()]
    assert ids == ["m2"]


def test_remove_messages_missing_ids_ignored():
    store.save_message({"id": "m1", "sender_email": "a@x.com", "subject": "s", "internal_date_ms": 1})
    assert store.remove_messages(["ghost", "m1"]) == 1
    assert store.list_messages() == []


def test_remove_messages_empty_is_noop():
    assert store.remove_messages([]) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest backend/tests/test_store_messages.py -q`
Expected: FAIL with `AttributeError: module 'backend.store' has no attribute 'remove_messages'`

- [ ] **Step 3: Implement `remove_messages`**

Add to `backend/store.py` right after `list_messages` (after line 415):

```python
def remove_messages(message_ids: list[str]) -> int:
    """Delete the given message ids from the local messages cache.
    Returns number of rows removed."""
    if not message_ids:
        return 0
    try:
        placeholders = ",".join("?" for _ in message_ids)
        with _lock:
            cur = _get().execute(
                f"DELETE FROM messages WHERE id IN ({placeholders})",
                tuple(message_ids),
            )
            _get().commit()
        return cur.rowcount
    except Exception as exc:
        logger.warning("store.remove_messages failed: %s", exc)
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest backend/tests/test_store_messages.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/store.py backend/tests/test_store_messages.py
git commit -m "feat: add store.remove_messages for cache purge"
```

---

### Task 2: Domain trash endpoint + tests

**Files:**
- Modify: `main.py` (add endpoint after `api_bundle_archive`, line ~1002)
- Test: `backend/tests/test_domain_trash.py` (create)

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_domain_trash.py`:

```python
import pytest
from fastapi.testclient import TestClient

from backend import store
from main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _seed(mid, domain="example.com"):
    store.save_message({
        "id": mid, "sender_name": "Sender", "sender_email": f"noreply@{domain}",
        "subject": "s", "snippet": "", "internal_date_ms": 1,
        "label_ids": [], "body_text": "",
    })


def test_domain_trash_caches_removed_and_gmail_called(client, monkeypatch):
    _seed("m1"); _seed("m2"); _seed("m3", domain="other.com")
    trashed = []
    monkeypatch.setattr("main.require_service", lambda: object())
    monkeypatch.setattr(
        "main.gmail_service.bulk_execute",
        lambda c, ids, fn: (trashed.extend(ids), [])[1],
    )
    monkeypatch.setattr("main._record_trace", lambda *a, **k: None)

    r = client.post("/api/domains/example.com/trash", json={"message_ids": ["m1", "m2", "m3"]})

    assert r.status_code == 200
    body = r.json()
    assert body["processed"] == 3
    assert body["failed"] == []
    assert body["removed_from_cache"] == 2
    assert set(trashed) == {"m1", "m2", "m3"}
    assert [m["id"] for m in store.list_messages()] == ["m3"]


def test_domain_trash_failed_ids_kept_in_cache(client, monkeypatch):
    _seed("m1"); _seed("m2")
    monkeypatch.setattr("main.require_service", lambda: object())
    monkeypatch.setattr(
        "main.gmail_service.bulk_execute",
        lambda c, ids, fn: ["m1"],  # m1 fails
    )
    monkeypatch.setattr("main._record_trace", lambda *a, **k: None)

    r = client.post("/api/domains/example.com/trash", json={"message_ids": ["m1", "m2"]})

    body = r.json()
    assert body["processed"] == 1
    assert body["failed"] == ["m1"]
    assert body["removed_from_cache"] == 1
    assert {m["id"] for m in store.list_messages()} == {"m1"}


def test_domain_trash_empty_ids_400(client):
    r = client.post("/api/domains/example.com/trash", json={"message_ids": []})
    assert r.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest backend/tests/test_domain_trash.py -q`
Expected: FAIL with `404 Not Found` for `/api/domains/example.com/trash`

- [ ] **Step 3: Implement the endpoint**

Add to `main.py` after `api_bundle_archive` (line ~1002):

```python
@app.post("/api/domains/{domain}/trash")
def api_domain_trash(domain: str, req: BulkRequest):
    service = require_service()
    if not req.message_ids:
        raise HTTPException(status_code=400, detail="No message_ids provided")
    failed = _run(gmail_service.bulk_execute, service, req.message_ids, gmail_service.trash)
    for mid in req.message_ids:
        if mid not in failed:
            _record_trace(service, mid, "trashed")
    removed = store.remove_messages([mid for mid in req.message_ids if mid not in failed])
    return {
        "ok": True,
        "processed": len(req.message_ids) - len(failed),
        "failed": failed,
        "removed_from_cache": removed,
        "undo": {"action": "trash", "message_ids": req.message_ids},
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest backend/tests/test_domain_trash.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/python -m pytest backend/tests -q`
Expected: Only the 6 known-failing wiki tests fail (they expect legacy `skill_md`).

- [ ] **Step 6: Commit**

```bash
git add main.py backend/tests/test_domain_trash.py
git commit -m "feat: add domain trash endpoint with cache purge"
```

---

### Task 3: shadcn token layer in `app.css`

**Files:**
- Modify: `static/app.css` (add token block at top, component classes near bottom)

- [ ] **Step 1: Add shadcn HSL tokens**

Insert at the very top of `static/app.css` (before existing `:root`):

```css
:root {
  --background: 0 0% 100%;
  --foreground: 240 10% 3.9%;
  --card: 0 0% 100%;
  --card-foreground: 240 10% 3.9%;
  --popover: 0 0% 100%;
  --popover-foreground: 240 10% 3.9%;
  --primary: 262 83% 58%;
  --primary-foreground: 0 0% 100%;
  --secondary: 240 4.8% 95.9%;
  --secondary-foreground: 240 5.9% 10%;
  --muted: 240 4.8% 95.9%;
  --muted-foreground: 240 3.8% 46.1%;
  --accent: 240 4.8% 95.9%;
  --accent-foreground: 240 5.9% 10%;
  --destructive: 0 84.2% 60.2%;
  --destructive-foreground: 0 0% 98%;
  --border: 240 5.9% 90%;
  --input: 240 5.9% 90%;
  --ring: 262 83% 58%;
  --radius: 0.5rem;
}

.dark {
  --background: 240 10% 3.9%;
  --foreground: 0 0% 98%;
  --card: 240 10% 3.9%;
  --card-foreground: 0 0% 98%;
  --popover: 240 10% 3.9%;
  --popover-foreground: 0 0% 98%;
  --primary: 262 83% 58%;
  --primary-foreground: 0 0% 100%;
  --secondary: 240 3.7% 15.9%;
  --secondary-foreground: 0 0% 98%;
  --muted: 240 3.7% 15.9%;
  --muted-foreground: 240 5% 64.9%;
  --accent: 240 3.7% 15.9%;
  --accent-foreground: 0 0% 98%;
  --destructive: 0 62.8% 30.6%;
  --destructive-foreground: 0 0% 98%;
  --border: 240 3.7% 15.9%;
  --input: 240 3.7% 15.9%;
  --ring: 262 83% 58%;
}
```

The existing `:root { --scroll-thumb: #cbd5e1; }` block that follows must be merged — change it to stay intact after the new token block (keep `--scroll-thumb` declarations; do not duplicate `:root`).

Expected: repeat `:root` selector is invalid — ensure only ONE `:root {` and ONE `.dark {` block at the top (scroll-thumb vars can live in the same `:root`/`.dark` blocks appended to the token ones).

- [ ] **Step 2: Add shared component classes**

Append to `static/app.css` (end of file):

```css
/* ═══════════════ shadcn-style shared components ═══════════════ */
.card {
  border-radius: var(--radius);
  border: 1px solid hsl(var(--border));
  background-color: hsl(var(--card));
  color: hsl(var(--card-foreground));
}

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border-radius: calc(var(--radius) - 2px);
  border: 1px solid transparent;
  padding: 6px 14px;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.25rem;
  white-space: nowrap;
  cursor: pointer;
  transition: background-color .15s ease, border-color .15s ease, opacity .15s ease;
}
.btn:focus-visible,
.input:focus-visible,
.focus-ring:focus-visible {
  outline: none;
  box-shadow: 0 0 0 2px hsl(var(--background)), 0 0 0 4px hsl(var(--ring));
}
.btn-primary { background-color: hsl(var(--primary)); color: hsl(var(--primary-foreground)); }
.btn-primary:hover { opacity: .9; }
.btn-outline {
  background-color: hsl(var(--background));
  border-color: hsl(var(--input));
  color: hsl(var(--foreground));
}
.btn-outline:hover { background-color: hsl(var(--accent)); color: hsl(var(--accent-foreground)); }
.btn-destructive { background-color: hsl(var(--destructive)); color: hsl(var(--destructive-foreground)); }
.btn-destructive:hover { opacity: .9; }
.btn-ghost { background-color: transparent; color: hsl(var(--muted-foreground)); }
.btn-ghost:hover { background-color: hsl(var(--accent)); color: hsl(var(--accent-foreground)); }
.btn:disabled { opacity: .5; cursor: not-allowed; }

.input {
  border-radius: calc(var(--radius) - 2px);
  border: 1px solid hsl(var(--input));
  background-color: hsl(var(--background));
  color: hsl(var(--foreground));
  padding: 7px 12px;
  font-size: 13px;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.input:focus-visible { border-color: hsl(var(--ring)); }

.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border-radius: 9999px;
  padding: 2px 10px;
  font-size: 11px;
  font-weight: 700;
}
```

- [ ] **Step 3: Verify CSS parses**

Run: `.venv/bin/python - <<'EOF'
import re
css = open("static/app.css").read()
assert css.count(":root {") == 1, f":root count = {css.count(':root {')}"
assert css.count(".dark {") == 1
print("ok")
EOF`
Expected: prints `ok`

- [ ] **Step 4: Commit**

```bash
git add static/app.css
git commit -m "feat: add shadcn token layer and shared component classes"
```

---

### Task 4: shadcn tokens + delete button styles in `email-viewer.css`

**Files:**
- Modify: `static/email-viewer.css`

- [ ] **Step 1: Add token block + dark-mode overrides**

Replace the top of `static/email-viewer.css` (lines 1-7) with:

```css
/* ═══════════════════════════════════════════════════════════════
   Email Viewer — shadcn tokens + dark support (indigo accent kept)
   ═══════════════════════════════════════════════════════════════ */

:root {
  --indigo: #4f46e5;
  --background: 0 0% 100%;
  --foreground: 240 10% 3.9%;
  --card: 0 0% 100%;
  --card-foreground: 240 10% 3.9%;
  --primary: 262 83% 58%;
  --primary-foreground: 0 0% 100%;
  --secondary: 240 4.8% 95.9%;
  --secondary-foreground: 240 5.9% 10%;
  --muted: 240 4.8% 95.9%;
  --muted-foreground: 240 3.8% 46.1%;
  --accent: 240 4.8% 95.9%;
  --accent-foreground: 240 5.9% 10%;
  --destructive: 0 84.2% 60.2%;
  --destructive-foreground: 0 0% 98%;
  --border: 240 5.9% 90%;
  --input: 240 5.9% 90%;
  --ring: 262 83% 58%;
  --radius: 0.5rem;
}

.dark {
  --background: 240 10% 3.9%;
  --foreground: 0 0% 98%;
  --card: 240 10% 3.9%;
  --card-foreground: 0 0% 98%;
  --primary: 262 83% 58%;
  --primary-foreground: 0 0% 100%;
  --secondary: 240 3.7% 15.9%;
  --secondary-foreground: 0 0% 98%;
  --muted: 240 3.7% 15.9%;
  --muted-foreground: 240 5% 64.9%;
  --accent: 240 3.7% 15.9%;
  --accent-foreground: 0 0% 98%;
  --destructive: 0 62.8% 30.6%;
  --destructive-foreground: 0 0% 98%;
  --border: 240 3.7% 15.9%;
  --input: 240 3.7% 15.9%;
  --ring: 262 83% 58%;
}
```

- [ ] **Step 2: Convert existing selectors to tokens + add dark variants**

Replace the hardcoded color literals in `static/email-viewer.css` per this mapping and append `.dark` equivalents (keep the shorthand rule groups):

| Old selector / value | New |
|---|---|
| scrollbar thumb `#cbd5e1` | `hsl(var(--border))` |
| `.domain-card` border `#e2e8f0`, bg `#fff` | `hsl(var(--border))`, `hsl(var(--card))` |
| `.domain-card[aria-expanded="true"]` border `#6366f1` | `hsl(var(--ring))` |
| `.domain-summary` bg `#f8fafc`, border `#e2e8f0`, left `#4f46e5` | `hsl(var(--muted))`, `hsl(var(--border))`, keep `#4f46e5` |
| `.ai-badge` bg `#eef2ff`, color `#4338ca` | keep (indigo accent) |
| `.email-ai-box` bg `#f8fafc`, border `#e2e8f0` | `hsl(var(--muted))`, `hsl(var(--border))` |
| `.shimmer` gradients | keep, add `.dark .shimmer` using `#1e293b/#28374d` (copy from app.css) |
| `.cat-chip` border `#cbd5e1`, bg `#fff`, color `#475569` | `hsl(var(--input))`, `hsl(var(--card))`, `hsl(var(--muted-foreground))` |
| `.cat-chip[aria-pressed="true"]` | `border-color: hsl(var(--ring)); background: hsl(var(--primary) / .12); color: hsl(var(--primary))` |
| `.email-row` border `#e2e8f0`, bg `#fff` | `hsl(var(--border))`, `hsl(var(--card))` |
| `.email-row:hover` border `#94a3b8`, bg `#f8fafc` | `hsl(var(--muted-foreground))`, `hsl(var(--muted))` |
| `.email-row[aria-selected="true"]` | `border-color: hsl(var(--ring)); background: hsl(var(--primary) / .12)` |
| `.toast` bg `#fff`, border `#e2e8f0`, color `#0f172a` | `hsl(var(--card))`, `hsl(var(--border))`, `hsl(var(--card-foreground))` |

Then append dark variants for selectors that set text on cards:

```css
.dark .domain-summary,
.dark .email-ai-box { background: hsl(var(--muted)); border-color: hsl(var(--border)); }
.dark .email-row p,
.dark .domain-summary p,
.dark .email-ai-box p { color: hsl(var(--muted-foreground)); }
.dark .email-row {
  background: hsl(var(--card));
  border-color: hsl(var(--border));
}
.dark .email-row:hover { background: hsl(var(--muted)); border-color: hsl(var(--muted-foreground)); }
.dark .email-row[aria-selected="true"] {
  background: hsl(var(--primary) / .12);
  border-color: hsl(var(--ring));
}
.dark .cat-chip {
  background: hsl(var(--card));
  border-color: hsl(var(--input));
  color: hsl(var(--muted-foreground));
}
.dark .cat-chip[aria-pressed="true"] {
  background: hsl(var(--primary) / .12);
  border-color: hsl(var(--ring));
  color: hsl(var(--primary));
}
.dark .toast {
  background: hsl(var(--card));
  border-color: hsl(var(--border));
  color: hsl(var(--card-foreground));
}
.dark .shimmer {
  background: linear-gradient(90deg, #1e293b 25%, #28374d 37%, #1e293b 63%);
  background-size: 400% 100%;
}
```

- [ ] **Step 3: Add the domain delete button style**

Append to `static/email-viewer.css`:

```css
.domain-delete-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border-radius: 9999px;
  border: 1px solid hsl(var(--destructive) / .35);
  background: hsl(var(--destructive) / .1);
  color: hsl(var(--destructive));
  padding: 2px 9px;
  font-size: 10px;
  font-weight: 700;
  cursor: pointer;
  transition: background-color .15s ease, opacity .15s ease;
}
.domain-delete-btn:hover:not(:disabled) { background: hsl(var(--destructive)); color: #fff; }
.domain-delete-btn:disabled { opacity: .5; cursor: not-allowed; }
```

- [ ] **Step 4: Commit**

```bash
git add static/email-viewer.css
git commit -m "feat: shadcn tokens + dark mode + delete button styles for email-viewer"
```

---

### Task 5: `email-viewer.html` — tokens config, dark mode, theme toggle, header button

**Files:**
- Modify: `static/email-viewer.html`

- [ ] **Step 1: Add theme bootstrap + Tailwind token config**

Replace the `<head>` block (lines 7-11) with:

```html
  <script>
    (function () {
      try {
        var q = new URLSearchParams(window.location.search).get("theme");
        var t = q || localStorage.getItem("gmailer-theme") || "dark";
        if (t === "apple") {
          document.documentElement.setAttribute("data-theme", "apple");
        } else if (t !== "light") {
          document.documentElement.classList.add("dark");
        }
      } catch (e) {}
    })();
  </script>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: "class",
      theme: {
        extend: {
          colors: {
            background: "hsl(var(--background))",
            foreground: "hsl(var(--foreground))",
            card: { DEFAULT: "hsl(var(--card))", foreground: "hsl(var(--card-foreground))" },
            popover: { DEFAULT: "hsl(var(--popover))", foreground: "hsl(var(--popover-foreground))" },
            primary: { DEFAULT: "hsl(var(--primary))", foreground: "hsl(var(--primary-foreground))" },
            secondary: { DEFAULT: "hsl(var(--secondary))", foreground: "hsl(var(--secondary-foreground))" },
            muted: { DEFAULT: "hsl(var(--muted))", foreground: "hsl(var(--muted-foreground))" },
            accent: { DEFAULT: "hsl(var(--accent))", foreground: "hsl(var(--accent-foreground))" },
            destructive: { DEFAULT: "hsl(var(--destructive))", foreground: "hsl(var(--destructive-foreground))" },
            border: "hsl(var(--border))",
            input: "hsl(var(--input))",
            ring: "hsl(var(--ring))",
          },
          borderRadius: {
            lg: "var(--radius)",
            md: "calc(var(--radius) - 2px)",
            sm: "calc(var(--radius) - 4px)",
          },
        },
      },
    };
  </script>
```

- [ ] **Step 2: Swap body + surfaces to tokens**

In `static/email-viewer.html`:
- `<body>`: replace `bg-slate-50 text-slate-900` with `bg-background text-foreground`
- `<header>`: `border-slate-200 bg-white` → `border-border bg-card`
- `<span id="debugStatus">`: `text-slate-400` → `text-muted-foreground`
- `<span id="resultCount">`: `text-slate-500` → `text-muted-foreground`
- expand-all button: `border-slate-300 text-slate-700 hover:bg-slate-100` → `btn btn-outline` + `text-muted-foreground`
- filter row + "Filter" label: border/h-text to tokens (`border-border`, `text-muted-foreground`)
- `<aside>`: `border-slate-200 bg-white` → `border-border bg-card`
- aside header "Domains" label: `text-slate-500` → `text-muted-foreground`; `#domainCount` `text-slate-400` → `text-muted-foreground`
- `<main>`: `bg-slate-50` → `bg-background`

- [ ] **Step 3: Add theme toggle button to header**

Replace the expand-all button block (line 26) with:

```html
      <button id="selectAllBtn" class="btn btn-outline text-[11px]">Expand all</button>
      <button id="themeBtn" class="btn btn-outline px-2.5 py-1.5" title="Theme: current — click to switch">
        <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>
        </svg>
      </button>
```

- [ ] **Step 4: Add the toggle logic**

Append an inline script at the end of `<body>` (before `email-viewer.js`) that wires the theme button:

```html
  <script src="/static/email-viewer.js"></script>
  <script>
    (function () {
      var themes = ["dark", "light", "apple"];
      var btn = document.getElementById("themeBtn");
      if (!btn) return;
      function cur() {
        if (document.documentElement.classList.contains("dark")) return "dark";
        if (document.documentElement.getAttribute("data-theme") === "apple") return "apple";
        return "light";
      }
      btn.addEventListener("click", function () {
        var next = themes[(themes.indexOf(cur()) + 1) % themes.length];
        document.documentElement.classList.toggle("dark", next === "dark");
        if (next === "apple") document.documentElement.setAttribute("data-theme", "apple");
        else document.documentElement.removeAttribute("data-theme");
        try { localStorage.setItem("gmailer-theme", next); } catch (e) {}
      });
    })();
  </script>
```

- [ ] **Step 5: Commit**

```bash
git add static/email-viewer.html
git commit -m "feat: shadcn tokens + theme toggle on email-viewer page"
```

---

### Task 6: `email-viewer.js` — domain delete button logic

**Files:**
- Modify: `static/email-viewer.js`

- [ ] **Step 1: Add `state.busyDomains` set**

In the `state` object (line 77-86), add:

```js
  busyDomains: new Set(),   // domains currently running a background trash op
```

- [ ] **Step 2: Add delete button to `domainCard()`**

Modify `domainCard(domain, items)` (line 245). Replace the header `<button ... data-toggle-domain>` block (lines 272-279) so the whole row is a flex container with the toggle button keeping most of the surface, and add the delete button AFTER the count badge:

```js
function domainCard(domain, items) {
  const expanded = state.expandedDomains.has(domain);
  const ds = (state.domainSummaries || {})[domain];
  const count = items.length;
  const busy = state.busyDomains.has(domain);

  let body = "";
  if (expanded) {
    const summaryBox = ds
      ? `<div class="domain-summary flex items-start gap-2 px-3 py-2.5 mb-2">
          ${I.spark()}
          <div class="min-w-0">
            <p class="text-[10px] font-bold uppercase tracking-wider text-indigo-600 mb-0.5">AI Domain Summary</p>
            <p class="text-[12px] text-slate-700 leading-relaxed">${esc(ds.summary)}</p>
          </div>
        </div>`
      : "";
    const rows = items.map((e) => emailRow(e)).join("");
    body = `${summaryBox}<div class="space-y-1.5">${rows}</div>`;
  } else {
    body = `<div class="space-y-1.5">
      ${items.slice(0, 2).map((e) => emailRow(e)).join("")}
      ${count > 2 ? `<p class="text-[11px] text-slate-400 pl-1">+${count - 2} more email${count - 2 === 1 ? "" : "s"}</p>` : ""}
    </div>`;
  }

  return `
    <div class="domain-card" aria-expanded="${expanded ? "true" : "false"}">
      <div class="flex items-center gap-1 pr-2">
        <button type="button" data-toggle-domain="${esc(domain)}"
          class="min-w-0 flex-1 flex items-center gap-2 px-3 py-2 text-left">
          ${I.chevron(expanded)}
          ${I.folder()}
          <span class="min-w-0 flex-1 text-[13px] font-semibold text-slate-800 truncate">${esc(domain)}</span>
          <span class="ai-badge rounded px-1.5 py-0.5 text-[10px] font-bold">AI</span>
          <span class="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-bold text-slate-600">${count}</span>
        </button>
        <button type="button" data-del-domain="${esc(domain)}" ${busy ? "disabled" : ""}
          class="domain-delete-btn shrink-0"
          title="Delete all ${count} email${count === 1 ? "" : "s"} from ${esc(domain)} (moves to Gmail trash + removes from cache)">
          ${busy ? I.spinner() : `<svg class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>`}
          ${busy ? "" : "Delete"}
        </button>
      </div>
      <div class="px-3 pb-3">${body}</div>
    </div>`;
}
```

- [ ] **Step 3: Wire the delete click handler**

In `renderDomainList()`, after the existing `[data-toggle-domain]` handler block (lines 228-236), add:

```js
  list?.querySelectorAll("[data-del-domain]").forEach((btn) =>
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const domain = btn.dataset.delDomain;
      await deleteDomainEmails(domain);
    })
  );
```

- [ ] **Step 4: Add `deleteDomainEmails()` (with in-flight spinner state)**

Add near `buildDomainSummaries`:

```js
async function deleteDomainEmails(domain) {
  const affected = state.emails.filter((e) => e.domain === domain);
  if (!affected.length) return;
  const confirmMsg = affected.length === 1
    ? `Delete 1 email from ${domain}? It moves to Gmail trash (30-day recovery) and is removed from this view.`
    : `Delete all ${affected.length} emails from ${domain}? They move to Gmail trash (30-day recovery) and are removed from this view.`;
  if (!window.confirm(confirmMsg)) return;

  const ids = affected.map((e) => e.id);
  const failIds = new Set();
  state.busyDomains.add(domain);
  renderDomainList();
  try {
    const res = await api(`/api/domains/${encodeURIComponent(domain)}/trash`, {
      method: "POST",
      body: JSON.stringify({ message_ids: ids }),
    });
    (res.failed || []).forEach((id) => failIds.add(id));
    state.emails = state.emails.filter((e) => e.domain !== domain || failIds.has(e.id));
    state.domainSummaries = buildDomainSummaries();
    if (state.activeEmailId && !state.emails.some((e) => e.id === state.activeEmailId)) {
      state.activeEmailId = null;
    }
    state.expandedDomains.delete(domain);
    const ok = ids.length - failIds.size;
    if (ok > 0) toast(`Deleted ${ok} email${ok === 1 ? "" : "s"} from ${domain} · synced to Gmail`, "ok");
    if (failIds.size) toast(`${failIds.size} email${failIds.size === 1 ? "" : "s"} failed — kept in view. Retry.`, "err");
    renderAll();
  } catch (err) {
    toast(`Delete failed: ${err.message}`, "err");
    renderDomainList();
  } finally {
    state.busyDomains.delete(domain);
  }
}
```

- [ ] **Step 5: Verify JS syntax**

Run: `node --check static/email-viewer.js`
Expected: no output (exit 0)

- [ ] **Step 7: Commit**

```bash
git add static/email-viewer.js
git commit -m "feat: domain-level delete button on email-viewer"
```

---

### Task 7: `index.html` + `app.js` — token surfaces

**Files:**
- Modify: `static/index.html`
- Modify: `static/app.js`

- [ ] **Step 1: Add token config to `index.html`**

Replace `tailwind.config = { darkMode: "class" };` (line 22) with the full token config (same block as Task 5 Step 1).

- [ ] **Step 2: Migrate index.html neutral surfaces to tokens**

Replace in `static/index.html`:
- `<body>`: `bg-slate-100 text-slate-900 dark:bg-slate-950 dark:text-slate-100` → `bg-background text-foreground`
- `<aside>` (line 72): `bg-white dark:bg-slate-900/70` → `bg-card`; `border-slate-200 dark:border-slate-800` → `border-border`
- sidebar `<header>` + divs (lines 74-116): all `border-slate-200 dark:border-slate-800` → `border-border`; `text-slate-600 dark:text-slate-500` → `text-muted-foreground`
- progress track `<div>` (line 86): `bg-slate-200 dark:bg-slate-800` → `bg-muted`
- `queueMeta`/`cacheInfo` text → `text-muted-foreground`
- skipped button → `btn btn-outline` + keep text-xs
- main `<header>` (line 120): `bg-white/70 dark:bg-slate-900/40` → `bg-card/70`; border → `border-border`
- all header pill buttons (Lines/Rules/Todo/theme/undo/cache/skip/loadmore/reload) `border-slate-300 dark:border-slate-700 text-slate-600 dark:text-slate-300` → `btn btn-outline`
- search input (line 127): `border-slate-300 dark:border-slate-700 text-slate-700 dark:text-slate-200` → `input` class + `text-foreground`
- modals `border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900` → `border-border bg-card`; title texts `text-slate-500` → `text-muted-foreground`; inputs → `input` class
- `emptyScreen` (line 221): `bg-slate-100 dark:bg-slate-950` → `bg-background`
- `authScreen` texts `text-slate-600 dark:text-slate-400` → `text-muted-foreground`
- editor `#editorContent`, `editorErrors` (lines 161-176): borders `border-slate-300 dark:border-slate-700` → `border-border`; `bg-white dark:bg-slate-900` → `bg-card`; `text-slate-800 dark:text-slate-200` → `text-foreground`

- [ ] **Step 3: Migrate `app.js` templates to tokens**

In `static/app.js`, replace these hardcoded neutral classes with tokens (semantic colors violet/emerald/red/amber/sky stay):

| Old | New |
|---|---|
| `bg-white dark:bg-slate-900/60` (surfaces) | `bg-card` |
| `border-slate-200 dark:border-slate-800` | `border-border` |
| `border-slate-300 dark:border-slate-700` | `border-border` (on buttons keep `btn-outline` where applicable) |
| `text-slate-500 dark:text-slate-600` | `text-muted-foreground` |
| `text-slate-600 dark:text-slate-400`/`text-slate-500` | `text-muted-foreground` |
| `text-slate-800 dark:text-slate-200` | `text-foreground` |
| `text-slate-900 dark:text-slate-100` | `text-foreground` |
| `text-slate-400 dark:text-slate-600` | `text-muted-foreground` |
| `bg-slate-50 dark:bg-slate-900/60` (subgroup header) | `bg-muted` |
| `bg-slate-100` count/badge | `bg-muted text-muted-foreground` |
| `toast` colors: `bg-white border-slate-300 text-slate-700 dark:bg-slate-800 dark:border-slate-700 dark:text-slate-100` | `bg-card border-border text-card-foreground` |
| toast undo button `bg-slate-200 ... dark:bg-slate-700/70` | `btn btn-outline` |

Apply to: `renderPendingOps`, `historyList`, `renderThreadRows`, `renderCategoryRows`, `groupRow`, `renderGroupView`, `renderSearchView`, `renderCategoryGroupView`, `emailCard`, `detailHTML`, `renderSidebar`, modals. Keep every `violet-*`, `emerald-*`, `red-*`, `amber-*`, `sky-*` class untouched.

- [ ] **Step 4: Verify**

Run: `node --check static/app.js`
Expected: no output (exit 0)

- [ ] **Step 5: Commit**

```bash
git add static/index.html static/app.js
git commit -m "feat: shadcn tokens on inbox page (html + templates)"
```

---

### Task 8: `rules.html` + `rules.js` — tokens + theme toggle

**Files:**
- Modify: `static/rules.html`
- Modify: `static/rules.js`

- [ ] **Step 1: rules.html head — add apple handling + token config**

Replace the bootstrap block and tailwind config in `static/rules.html` head with the same code from Task 5 Steps 1 (theme bootstrap handling apple) + token config.

- [ ] **Step 2: Migrate rules.html surfaces to tokens**

Replace in `static/rules.html`:
- `<body>`: `bg-slate-100 text-slate-900 dark:bg-slate-950 dark:text-slate-100` → `bg-background text-foreground`
- `<header>`: `bg-white/80 dark:bg-slate-900/70` → `bg-card/80`; `border-slate-200 dark:border-slate-800` → `border-border`
- header links/buttons/inputs → `btn btn-outline` / `input` where applicable; `text-slate-500` labels → `text-muted-foreground`
- Add theme button in header `.ml-auto` group before Refresh:

```html
        <button id="themeBtn" class="btn btn-outline px-2.5 py-1.5 rounded-lg" title="Toggle theme">
          <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>
          </svg>
        </button>
```

- All `border-slate-300 dark:border-slate-700` inputs → `input` class + `text-foreground`; form card `border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/60` → `border-border bg-card`; `#ruleMarkdown` `bg-white dark:bg-slate-950` → `bg-background text-foreground`; tab buttons keep.

- **theme toggle snippet** — add before `</body>` (after rules.js):

```html
  <script>
    (function () {
      var themes = ["dark", "light", "apple"];
      var btn = document.getElementById("themeBtn");
      if (!btn) return;
      function cur() {
        if (document.documentElement.classList.contains("dark")) return "dark";
        if (document.documentElement.getAttribute("data-theme") === "apple") return "apple";
        return "light";
      }
      btn.addEventListener("click", function () {
        var next = themes[(themes.indexOf(cur()) + 1) % themes.length];
        document.documentElement.classList.toggle("dark", next === "dark");
        if (next === "apple") document.documentElement.setAttribute("data-theme", "apple");
        else document.documentElement.removeAttribute("data-theme");
        try { localStorage.setItem("gmailer-theme", next); } catch (e) {}
      });
    })();
  </script>
```

- [ ] **Step 3: Migrate rules.js templates to tokens**

Same mapping table as Task 8 Step 3, applied to `renderRuleCard`, `renderProposal`, `renderWiki`, `renderTrace`, `renderSenderMap`, modal shells in `static/rules.js`.

- [ ] **Step 4: Verify**

Run: `node --check static/rules.js`
Expected: no output (exit 0)

- [ ] **Step 5: Commit**

```bash
git add static/rules.html static/rules.js
git commit -m "feat: shadcn tokens + theme toggle on rules page"
```

---

### Task 9: `todos.html` + `todos.js` — tokens + theme toggle

**Files:**
- Modify: `static/todos.html`
- Modify: `static/todos.js`

- [ ] **Step 1: todos.html head — apple handling + token config + theme button + toggle**

Same pattern as Task 9 Steps 1-2: replace bootstrap/config, body/header surfaces, add `#themeBtn` in header `.ml-auto` group, add the closing theme toggle snippet.

- [ ] **Step 2: Migrate todos.js templates to tokens**

Apply the Task 8 Step 3 mapping table to `static/todos.js` (list rows, sort buttons, empty state).

- [ ] **Step 3: Verify**

Run: `node --check static/todos.js`
Expected: no output (exit 0)

- [ ] **Step 4: Commit**

```bash
git add static/todos.html static/todos.js
git commit -m "feat: shadcn tokens + theme toggle on todos page"
```

---

### Task 10: Apple theme coexistence check

**Files:**
- Modify: `static/app.css` (if needed)

- [ ] **Step 1: Ensure Apple glass surfaces still render under token classes**

The Apple overrides in `app.css` target `[data-theme="apple"] aside`, `.group-card`, `#groupHeader` etc. In dark/light token mode those elements now carry `bg-card` etc. — Apple's `!important` rules already override cards; add one guard so Apple's sidebar/main headers keep glass:

Check that `[data-theme="apple"] aside` and header selectors still apply. If token classes introduced `background-color` on `main > header` without `!important`, add:

```css
[data-theme="apple"] main > header,
[data-theme="apple"] aside,
[data-theme="apple"] aside > header {
  background-color: transparent;
}
```

- [ ] **Step 2: Verify no CSS regression**

Run: `.venv/bin/python - <<'EOF'
import re
css = open("static/app.css").read()
assert css.count(":root {") == 1
assert css.count(".dark {") == 1
print("ok")
EOF`
Expected: prints `ok`

- [ ] **Step 3: Commit**

```bash
git add static/app.css
git commit -m "fix: keep Apple glass under shadcn token classes"
```

---

### Task 11: Dev-loop verification (manual smoke)

**Files:** none

- [ ] **Step 1: Syntax + tests**

Run all three:
```bash
node --check static/app.js && node --check static/rules.js && node --check static/todos.js && node --check static/email-viewer.js
.venv/bin/python -m py_compile main.py backend/store.py
.venv/bin/python -m pytest backend/tests -q
```
Expected: node checks silent; py_compile silent; pytest only the 6 known wiki failures.

- [ ] **Step 2: Start server and smoke the endpoint**

```bash
.venv/bin/uvicorn main:app --port 8601 --reload --reload-include '*.py'
```
Then (in a second shell):
```bash
curl -s -X POST localhost:8601/api/domains/example.com/trash -H 'Content-Type: application/json' -d '{"message_ids": []}' | head -c 300
```
Expected: `401` when not signed in (`{detail:...}`) proving the route exists, or `{"detail":"No message_ids provided"}` (400) when an auth session is present.
Also `curl -s localhost:8601/email-viewer | head -c 200` → contains the theme bootstrap script.

- [ ] **Step 3: Manual visual check (user)**

Open `/`, `/email-viewer`, `/rules`, `/todos` — click the theme button until light, dark, and apple all render correctly; in the email-viewer expand a domain, click Delete, confirm, verify emails disappear and toast shows.

- [ ] **Step 4: No commit** (smoke only)