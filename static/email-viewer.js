"use strict";
/* Email Viewer — Categorized Domain-Grouped Email Viewer with Multi-Level AI Summaries.
   Data source: real Gmailer API (/api/categories, /api/queue, /api/messages/{id}).
   Falls back to a small mock dataset when the API is unreachable or unauthenticated. */

/* ───────────────────────────── Mock dataset (fallback only) ───── */
const DOMAIN_SUMMARIES_MOCK = {
  "mail.google.com": { summary: "Work traffic from Google Workspace. Mostly calendar invites, admin notices, and security alerts. No action required unless flagged.", totalEmails: 6 },
  "github.com":       { summary: "Engineering activity — PRs, CI failures, and release notes. Two PRs await your review. One CI run failed and needs investigation.", totalEmails: 5 },
  "stripe.com":       { summary: "Billing & payments. One invoice is past due and requires attention; the rest are receipts and usage summaries.", totalEmails: 4 },
  "news.example.co":  { summary: "Newsletter digests. Low priority — archive or unsubscribe.", totalEmails: 3 },
};

const EMAILS_MOCK = [
  { id: "e1", subject: "Security alert: new sign-in", senderName: "Google", senderEmail: "no-reply@mail.google.com", domain: "mail.google.com", category: "Security", timestamp: 1726000000000, body: "A new sign-in to your account was detected on a Windows device. If this was you, no action is needed.", aiSummary: { overview: "Google account sign-in alert — informational.", keyPoints: ["New sign-in detected", "Location: San Francisco", "Device: Windows"], actionItems: ["Review if unrecognized"] } },
  { id: "e2", subject: "Your calendar invite is waiting", senderName: "Google Calendar", senderEmail: "calendar-noreply@mail.google.com", domain: "mail.google.com", category: "Calendar", timestamp: 1726001000000, body: "You have been invited to 'Q3 Planning'. Accept or decline before the event.", aiSummary: { overview: "Calendar invitation for Q3 planning meeting.", keyPoints: ["Event: Q3 Planning", "Date: Thursday", "Organizer: Ops Team"], actionItems: ["Accept or decline"] } },
  { id: "e3", subject: "PR #4821 needs your review", senderName: "GitHub", senderEmail: "notifications@github.com", domain: "github.com", category: "Engineering", timestamp: 1726002000000, body: "Pull request 4821 adds retry logic to the queue worker. Two files changed. Review requested.", aiSummary: { overview: "Pull request awaiting your code review.", keyPoints: ["PR #4821", "Adds retry logic", "2 files changed"], actionItems: ["Review PR", "Approve or request changes"] } },
  { id: "e4", subject: "Invoice #INV-9921 past due", senderName: "Stripe", senderEmail: "billing@stripe.com", domain: "stripe.com", category: "Finance/Bill", timestamp: 1726003000000, body: "Invoice INV-9921 is past due. Update your payment method to avoid service interruption.", aiSummary: { overview: "Past-due invoice requiring payment.", keyPoints: ["Invoice: INV-9921", "Amount: $49.00", "Status: past due"], actionItems: ["Pay invoice", "Update payment method"] } },
  { id: "e5", subject: "Weekly digest", senderName: "The Daily", senderEmail: "digest@news.example.co", domain: "news.example.co", category: "Newsletter", timestamp: 1726004000000, body: "Your weekly roundup of tech news. Top stories inside.", aiSummary: { overview: "Weekly newsletter digest — low priority.", keyPoints: ["Top tech stories", "5 articles"], actionItems: ["Read or archive"] } },
];

/* ───────────────────────────── API client ───── */
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || body.message || `HTTP ${res.status}`);
  }
  return res.json();
}

/* ───────────────────────────── Normalizers ───── */
function domainOf(email) {
  const e = (email || "").toLowerCase();
  const at = e.indexOf("@");
  return at >= 0 ? e.slice(at + 1) : e;
}

