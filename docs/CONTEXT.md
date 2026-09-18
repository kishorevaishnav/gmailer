# Domain Glossary — Gmailer

Living glossary of domain terms used across the app, docs, and ADRs.

## Queues, summaries, and the worker

- **Queue** — the list of unread emails pulled from Gmail into the local cache
  (`messages` table) awaiting the user's swipe-style triage.
- **Hungry** — a message whose summary is empty or only a mock fallback; the
  background worker retries hungry messages each cycle until a real one lands
  (ADR 0002).
- **Worker / summarizer** — `backend/summarizer.py`; the single background
  thread that calls the LLM strictly one message at a time. Expanding a card
  marks a message **urgent** to reorder it to the front of its queue.
- **Mock summary** — a cheap fallback summary used when the LLM is unavailable;
  counts as hungry until replaced (ADR 0002).
- **Skip AI** — a per-sender opt-out that stops the worker from summarizing
  future mail from that sender (`summary_skipped`).
- **Token / token pill** — the model's token spend hint shown beside a summary.

## Categories, rules, and senders

- **Category** — the triage label an email is grouped under (e.g.
  Finance/Bill, Updates, Promotion). Derivable from the AI summary or a rule.
- **Rule** — a sender-match (sender list + optional subject) that assigns a
  category, or trashes/deletes matching mail on queue load (sender **lists**,
  never scalars — see AGENTS.md). Rules live in `rules`/`observations`.
- **Exact-subject rule** — sender + exact subject (case-insensitive) that
  "never stores" that recurring mail: it is trashed from Gmail and purged from
  the cache (ADR 0003).
- **Block sender / domain delete** — bulk actions that trash a sender's or a
  domain's mail (always via Gmail trash, never permanent delete).
- **Highlight** — categories (default Finance/Bill) pinned above the rest in
  the categories and senders views; matching cards get a left border.

## Email bodies

- **`body_text`** — plain-text extraction of the body, capped at 80k chars,
  used by the AI summary, search, and fallback display.
- **`body_html`** — the message's raw HTML body, stored untruncated (IPv6-free
  varchar/TEXT) for formatted rendering; only captured on first expand.
- **`has_html`** — tri-state flag: `0` = known text-only, `1` = `body_html`
  present, `NULL` = unknown (pre-migration row awaiting lazy backfill).
- **Paper surface / `.email-paper`** — the always-light surface formatted email
  renders on, so sender styles read as intended in dark mode too.
- **cid image** — an inline image referenced by `cid:<key>` in the email HTML;
  rewritten server-side to our attachment proxy endpoint.
- **External image** — any image/CSS background loaded from a third-party
  host (a tracking/leak vector); blocked by default, revealed per-sender via
  "Show images".
- **Reader / full-email reader** — the full-screen overlay showing the entire
  formatted email.