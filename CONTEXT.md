# Gmailer

A single-user local tool for triaging Gmail. It pulls the unread inbox into a
live queue, asks a local Ollama model to summarize each email, and lets the user
delete/archive/star mail, park items as todos, and teach reusable rules that
auto-triage future mail.

## Language

**Summary**:
The AI output for one email: a factual one-liner, up to 3 bullets, plus a
category, an action-needed flag, money detail, and flags.
_Avoid_: TL;DR, digest

**Hungry**:
A message that still needs a real AI summary. It has no summary yet, or only a
fallback mock summary. Only hungry messages are worked by the background
summarizer. Mock summaries count as hungry, not done.
_Avoid_: unsaved, pending email

**Real summary**:
A summary with a one-liner that is not a mock/fallback output.
_Avoid_: complete summary

**Queue**:
The live list of unread emails pulled from Gmail on each load. Always
re-fetched; never the source of truth. Bodies and summaries live in the local
cache.
_Avoid_: inbox batch, batch

**Cache**:
The local SQLite store of messages (bodies, summaries, categories,
attachments). Survives reloads; hydrated into queue items so reloads don't
re-hit Gmail.
_Avoid_: local DB, storage

**Bundle**:
A group of messages from the same sender (or mailing-list domain), shown so the
user can bulk-delete whole senders.
_Avoid_: sender group, bundle group

**Summary-skip**:
A per-sender opt-out of AI summarizing. Summary-skipped senders still get their
message stored and shown; the AI simply never summarizes them. Distinct from
"skipped", which hides a message from the queue.
_Avoid_: block, AI-block, mute sender

**Skipped**:
A message hidden from the queue until the user asks to bring it back. Persisted
so a reload respects the choice.
_Avoid_: snoozed, hidden

**Rule**:
An editable markdown "skill": frontmatter (name/enabled/action/scope) plus a
match header (sender, subject, exact_subject, category, emails_per_day). Rules
run first-match-wins in precedence order on each queue load and trash/star/skip
matching mail.
_Avoid_: automation, skill (codebase doc wording only)

**Exact-subject rule**:
A trash rule matching a sender plus the exact subject line (case-insensitive).
Ensures matching future mail is removed from Gmail and never cached.
_Avoid_: block-this-subject, subject delete

**Highlight category**:
A category pinned to the top of the category and sender views and given a visual
accent on its cards. User-editable in Settings; defaults to Finance/Bill.
_Avoid_: priority list, featured category

**Finance/Bill**:
The default highlight category: emails that are financial bills or payments.
Real bills also hide under banks/services senders.
_Avoid_: bills, financial category

**Trash**:
Gmail trash (30-day recovery). The app never permanently deletes email from
Gmail; every "delete" action sends mail to trash.
_Avoid_: delete, hard delete

**Needs-summary**:
A message without a real (non-mock) summary whose sender is not summary-skipped;
the frontend renders it as "summary queued" and the background worker will pick
it up.
_Avoid_: awaiting AI, pending summary