// Real summary shape: { one_liner, bullets, action_needed, category, money, ... }
function normalizeSummary(s) {
  if (!s) return null;
  const bullets = Array.isArray(s.bullets) ? s.bullets : [];
  const action_needed = s.action_needed || "";
  const actionItems = action_needed && action_needed !== "nothing"
    ? [action_needed === "pay" ? "Pay / settle"
       : action_needed === "respond" ? "Respond"
       : action_needed === "review" ? "Review"
       : action_needed]
    : [];
  return {
    overview: (s.one_liner || s.summary || "").slice(0, 400),
    keyPoints: bullets,
    actionItems,
    category: s.category || "",
    money: s.money || "",
  };
}

// Map a raw queue/message item into the viewer shape.
function normalizeItem(it, categories) {
  const raw = it.summary || {};
  const summary = normalizeSummary(raw);
  return {
    id: it.id,
    subject: it.subject || "(no subject)",
    senderName: it.sender_name || it.senderName || "",
    senderEmail: it.sender_email || it.senderEmail || "",
    domain: domainOf(it.sender_email || it.senderEmail),
    category: it.category || summary.category || "Unclear",
    timestamp: it.internal_date_ms || it.timestamp || 0,
    body: it.body_text || it.body || "",
    aiSummary: summary,
  };
}

/* ───────────────────────────── State ───── */
const state = {
  selectedCategoryId: "all",
  expandedDomains: new Set(),
  activeEmailId: null,
  emails: [],          // normalized viewer items
  categories: [],      // from /api/categories
  domainSummaries: {}, // { domain: { summary, totalEmails } }
  loading: true,
  error: null,
};

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const days = Math.floor(h / 24);
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function visibleEmails() {
  return state.selectedCategoryId === "all"
    ? state.emails
    : state.emails.filter((e) => e.category === state.selectedCategoryId);
}

function groupedDomains() {
  const map = new Map();
  for (const e of visibleEmails()) {
    if (!map.has(e.domain)) map.set(e.domain, []);
    map.get(e.domain).push(e);
  }
  return [...map.entries()].sort((a, b) => b[1].length - a[1].length);
}

/* Build a domain-level summary by aggregating the per-email AI overviews. */
function buildDomainSummaries() {
  const map = new Map();
  for (const e of state.emails) {
    if (!map.has(e.domain)) map.set(e.domain, []);
    map.get(e.domain).push(e);
  }
  const out = {};
  for (const [dom, items] of map.entries()) {
    const withSummary = items.filter((i) => i.aiSummary && i.aiSummary.overview);
    const overview = withSummary.length
      ? withSummary.map((i) => i.aiSummary.overview).join(" ")
      : `${items.length} email${items.length === 1 ? "" : "s"} — no AI summary yet.`;
    out[dom] = { summary: overview, totalEmails: items.length };
  }
  return out;
}

