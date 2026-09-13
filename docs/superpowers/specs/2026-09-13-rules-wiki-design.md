# Design: WikiSkill Rules & Wiki (editable auto-delete engine)

Date: 2026-09-13
Status: Approved in conversation 2026-09-13
Stack: FastAPI (`main.py`, `backend/`), vanilla JS + Tailwind CDN (`static/`), SQLite (`data/gmailer.db`), local Ollama `gemma3:4b`

## 1. Goal

Replace the hardcoded "blocked sender" and "promo auto-delete" lists with a
general, **user-editable rule engine** modeled on Google Research's WikiSkill
framework (arXiv:2608.27454): three layers — immutable execution traces (Raw),
a compounding knowledge base (Wiki), and editable procedural rules (Skills).
Rules are edited as **raw markdown skill files** in the frontend, validated on
save. Two local **AI agent roles** (running on the existing Ollama model) do the
learning — a *Wiki Maintainer* that distills traces into the knowledge base and
a *Skill Proposer* that drafts reasoned rule changes from it. The system
proposes rule changes from observed behavior; the user edits, approves, or
rejects each proposal before it goes live (suggest + approve). No cloud, no
downloaded WikiSkill code, no multi-agent orchestration framework — both agents
are single Ollama calls inside this codebase.

## 2. Decisions (confirmed)

1. **Editing model**: suggest + approve. System proposes; user edits/approves.
2. **Match conditions** (all at launch): sender (address / `@domain` / display
   name), promo-only scope, subject keywords, AI category, frequency/volume.
3. **Actions**: `trash`, `star`, `skip`. (No `archive`.)
4. **Proposal timing**: smart badge on pattern. Passive detector flags strong
   patterns; a badge appears; click to open, edit markdown, approve or reject.
5. **Existing lists**: migrate `blocked` + `promo_blocked` into rules on boot,
   then retire those tables/endpoints/UI chips.
6. **Editing surface**: raw markdown editor, validated server-side on save
   (errors reported with line numbers).
7. **Wiki layer (initial)**: read-only observations with evidence lists and a
   "turn into rule" affordance. User will react to the working version before
   we expand it.
8. **UI placement**: sidebar section "Rules & Wiki" (option A). Rules are always
   tied to the emails they acted on — evidence counts + latest-mail links.
9. **Precedence**: rules run in order; first match wins. Reorderable.
10. **Apply-now**: approving/saving a rule offers "apply to existing mail now"
    (trash/star/skip unread matches immediately).
11. **Quick-create**: `Block` button and `P` key stay as rule quick-creators
    (explicit user action → rule is live immediately, no proposal). Only
    behavior *observed* by the system goes through propose → approve.
12. **True WikiSkill agent loop** (local, Ollama `gemma3:4b`): a **Wiki
    Maintainer** agent periodically distills `traces` into wiki observations,
    and a **Skill Proposer** agent reads the wiki + evidence and drafts each
    proposal (rule markdown + rationale + downside). Human approval is the
    gate — no auto-approve, no rollback machinery. A cheap deterministic
    pre-filter bounds which evidence clusters reach the agents (compute guard).

## 3. Rule format (skill markdown)

Each rule is a markdown file parsed by `backend/rules.py` into a structured
rule. Strict parse: unknown keys, bad actions, bad addresses, empty match →
400 with line-numbered errors.

```markdown
---
name: Amazon deals → trash
enabled: true
action: trash            # trash | star | skip
scope: all_mail          # all_mail | promo_only
---
## match
sender: @amazon.com              # optional: exact addr | @domain | name
subject: ["receipt", "invoice"]  # optional keywords (case-insensitive substring)
category: ["Offer/Deal"]         # optional AI categories
frequency:
  emails_per_day: 3              # optional threshold

## about
Freeform notes; shown on the rule card and in the editor.
```

- `sender`, `subject`, `category`, `frequency` are ANDed within a rule.
- A sender displayed as `@domain` matches any address at that domain; a plain
  string matches the display name (normalized, case-insensitive) or exact
  address; an address with `@` matches that exact address.
- `scope: promo_only` adds `item.promo` (Gmail `CATEGORY_PROMOTIONS`) to the
  match — this reproduces today's promo auto-delete.
- `frequency.emails_per_day`: matches when the sender's volume over the last
  7 days exceeds the threshold (computed from `traces`, not Gmail).

## 4. Data model (SQLite, new tables)

