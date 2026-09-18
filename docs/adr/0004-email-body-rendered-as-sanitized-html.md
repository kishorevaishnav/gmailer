# Email bodies render as sanitized HTML, "Gmail-like" formatting included

Users want to read the actual formatted email, not a wall of extracted text.
Gmail messages carry their own HTML; we now store that HTML verbatim and render
it (sanitized) on a light "paper" surface so bold, lists, tables, banners,
colors and inline images appear as the sender intended.

## Decisions

- **Capture raw HTML**: `get_full` extracts the message's HTML body
  (`body_html`, untruncated) alongside the existing AI/text body (`body_text`,
  still capped at 80k for summarization/search). Cid images are rewritten to
  our own attachment proxy (`/api/messages/{id}/attachments/{attachmentId}`)
  so no third-party request fires when a cid image renders.
- **Client-side sanitize**: no build step. DOMPurify is vendored at
  `static/vendor/purify.min.js` (MIT) and every card render funnels through it
  before `innerHTML`; script/style-tag/iframe/js-URL classes are dropped while
  inline styles and formatting tags survive.
- **Always-light paper**: the formatted body renders on `.email-paper`
  (fixed white surface) in both dark and light chrome so sender colors read as
  intended.
- **External images blocked by default**: any `<img>` or CSS `url()` pointing
  off the app (a tracking/leak vector) is stripped to a placeholder; a
  per-sender "Show images" choice is remembered in
  `localStorage[gmailer.allowImages.<sender>]`.
- **Whole-message reader**: a full-screen overlay shows the entire formatted
  email; long emails can also open it from the expanded card.
- **Lazy capture**: body_html is only fetched when a message is first expanded
  (one-time backfill for already-cached messages); there is no bulk backfill.

## Storage

Schema adds `body_html TEXT` and `has_html INTEGER` to `messages`.
`has_html` is written with **no default** during migration so pre-migration
rows read `NULL` ("unknown") and are lazily backfilled on first expand,
whereas fresh inserts default to `0`. A cached row is only served directly when
`display_complete` (`has_html == 0` or `body_html` present); otherwise the
detail endpoint fetches from Gmail once and persists the html.