/* ───────────────────────────── Icons (Lucide-style inline SVGs) ───── */
const I = {
  chevron: (rotated) =>
    `<svg class="chevron h-4 w-4 text-slate-400 ${rotated ? "rotate-180" : ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>`,
  mail: () =>
    `<svg class="h-4 w-4 text-slate-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>`,
  spark: () =>
    `<svg class="h-3 w-3 text-indigo-500" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 0 14.3 9.7 24 12l-9.7 2.3L12 24l-2.3-9.7L0 12l9.7-2.3z"/></svg>`,
  check: () =>
    `<svg class="h-3.5 w-3.5 text-slate-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>`,
  bullet: () =>
    `<svg class="h-3.5 w-3.5 text-slate-400" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="2.5"/><circle cx="12" cy="12" r="2.5"/><circle cx="19" cy="12" r="2.5"/></svg>`,
  folder: () =>
    `<svg class="h-3.5 w-3.5 text-slate-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`,
  alert: () =>
    `<svg class="h-4 w-4 text-slate-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
  spinner: () =>
    `<svg class="h-4 w-4 animate-spin text-slate-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-6.2-8.6"/></svg>`,
};

/* ───────────────────────────── Render: category chips ───── */
function renderCategoryChips() {
  const cats = [...new Set(state.emails.map((e) => e.category).filter(Boolean))].sort();
  const allCount = state.emails.length;
  const chip = (label, cat, active, count) => {
    const pressed = active ? "true" : "false";
    return `
      <button type="button" data-cat="${esc(cat)}" aria-pressed="${pressed}"
        class="cat-chip inline-flex items-center gap-1 rounded-lg px-2.5 py-1 text-[11px] font-semibold">
        ${esc(label)}
        <span class="text-[10px] opacity-70">${count}</span>
      </button>`;
  };
  let html = chip("All", "all", state.selectedCategoryId === "all", allCount);
  for (const c of cats) {
    const n = state.emails.filter((e) => e.category === c).length;
    html += chip(c, c, state.selectedCategoryId === c, n);
  }
  $("#categoryChips").innerHTML = html;
  $("#categoryChips").querySelectorAll("[data-cat]").forEach((b) =>
    b.addEventListener("click", () => {
      state.selectedCategoryId = b.dataset.cat;
      state.activeEmailId = null;
      renderAll();
    })
  );
}

/* ───────────────────────────── Render: domain groups ───── */
function renderDomainList() {
  if (state.loading) {
    $("#domainList").innerHTML =
      `<div class="flex flex-col items-center justify-center h-full gap-3 p-8 text-center">
        ${I.spinner()}
        <p class="text-sm text-slate-600">Loading inbox…</p>
      </div>`;
    return;
  }
  if (state.error) {
    $("#domainList").innerHTML =
      `<div class="flex flex-col items-center justify-center h-full gap-3 p-8 text-center">
        ${I.alert()}
        <p class="text-sm font-semibold text-slate-800">Could not load data</p>
        <p class="text-xs text-slate-500 max-w-xs">${esc(state.error)}</p>
        <button id="retryBtn" class="mt-1 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-bold text-white hover:bg-indigo-700">Retry</button>
      </div>`;
    $("#retryBtn")?.addEventListener("click", loadData);
    return;
  }

  const groups = groupedDomains();
  $("#domainCount").textContent = `${groups.length} domain${groups.length === 1 ? "" : "s"}`;
  if (!groups.length) {
    $("#domainList").innerHTML =
      `<p class="text-sm text-slate-500 p-6 text-center">No emails match this category.</p>`;
    return;
  }
  let html = "";
  for (const [domain, items] of groups) html += domainCard(domain, items);
  $("#domainList").innerHTML = html;

  $("#domainList").querySelectorAll("[data-toggle-domain]").forEach((btn) =>
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const d = btn.dataset.toggleDomain;
      if (state.expandedDomains.has(d)) state.expandedDomains.delete(d);
      else state.expandedDomains.add(d);
      renderDomainList();
    })
  );
  $("#domainList").querySelectorAll("[data-email-id]").forEach((row) =>
    row.addEventListener("click", () => {
      state.activeEmailId = row.dataset.emailId;
      renderAll();
    })
  );
}

function domainCard(domain, items) {
  const expanded = state.expandedDomains.has(domain);
  const ds = (state.domainSummaries || {})[domain];
  const count = items.length;

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
      <button type="button" data-toggle-domain="${esc(domain)}"
        class="w-full flex items-center gap-2 px-3 py-2 text-left">
        ${I.chevron(expanded)}
        ${I.folder()}
        <span class="min-w-0 flex-1 text-[13px] font-semibold text-slate-800 truncate">${esc(domain)}</span>
        <span class="ai-badge rounded px-1.5 py-0.5 text-[10px] font-bold">AI</span>
        <span class="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-bold text-slate-600">${count}</span>
      </button>
      <div class="px-3 pb-3">${body}</div>
    </div>`;
}