```sql
CREATE TABLE traces (            -- RAW Layer: immutable execution log
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  message_id TEXT,
  sender_email TEXT, sender_name TEXT,
  subject TEXT, promo INTEGER DEFAULT 0, category TEXT,
  action TEXT NOT NULL,          -- reviewed|trashed|starred|skipped|kept|auto
  rule_id INTEGER                -- null unless a rule fired
);
CREATE INDEX idx_traces_sender ON traces(sender_email, action, ts);

CREATE TABLE rules (             -- SKILL Layer: editable markdown + parsed copy
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_md TEXT NOT NULL,
  parsed_json TEXT NOT NULL,
  enabled INTEGER DEFAULT 1,
  precedence INTEGER NOT NULL UNIQUE,
  created_at REAL, updated_at REAL
);

CREATE TABLE wiki_observations ( -- WIKI Layer: compounding knowledge
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,            -- sender|keyword|category|frequency
  target TEXT NOT NULL,          -- sender email/domain | keyword | category name
  summary TEXT NOT NULL,         -- human-readable observation
  evidence_count INTEGER DEFAULT 0,
  signal REAL DEFAULT 0,
  first_seen REAL, last_seen REAL,
  status TEXT DEFAULT 'open',    -- open|dismissed|converted
  rule_id INTEGER
);

CREATE TABLE rule_proposals (    -- pending suggest+approve queue
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,          -- proposer
  label TEXT NOT NULL,           -- "StockAlertsPress — 4/4 latest trashed"
  summary TEXT,                  -- agent's plain-English proposal summary
  rationale TEXT,                -- agent's "why" (evidence-driven)
  downside TEXT,                 -- agent's "what could go wrong"
  evidence_json TEXT NOT NULL,   -- trace ids + rendered list
  proposed_skill_md TEXT NOT NULL,
  rule_id INTEGER, status TEXT DEFAULT 'pending',
  created_at REAL, rejected_until REAL   -- suppress re-proposal 30d on reject
);
```

`traces` rows are appended (never updated) on: user *reads* an email (expands
or opens it), every explicit user action, and every auto-action. Merely
loading an item in a batch does **not** write a trace (avoids ~200 rows/pull).
Capped retention (e.g. 90 days) via trim on ingest.

## 5. Engine

- `backend/rules.py`:
  - `parse_skill_md(md) -> Rule` + `validate(...) -> [errors]` (line numbers).
  - `match_item(rule, item, ctx)` — evaluates the four conditions + environment
    (`ctx` carries a per-sender frequency map from `traces`).
- `main.py::api_queue` runs rules **before** surfacing items (same point today's
  blocked auto-delete runs): enabled rules in precedence order, first match
  wins → `trash` (Gmail trash + `store.remove_skipped`), `star` (Gmail
  star/label), `skip` (`store.add_skipped` + hide). Response adds
  `auto_trashed`, `auto_starred`, `auto_skipped` counts; queue never shows
  auto-processed mail. Trace rows are written for every auto-action.
- Apply-now (`POST /api/rules/{id}/apply`): re-match **current unread** mail.
  Sender-based rules scan `list_sender_unread_ids`; subject/category/frequency
  rules scan the current queue batch items (documented MVP limitation). Only
  unread mail is touched; results counted and toasted.

## 6. Evolution loop (Wiki Maintainer + Skill Proposer agents)

Orchestrated on `POST /api/evolve` (called on demand + auto, debounced, after a
triage session). All agent calls are single Ollama (`gemma3:4b`) invocations in
`backend/wiki.py`; there is no agent framework, just these two roles.

1. **Pre-filter (deterministic, no LLM)** — bounds compute by mining `traces`
   (last 30 days) into candidate evidence clusters:
   - **sender-consistent**: ≥3 mail from a sender with ≥90% one action and ≥1
     within last 7d → sender rule with that action.
   - **promo-heavy**: sender's latest 5 ≥90% promo and user trashed ≥3 →
     sender + `scope: promo_only` + trash.
   - **keyword**: ≥3 trashed across senders sharing a subject keyword (min 4
     chars) → keyword rule; **category**: ≥3 trashed with same AI category →
     category rule.
   - **frequency**: same sender >3 mail/day over 7d while mostly trashed →
     frequency rule.
2. **Wiki Maintainer agent**: for each new cluster, distills the raw traces
   into a `wiki_observations` row — `summary` (findings), `evidence_count`,
   `signal`, first/last seen. Unchanged clusters are touched only when their
   evidence significantly changes (prevent churn).
3. **Skill Proposer agent**: reads the updated observations + their evidence
   and drafts a `rule_proposals` row — `label`, `summary`, `rationale` (why it
   fits your behavior), `downside` (what could go wrong, e.g. over-delete),
   and `proposed_skill_md`. One proposal per eligible cluster per evolve.
