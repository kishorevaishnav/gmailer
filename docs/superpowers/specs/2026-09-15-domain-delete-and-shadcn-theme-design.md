# Design: Domain-level Bulk Delete + shadcn-style Theming

Date: 2026-09-15
Status: Approved (user: "yes go ahead", primary = violet)

## Goals

1. Add a per-domain **Delete** button to the email-viewer page so all emails from a
   sender domain can be trashed in one go (Gmail + local cache), as a background job.
2. Introduce a **shadcn-style theme system** supporting dark and light (plus the
   existing Apple theme) across all four pages: `index`, `email-viewer`, `rules`, `todos`.
3. Keep the violet accent as the shadcn `primary` for brand consistency.

## Scope today

- Backend: one new endpoint + one new store function.
- Frontend: email-viewer delete button + shadcn token theme across all pages + shared
  theme toggle (including the currently toggle-less pages).
- No changes to the main inbox page's existing bulk delete/archive behavior.
- The Apple theme is kept as a third option, left visually untouched except where it
  needs to coexist with the new token classes.

## Part 1 — Domain delete (email-viewer)

### Backend

**`backend/store.py`** — add:

```python
def remove_messages(message_ids: list[str]) -> int:
    """Delete the given message ids from the local messages cache.
    Returns number of rows removed."""
```

Implementation: `DELETE FROM messages WHERE id IN (...)` under the existing `_lock`,
following the same pattern/logger as `clear_cache`. No migration, no schema change.

**`main.py`** — add near the bundle endpoints:

```python
class DomainTrashRequest(BaseModel):
    message_ids: list[str]

@app.post("/api/domains/{domain}/trash")
def api_domain_trash(domain: str, req: DomainTrashRequest):
    service = require_service()
    if not req.message_ids:
        raise HTTPException(status_code=400, detail="No message_ids provided")
    failed = _run(gmail_service.bulk_execute, service, req.message_ids, gmail_service.trash)
    for mid in req.message_ids:
        if mid not in failed:
            _record_trace(service, mid, "trashed")
    removed = store.remove_messages([mid for mid in req.message_ids if mid not in failed])
    return {"ok": True, "processed": len(req.message_ids) - len(failed),
            "failed": failed, "removed_from_cache": removed}
```

- Reuses the existing `_run` (auth/session renewal guard) and `_record_trace`.
- Traces recorded as `trashed` so rules/wikis stay anchored.

### Frontend — `email-viewer.js` / `email-viewer.css`

- Add a red **Delete** button (trash icon + label) on each domain card header, placed
  after the count badge. `e.stopPropagation()` so it never toggles expand.