function emailRow(e) {
  const active = state.activeEmailId === e.id;
  const sum = e.aiSummary || {};
  return `
    <div data-email-id="${esc(e.id)}" aria-selected="${active ? "true" : "false"}" class="email-row flex items-center gap-2 px-2.5 py-2">
      ${I.mail()}
      <div class="min-w-0 flex-1">
        <p class="text-[12px] font-semibold text-slate-800 truncate">${esc(e.subject)}</p>
        <p class="text-[11px] text-slate-500 truncate">${esc(e.senderName)} · ${fmtTime(e.timestamp)}</p>
      </div>
      ${(sum.actionItems || []).length
        ? `<span class="shrink-0 rounded bg-slate-100 px-1 py-0.5 text-[9px] font-bold text-slate-600">${sum.actionItems.length}</span>` : ""}
    </div>`;
}

/* ───────────────────────────── Render: detail view ───── */
async function renderDetail() {
  const e = state.emails.find((x) => x.id === state.activeEmailId);
  const total = visibleEmails().length;
  $("#resultCount").textContent = `${total} email${total === 1 ? "" : "s"}`;

  if (!e) {
    $("#detailView").innerHTML =
      `<div class="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
        ${I.mail()}
        <p class="text-sm font-semibold text-slate-700">No email selected</p>
        <p class="text-xs text-slate-500">Pick an email from a domain group on the left.</p>
      </div>`;
    return;
  }

  // Ensure the full message (with body + summary) is loaded for the active email.
  if (!e.body && !e._loaded) {
    e._loading = true;
    renderDetail();
    try {
      const full = await api(`/api/messages/${encodeURIComponent(e.id)}`);
      Object.assign(e, normalizeItem(full, state.categories));
      e._loaded = true;
    } catch (err) {
      e._loadError = err.message;
    } finally {
      e._loading = false;
      renderDetail();
    }
    return;
  }

  const sum = e.aiSummary || {};
  const aiBox = (sum.overview || (sum.keyPoints || []).length || (sum.actionItems || []).length)
    ? `<div class="email-ai-box flex items-start gap-2 px-3 py-2.5 mb-4">
        ${I.spark()}
        <div class="min-w-0 flex-1">
          <p class="text-[10px] font-bold uppercase tracking-wider text-indigo-600 mb-1.5">AI Summary</p>
          ${sum.overview ? `<p class="text-[12px] text-slate-700 leading-relaxed mb-2">${esc(sum.overview)}</p>` : ""}
          ${(sum.keyPoints || []).length ? `
            <div class="mb-2">
              <p class="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1">Key points</p>
              <ul class="space-y-1">
                ${sum.keyPoints.map((k) => `<li class="flex items-start gap-1.5 text-[11px] text-slate-600">${I.check()}<span class="min-w-0">${esc(k)}</span></li>`).join("")}
              </ul>
            </div>` : ""}
          ${(sum.actionItems || []).length ? `
            <div>
              <p class="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1">Action items</p>
              <ul class="space-y-1">
                ${sum.actionItems.map((a) => `<li class="flex items-start gap-1.5 text-[11px] text-slate-700 font-medium">${I.bullet()}<span class="min-w-0">${esc(a)}</span></li>`).join("")}
              </ul>
            </div>` : ""}
        </div>
      </div>`
    : "";

  $("#detailView").innerHTML = `
    <div class="mx-auto max-w-3xl px-6 py-6">
      <div class="mb-4 flex items-center gap-2 flex-wrap text-[11px] text-slate-500">
        <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-bold text-slate-600">${esc(e.category)}</span>
        <span class="font-mono">${esc(e.senderName)}</span>
        <span>·</span>
        <span>${esc(e.senderEmail)}</span>
        <span>·</span>
        <span>${fmtTime(e.timestamp)}</span>
        <span class="ml-auto rounded bg-indigo-50 px-1.5 py-0.5 text-[10px] font-bold text-indigo-700">${esc(e.domain)}</span>
      </div>
      <h2 class="text-xl font-bold text-slate-900 mb-4">${esc(e.subject)}</h2>
      ${e._loading ? `<div class="space-y-2 mb-4"><div class="shimmer h-4 w-full"></div><div class="shimmer h-4 w-5/6"></div></div>` : ""}
      ${e._loadError ? `<p class="text-xs text-slate-500 mb-4">Failed to load full message: ${esc(e._loadError)}</p>` : ""}
      ${aiBox}
      <div class="rounded-lg border border-slate-200 bg-white px-4 py-3">
        <p class="text-[11px] font-bold uppercase tracking-wider text-slate-400 mb-2">Body</p>
        ${e.body
          ? `<p class="text-[13px] text-slate-700 leading-relaxed whitespace-pre-wrap">${esc(e.body)}</p>`
          : `<p class="text-[13px] text-slate-400 italic">(no body available — metadata only)</p>`}
      </div>
    </div>`;
}