4. **Gate = human**: the badge appears; the user edits markdown, approves
   (→ live rule, optional apply-now), or rejects. Reject sets
   `rejected_until` (+30 days) — the proposer never re-offers it before then.
5. **Dedup**: skip if an identical enabled rule already exists or the same
   proposal is pending/rejected-within-30d.
6. **Bounds**: ≤10 clusters/evolve; thresholds live in `backend/config.py`;
   agent failures are non-fatal (grade to template text, keep the queue
   triage unaffected).

## 7. API

```
GET    /api/rules                 -> {items:[rule cards (parsed, counts)]}
POST   /api/rules                 {skill_md}               -> create (parse|400)
PUT    /api/rules/{id}            {skill_md?, enabled?, precedence?}
POST   /api/rules/{id}/toggle                             -> {enabled}
POST   /api/rules/{id}/move       {dir: up|down}          -> reorder
POST   /api/rules/{id}/apply                               -> apply-now counts
DELETE /api/rules/{id}

GET    /api/wiki                  -> {items:[observations]}
POST   /api/wiki/{id}/dismiss
POST   /api/wiki/{id}/create-rule -> creates proposal from observation

GET    /api/traces?limit=50       -> recent trace rows (evidence view)
POST   /api/evolve                -> run detector+proposer -> {added, proposals}
GET    /api/proposals             -> {items:[pending]}
POST   /api/proposals/{id}/approve  {skill_md?, apply_now?}
POST   /api/proposals/{id}/reject
```

Rule create/edit validate `skill_md`; invalid → `400 {ok:false, errors:[{line,msg}]}`.

## 8. Frontend (sidebar section A)

- **Sidebar block “Rules & Wiki”** (collapsible, badge = pending proposals).
  - Proposals pinned above rules: `PROPOSAL — <label>` + agent's `summary`
    (collapsible `why` / `what could go wrong`) + evidence list +
    `[approve] [edit skill] [reject]`.
  - Rule row: name, on/off toggle, precedence ▲▼, `<N> <action> · <M> recent`,
    action label, `▸ see latest →` (expands the exact trailing emails, linked
    for recoverability), `[edit skill] [apply now] [delete]`.
  - Wiki tab: observation rows (kind icon, summary, `signal`, `N evidence`,
    `▸ evidence`, `[create rule]`, `[dismiss]`).
- **Editor**: opens in the main pane (group view replaced) — monospace textarea
  with the skill markdown, live server validation on Save, inline line-number
  errors, `Apply now` checkbox, Cancel/Save/Delete.
- **Quick-create**: `Block` and `P` (promo) create rules directly
  (`trash` all mail / `trash` promos from that sender), toast + `edit skill`
  link. `B` reuse existing `issueBlock` path; keyboard `P` calls the same.
- **Migration**: on boot, `blocked` → sender/`trash`/`all_mail`; `promo_blocked`
  → sender/`trash`/`promo_only`; then old tables are dropped and the old
  sidebar chips (`Blocked:`, `Promo auto-delete:`) are removed from the UI.
- `auto_deleted` toasts now read `auto_trashed/starred/skipped` counts.

## 9. Error handling

- Rule parse failure → 400 with line numbers; editor shows them inline.
- Gmail quota 403 → existing 30s backoff in `gmail_service`; apply-now reports
  `{done, failed}` and toasts failures.
- Detector/proposer failures are non-fatal: `evolve` returns what succeeded,
  keeps queue triage unaffected.

## 10. Testing

- `pytest` (added to requirements) in `backend/tests/`:
  - `rules_test.py`: parser (valid/invalid, line numbers), match semantics
    (sender forms, promo scope, keywords, category, frequency, AND logic,
    precedence first-match-wins).
  - `wiki_test.py`: pre-filter candidate generation from synthetic `traces`;
    Maintainer/Proposer with a **mocked Ollama client** (assert wiki rows +
    proposal rationale/downside fields are populated, malformed LLM output
    grades to template fallback); dedup + 30d reject suppression; migration
    of blocked → rules.
  - `engine_test.py`: api_queue integration (rules applied before surfacing,
    counts, apply-now on fake client).
- Keep the existing verify loop: `node --check static/app.js`, el-map
  invariant, `.venv/bin/python3.11 -c "import main"`.

## 11. Out of scope (now)

- `archive` action, multi-action rules, regex matches, per-rule scheduling.
- Full wiki editing / user notes (revisit after the working version feedback).
- Rule testing/backtesting UI ("what if this rule had run").
- Apply-now for subject/category/frequency beyond the current queue batch.