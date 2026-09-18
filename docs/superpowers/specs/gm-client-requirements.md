# Client Requirements — GM Email Triage Flow

Status: Approved via grill-with-docs (2026-09-17)
Related: `CONTEXT.md`, `docs/adr/0001-0003`

## 1. Background AI summaries run sequentially, one by one

**Requirement**: As soon as emails are pulled from Gmail into the queue, AI
summarization starts in the background, working sequentially one email at a time
— never in parallel — so the server doesn't grind to a halt from memory or LLM
load.

**Covers**:
- All summarization goes through the single background worker thread
  (`backend/summarizer.py`), which processes one message per cycle, paced.
- API endpoints return cached data; they do **not** generate summaries
  synchronously on demand.
- Expanding a card marks that message **urgent** so the worker reorders it to the
  front of its queue (still strictly serial).
- A queue load nudges the worker to run immediately instead of waiting for the
  fixed sweep.

**Acceptance criteria**:
- [ ] No frontend code fires parallel summarize requests (the old
      `SINGLE_SUMM_CONCURRENCY` path is gone).
- [ ] `GET /api/messages/{id}` never calls the LLM; it returns cached/body data.
- [ ] The worker still runs at most one Ollama call at a time.

## 2. Summarize only messages that are hungry — never redo

**Requirement**: The worker only summarizes messages whose summary is empty (or
only a mock fallback). Already-summarized messages are not touched.

**Covers**:
- A message with a real (non-mock) one-liner is skipped on every cycle.
- **Mock** summaries count as hungry: the worker retries them until a real
  summary lands (ADR 0002).
- Body-less messages and summary-skipped senders are left alone.

**Acceptance criteria**:
- [ ] A message with a real summary is never re-summarized.
- [ ] A mock summary is retried and eventually replaced by a real one.

## 3. Hungry emails show a "queued" placeholder plus their content

**Requirement**: If the AI summary isn't ready yet, that's fine — show a
placeholder saying the summary is in the queue, and show the email's content
(snippet) so the extracted message is visible even without a summary.

**Covers**:
- Hungry cards render a "AI summary queued — generating in background" chip
  instead of an empty shimmer.
- Cards always show the email **snippet/preview** line (no extra Gmail fetches;
  the full body stays expand-only).
- While any visible card is hungry, the frontend polls a lightweight
  `GET /api/summaries` endpoint and the card updates in place once the worker
  lands a real summary.

**Acceptance criteria**:
- [ ] A freshly fetched email with no summary shows the queued chip and a
      snippet line.
- [ ] The card updates to show its real summary without a manual reload.

## 4. "Don't do next time" — per-sender summary skip

**Requirement**: For a particular sender's email address, the user can opt out of
the AI summary steps for future emails from that sender.

**Covers**:
- A "Skip AI" button is visible on the card without expanding (promoted from the
  more-actions row).
- It records the sender in `summary_skipped`; both the worker and the summary
  generator skip that sender from then on.
- Scope is the AI **summary** only — category/todo handling is out of scope for
  this button.
- A sender list with undo lives on the Settings page.

**Acceptance criteria**:
- [ ] Toggling Skip AI stops summarization for that sender immediately (card
      shows "AI skipped" state).
- [ ] The sender is listed on Settings with a working remove/undo.

## 5. "Don't repeat this subject" — delete from Gmail, never store

**Requirement**: A button that, for this email's exact subject, deletes matching
mail from Gmail and never stores it in the local database.

**Covers**:
- Creates an **exact-subject rule** (sender + exact subject, case-insensitive,
  action trash). Future identical mail is trashed on queue load and never
  cached (ADR 0003).
- Trashes the currently-viewed email from Gmail.
- Purges any already-cached copies of that exact sender+subject.
- Uses Gmail **trash** (30-day recovery) — never permanent delete.

**Acceptance criteria**:
- [ ] Clicking the action trashes the email and removes matching rows from the
      local cache.
- [ ] Reloading the queue does not surface future matching mail.

## 6. Finance bills are highlighted first, everywhere

**Requirement**: Finance bills are highlighted above everything else and sit on
top — in both the categories view and the senders view.

**Covers**:
- **Finance/Bill** is the default highlight category, editable in Settings.
- The categories list pins the highlight category to the top; the senders list
  does the same.
- Highlighted cards get a visual accent (left border).
- Banks/services stay in the worker's summarize priority (bills hide there) but
  are only "highlighted" if added in Settings.

**Acceptance criteria**:
- [ ] Finance/Bill ranks above all other categories **and** senders.
- [ ] Highlighted cards are visually distinct.
- [ ] Changing the highlight list in Settings re-pins the views immediately.