/* ───────────────────────────── Render: top-level ───── */
function renderAll() {
  renderCategoryChips();
  renderDomainList();
  renderDetail();
}

function setAllDomains(expand) {
  const domains = new Set(visibleEmails().map((e) => e.domain));
  state.expandedDomains.clear();
  if (expand) domains.forEach((d) => state.expandedDomains.add(d));
  renderDomainList();
}

/* ───────────────────────────── Toast ───── */
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2200);
}

/* ───────────────────────────── Data loading ───── */
async function loadData() {
  state.loading = true;
  state.error = null;
  renderAll();
  try {
    // Categories (independent of auth).
    let categories = [];
    try { categories = await api("/api/categories"); } catch (e) { /* ignore */ }
    state.categories = [...new Set(categories.map(String).filter(Boolean))];

    // 1) Try the local DB cache first — no OAuth required.
    let items = [];
    let source = "local DB";
    try {
      const data = await api("/api/messages?limit=500");
      items = data.items || [];
    } catch (err) {
      // 2) Fall back to the authenticated queue endpoint.
      try {
        const data = await api("/api/queue?max_results=200");
        items = data.items || [];
        source = "Gmail queue";
      } catch (err2) {
        state.error = err2.message;
      }
    }

    if (!items.length) {
      // 3) Last resort: bundled mock dataset so the viewer is demoable.
      state.emails = EMAILS_MOCK.map((e) => ({ ...e }));
      state.domainSummaries = { ...DOMAIN_SUMMARIES_MOCK };
      state.error = state.error
        ? `${state.error} · showing bundled mock data`
        : "No cached messages — showing bundled mock data";
    } else {
      state.emails = items.map((it) => normalizeItem(it, state.categories));
      state.domainSummaries = buildDomainSummaries();
      console.log(`[email-viewer] loaded ${state.emails.length} emails from ${source}`);
    }

    // default: expand every domain so the domain-level summary is visible
    state.expandedDomains = new Set(state.emails.map((e) => e.domain));
    state.loading = false;
    renderAll();
  } catch (err) {
    state.loading = false;
    state.error = err.message;
    renderAll();
  }
}

/* ───────────────────────────── Init ───── */
function init() {
  $("#selectAllBtn").addEventListener("click", () => {
    const allExpanded = visibleEmails().every((e) => state.expandedDomains.has(e.domain));
    setAllDomains(!allExpanded);
    $("#selectAllBtn").textContent = allExpanded ? "Expand all" : "Collapse all";
  });
  const observer = new MutationObserver(() => {
    const allExpanded = visibleEmails().every((e) => state.expandedDomains.has(e.domain));
    $("#selectAllBtn").textContent = allExpanded ? "Collapse all" : "Expand all";
  });
  observer.observe(document.getElementById("domainList"), { childList: true, subtree: true });

  loadData();
}

document.addEventListener("DOMContentLoaded", init);
