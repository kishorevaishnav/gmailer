# ADR: UI/UX Redesign — Light Theme, Readable Typography, Content-First Layout

Date: 2026-09-17
Status: Proposed
Decision-maker: User

## Context

The current Gmailer UI has several usability problems that make rapid email
triage harder than it should be:

1. **Dark theme is the default.** The app forces `dark` class on load across all
   pages. The user now wants **light as the default**.
2. **Typography is cramped.** Nearly every element uses `text-xs` (0.75rem),
   `text-[10px]`, `text-[11px]`, or `text-[12px]`. On a 14-inch laptop this
   forces squinting at email subjects, senders, and AI summaries.
3. **Color is over-applied.** The email card in `index.html` has ~10 colored
   pills per email (violet category chip, amber promo badge, sky todo badge,
   emerald archive button, red delete button, amber star, etc.). The user finds
   multicolor "not fancy" unless it serves a real functional purpose.
4. **Action buttons crowd the content.** On the main triage page, each email
   card shows 8+ action buttons inline. This competes visually with the email
   subject/sender/summary — the actual information the user needs to decide
   what to do.
5. **email-viewer page is inconsistent.** It defaults to dark mode despite
   having a light aesthetic and lacks theme toggle / apple handling.

## Goals

1. **Light theme as default**, with dark and "apple" themes retained as options.
2. **Readable typography** — body text at 14px+ (≥0.875rem), labels at 13px,
   secondary metadata at 12px minimum. Never smaller.
3. **Minimal color palette** — use a neutral grayscale for surfaces, with color
   reserved for *functional* status only (destructive = red, confirmed = green,
   neutral = slate). Remove decorative multicolor badges.
4. **Content-first layout** — email subject, sender, date, and AI summary are the
   visual focal point. Action buttons demoted to a compact footer bar or revealed
   on expand.
5. **Consistency across all pages** — index, email-viewer, rules, settings, todos.

## Decisions

### D1. Theme default: light
- `index.html` boot script default changes from `"dark"` → `"light"`.
- `email-viewer.html`, `rules.html`, `todos.html`, `settings.html` get the same
  `"light"` default and full 3-way theme toggle (dark → light → apple).
- Theme toggle button appears on every page header.

### D2. Font scale
| Role | Old size | New size |
|---|---|---|
| Email subject (card header) | `text-sm` (0.875rem) | `text-base` (1rem) |
| Sender name | `text-[11px]` | `text-sm` (0.875rem) |
| Secondary metadata (time, category) | `text-[10px]` | `text-xs` (0.75rem) |
| AI summary one-liner | `text-sm` (0.875rem) | `text-base` (1rem) |
| AI summary bullets | `text-[12px]` | `text-sm` (0.875rem) |
| Action button labels | `text-[10px]` | `text-sm` (0.875rem) |
| Sidebar group label | `text-[13px]` | `text-sm` (0.875rem) |
| Header nav buttons | `text-xs` (0.75rem) | `text-sm` (0.875rem) |
| Key hint pills | `text-[11px]` | `text-xs` (0.75rem) — but larger padding |

### D3. Color palette simplification
- **Surfaces**: grayscale only (`bg-white`/`bg-slate-50`/`bg-slate-100` for
  cards, `text-slate-700`/`text-slate-500` for text).
- **Accent**: violet stays as the *single* brand accent for focus rings,
  selected states, and primary buttons. No violet on every badge.
- **Functional color** (retained, used sparingly):
  - Red = destructive (delete/trash)
  - Green = positive (archive/done/undo)
  - Amber = warnings (promo, skip)
  - Sky = informational (todo)
- **Badges**: Replace colored pill badges with **outline badges** (border +
  muted text) for categories/promo/todo. Only PAYMENT keeps a red outline.
  Action buttons use **outline style** instead of colored background.

### D4. Content-first email card layout (index.html)
- **Card header**: sender name + domain + date in a single line (font-semibold).
- **Subject**: bold, one line, primary text color.
- **AI summary**: one-liner above bullets, with adequate line-height and spacing.
- **Action bar**: collapsed by default at card level; expands on click.
  Primary actions (D/E/S) shown as small icon-buttons in a compact row.
  Secondary actions (Block, Todo, Regen, Skip, Gmail link) below a "more"
  expander or in a compact footer.

### D5. email-viewer redesign
- Default to light theme.
- Increase email row, domain card, and detail view font sizes to match D2.
- Replace colored pills with outline badges.
- Detail view: subject at `text-xl` → `text-2xl`, body at `text-sm`, action
  items at `text-sm`.

## Consequences

- **Positive**: Faster triage (can read email content at a glance), reduced eye
  strain, cleaner visual hierarchy, consistent experience across all pages.
- **Risk**: Tailwind utility class migration is mechanical but touches many
  lines across `app.js`, `rules.js`, `todos.js`, `email-viewer.js`. Mitigated
  by `node --check` on every touched JS file.
- **Risk**: Removing colored backgrounds from action buttons reduces affordance.
  Mitigated by using clear iconography + hover states.