- Behavior:
  - Click → `confirm("Delete N email(s) from <domain>? ...")`
  - Collect `state.emails` ids where `domainOf(e.senderEmail) === domain`.
  - Optimistically remove the domain's emails from `state.emails`, recompute
    `state.domainSummaries`, `renderAll()`.
  - `POST /api/domains/{domain}/trash` with `{message_ids}` (fire/wait pattern like
    main inbox's `queueBulk`). On success → `toast(processed ok)`. On failure →
    `toast(err)`, re-add the emails, re-render.
  - Visual "deleting…" state while in-flight (spinner + disabled button), keyed per domain.
- If unauthenticated (viewer runs on cached data), the endpoint returns 401 → catch,
  toast, keep emails in listing.
- Guard: skip ids that are also TODO items (mirror `actOn`'s guard) if the email data
  has todo info; if unknown, no-op.

### Testing (backend)

- `backend/tests`: add a test for `store.remove_messages` (insert rows, remove subset,
  assert count and that others remain).
- Add a test for `POST /api/domains/example.com/trash` using the existing test harness
  pattern for authenticated endpoints (mock gmail service) asserting: response shape,
  Gmail bulk trash called, cache rows removed for non-failed ids.
- Run: `.venv/bin/python -m pytest backend/tests -q` (known failures: 6 wiki tests —
  unaffected).
- `node --check static/email-viewer.js`, `.venv/bin/python -m py_compile main.py
  backend/store.py`.

## Part 2 — shadcn-style theme (all pages, 3 themes)

### Token layer

**`app.css` and `email-viewer.css`**

Define shadcn HSL tokens on `:root` (light) and `.dark` (dark). Primary = violet.

```css
:root {
  --background: 0 0% 100%;        /* white */
  --foreground: 240 10% 3.9%;     /* neutral-950 */
  --card: 0 0% 100%;
  --card-foreground: 240 10% 3.9%;
  --popover: 0 0% 100%;
  --popover-foreground: 240 10% 3.9%;
  --primary: 262 83% 58%;         /* violet-500 */
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
  --background: 240 10% 3.9%;     /* neutral-950 */
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

- Add shared token-driven component classes: `.card`, `.btn`, `.btn-primary`,
  `.btn-outline`, `.input`, `.chip`, `.focus-ring` (focus-visible ring), `.hairline`
  (border). These mirror shadcn and are used across all pages.
- Keep existing dark/light adjustments where they already work; Apple theme classes
  retained verbatim.

### Tailwind config (all 4 HTML files)

Extend the Tailwind CDN config so the token palette is available as utility classes
AND `dark:` variants resolve correctly:

```js
tailwind.config = {
  darkMode: "class",
  theme: { extend: {
    colors: {
      background: "hsl(var(--background))",
      foreground: "hsl(var(--foreground))",
      card: { DEFAULT: "hsl(var(--card))", foreground: "hsl(var(--card-foreground))" },
      primary: { DEFAULT: "hsl(var(--primary))", foreground: "hsl(var(--primary-foreground))" },
      secondary: { ... }, muted: { ... }, accent: { ... }, destructive: { ... },
      border: "hsl(var(--border))", input: "hsl(var(--input))", ring: "hsl(var(--ring))",
    },
    borderRadius: {
      lg: "var(--radius)", md: "calc(var(--radius) - 2px)", sm: "calc(var(--radius) - 4px)",
    },
  }},
};
```

### Page-by-page class migration

Replace hardcoded `bg-slate-*`/`text-slate-*`/`border-slate-*` surface classes with
token classes (`bg-card`, `text-foreground`, `border-border`, `bg-muted`,
`text-muted-foreground`) in:

- `index.html`, `rules.html`, `todos.html`, `email-viewer.html`
- JS templates in `app.js`, `rules.js`, `todos.js`, `email-viewer.js`
  (email cards, group rows, sidebar rows, rule cards, todo rows, modal shells, toasts).

Semantic status colors (violet/emerald/red/amber/sky) for actions/categories/badges are
KEPT as-is — only neutral surfaces migrate to tokens. This keeps risk low and preserves
the app's information color coding.

### Theme toggle (shared behavior)

- The 3-way toggle already lives in `app.js` (`toggleTheme`): dark → light → apple.
- Standalone pages that don't load `app.js` (`email-viewer`, `rules`, `todos`) get a
  small shared snippet from `app.css` token classes + their own toggle handling:
  - Persist/read `localStorage["gmailer-theme"]` default `dark`.
  - `email-viewer`, `rules`, `todos`: apply `.dark` class / `data-theme="apple"` in the
    inline `<head>` script (same pattern as index), and add a theme button in their
    headers that cycles dark → light → apple.
- All pages keep honoring `?theme=` query param override exactly as index.html does
  today.

### Verification

- `node --check static/*.js` for every touched script.
- `.venv/bin/python -m py_compile` for touched Python.
- `.venv/bin/python -m pytest backend/tests -q`.
- Restart app; curl endpoint. Visual smoke on all 4 pages in dark/light/apple.

## Out of scope

- Permanent (hard) Gmail deletion — all deletes use Gmail trash (30-day recovery),
  consistent with the rest of the app.
- Changes to main inbox bulk delete/archive endpoints or the `queueBulk` UX.
- Redesigning the Apple theme aesthetic.

## Risks / notes

- Viewer's `postMessage`-free design means re-render after optimistic removal must
  recompute domain summaries (`buildDomainSummaries`) — already a pure function of
  `state.emails`.
- Email-viewer currently loads cached messages only; deleted cache rows legitimately
  disappear forever unless re-fetched. This matches the requested behavior ("here too").