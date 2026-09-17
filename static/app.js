"use strict";

/* ───────────────────────────── State ───────────────────────────── */
const BATCH = 200;

const state = {
  authenticated: false,
  email: null,
  queue: [],            // queue items (metadata)
  index: 0,
  originalTotal: 0,
  detail: new Map(),    // id -> full message (body + summary)
  fetching: new Set(),  // ids currently being fetched
  history: [],          // [{action, ids, items, label, at}]
  busy: false,
  renderedId: null,
  groupsSig: null,       // signature of current sender groups (for re-render)
  expandedGroups: new Set(),
  groupCache: new Map(), // sender key -> {summaries:[...]}
  groupFetching: new Set(),
  cacheCount: 0,
  skippedIds: new Set(),  // ids hidden "for now" (persisted server-side)
  skippedItems: [],       // metadata for those hidden emails
  todoIds: new Set(),     // ids parked on the TODO page (never deletable)
  pendingTodoId: null,    // email awaiting due-date confirmation in the modal
  pendingRecatId: null,   // email awaiting category choice in the modal
  recatCats: [],          // allowed categories cache for the picker
  searchQuery: "",        // active search text ("" = no search)
  searchResults: null,    // null = group view; array = search results view
  viewMode: (() => { try { return localStorage.getItem("gmailer-view-mode") || "senders"; } catch (e) { return "senders"; } })(),
  activeCatGroup: null,   // selected category in categories view mode
  activeThreadId: null,   // selected thread in threads view mode
  nextPageToken: null,    // Gmail pagination token for "load next batch"
  activeGroupKey: null,    // "singles" | bundle_key | null
  expandedCardId: null,    // currently inline-expanded email id
  categories: [],          // allowed category names from /api/categories
  pendingOps: [],          // [{id, action, groupKey, label, count, status, ts}] background bulk ops
  visibleEmails: [],       // emails currently shown in the middle pane (group view)
  autoSelectedOnce: false, // pick a default group only on the very first queue load
  rules: [],               // [{id, name, enabled, action, scope, parsed, precedence, skill_md}]
  proposals: [],           // [{id, label, summary, rationale, downside, evidence_json, proposed_skill_md, status, observation_id}]
  wikiObservations: [],    // [{id, kind, target, summary, evidence_count, signal, status}]
  rulesWikiCollapsed: false,
  editorOpen: false,       // editor view active in main pane
  editorRuleId: null,      // rule being edited (null = new rule)
  editorMarkdown: "",      // current editor content
  editorApplyNow: false,
  pendingSummCount: 0,     // emails currently being summarized in the background
};

/* ───────────────────────────── Elements ────────────────────────── */
const $ = (id) => document.getElementById(id);

const el = {
  authScreen: $("authScreen"), authBtn: $("authBtn"), authMissingCreds: $("authMissingCreds"), authError: $("authError"), authErrorText: $("authErrorText"),
  loadingScreen: $("loadingScreen"), loadingText: $("loadingText"),
  app: $("app"),
  remainCount: $("remainCount"), remainSub: $("remainSub"),
  progressFill: $("progressFill"), queueMeta: $("queueMeta"),
  historyList: $("historyList"),
  groupList: $("groupList"), groupsCount: $("groupsCount"), viewSendersBtn: $("viewSendersBtn"), viewCatsBtn: $("viewCatsBtn"), viewThreadsBtn: $("viewThreadsBtn"),
  cacheInfo: $("cacheInfo"),
  skippedBtn: $("skippedBtn"), skippedCount: $("skippedCount"), skippedRestoreBtn: $("skippedRestoreBtn"),
  skipForNowBtn: $("skipForNowBtn"),

  loadMoreBtn: $("loadMoreBtn"),
  userChip: $("userChip"), logoutBtn: $("logoutBtn"),
  themeBtn: $("themeBtn"), themeIconMoon: $("themeIconMoon"), themeIconSun: $("themeIconSun"),
  position: $("position"), positionTotal: $("positionTotal"),
  undoTopBtn: $("undoTopBtn"), reloadBtn: $("reloadBtn"), clearCacheBtn: $("clearCacheBtn"), todoCount: $("todoCount"),   searchInput: $("searchInput"),
  summProgress: $("summProgress"),
  todoModal: $("todoModal"), todoModalSub: $("todoModalSub"), todoDueInput: $("todoDueInput"), todoModalCancel: $("todoModalCancel"), todoModalSave: $("todoModalSave"),
  recatModal: $("recatModal"), recatModalSub: $("recatModalSub"), recatSelect: $("recatSelect"), recatReason: $("recatReason"), recatModalCancel: $("recatModalCancel"), recatModalAuto: $("recatModalAuto"), recatModalSave: $("recatModalSave"),
  toasts: $("toasts"),
  emptyScreen: $("emptyScreen"), emptyStat: $("emptyStat"), emptyReload: $("emptyReload"),
  groupHeader: $("groupHeader"), groupTitle: $("groupTitle"), groupCount: $("groupCount"), groupOverview: $("groupOverview"), bulkDeleteBtn: $("bulkDeleteBtn"), bulkArchiveBtn: $("bulkArchiveBtn"), emailCards: $("emailCards"), remapCatsBtn: $("remapCatsBtn"),
  normalView: $("normalView"), editorView: $("editorView"), editorTitle: $("editorTitle"), editorContent: $("editorContent"), editorErrors: $("editorErrors"), editorSave: $("editorSave"), editorCancel: $("editorCancel"), editorDelete: $("editorDelete"), editorApplyNow: $("editorApplyNow"),
};

/* ───────────────────────────── Utils ───────────────────────────── */
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function timeAgo(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
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

function linkify(text) {
  return esc(text).replace(
    /((?:https?:\/\/|www\.)[^\s<>"']+)/gi,
    (m) => {
      const href = /^https?:/i.test(m) ? m : "https://" + m;
      return `<a href="${href}" target="_blank" rel="noopener noreferrer">${m}</a>`;
    }
  );
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (res.status === 401) {
    showAuth();
    throw new Error("Session expired — sign in again.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail ?? data;
    const msg = typeof detail === "string" ? detail
      : detail && detail.errors ? detail.errors.map((e) => `line ${e.line}: ${e.msg}`).join("\n")
      : res.statusText || "Request failed";
    const err = new Error(msg);
    err.detail = detail;
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ───────────────────────────── Toasts ──────────────────────────── */
function toast(msg, tone = "info", opts = {}) {
  const colors = {
    info: "bg-white border-slate-300 text-slate-700 dark:bg-slate-800 dark:border-slate-700 dark:text-slate-100",
    err: "bg-red-50 border-red-300 text-red-700 dark:bg-red-950 dark:border-red-600/60 dark:text-red-100",
    ok: "bg-emerald-50 border-emerald-300 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-600/60 dark:text-emerald-100",
  };
  const t = document.createElement("div");
  t.className = `toast pointer-events-auto flex items-center gap-3 rounded-xl border px-4 py-2.5 text-sm shadow-xl ${colors[tone] || colors.info}`;
  t.innerHTML = `<span>${esc(msg)}</span>`;
  if (opts.undo) {
    const b = document.createElement("button");
    b.className = "rounded-lg bg-slate-200 text-slate-800 hover:bg-slate-300 dark:bg-slate-700/70 dark:text-white dark:hover:bg-slate-600 px-2.5 py-1 text-xs font-bold transition";
    b.textContent = "Undo";
    b.onclick = () => { (opts.undoFn || undoLast)(); dismiss(); };
    t.appendChild(b);
  }
  if (opts.duration !== 0) {
    const ms = opts.duration ?? 2600;
    setTimeout(() => { t.style.transition = "opacity .3s"; t.style.opacity = "0"; setTimeout(dismiss, 300); }, ms);
  }
  function dismiss() { t.remove(); }
  el.toasts.appendChild(t);
}

let toastHideTimer = null;
function clearToasts() {
  el.toasts.innerHTML = "";
}

/* ───────────────────────────── Theme ───────────────────────────── */
const THEMES = ["dark", "light"];

function currentTheme() {
  if (document.documentElement.classList.contains("dark")) return "dark";
  return "light";
}

function applyThemeUI() {
  const t = currentTheme();
  el.themeIconMoon.classList.toggle("hidden", t !== "dark");
  el.themeIconSun.classList.toggle("hidden", t !== "light");
  el.themeBtn.title = t === "dark" ? "Theme: dark — click for light" : "Theme: light — click for dark";
}

function toggleTheme() {
  const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
  document.documentElement.classList.toggle("dark", next === "dark");
  try { localStorage.setItem("gmailer-theme", next); } catch (e) {}
  applyThemeUI();
}

/* ───────────────────────────── Screens ─────────────────────────── */
function showAuth(status) {
  el.loadingScreen.classList.add("hidden");
  el.app.classList.add("hidden");
  el.emptyScreen.classList.add("hidden");
  if (status && !status.credentials_found) {
    el.authMissingCreds.classList.remove("hidden");
    el.authBtn.classList.add("hidden");
  } else {
    el.authBtn.classList.remove("hidden");
  }
  el.authError.classList.add("hidden");
  const p = new URLSearchParams(window.location.search);
  const authErr = p.get("auth_error") || p.get("error");
  if (authErr) {
    el.authErrorText.textContent = decodeURIComponent(authErr);
    el.authError.classList.remove("hidden");
    history.replaceState({}, "", window.location.pathname);
  }
  el.authScreen.classList.remove("hidden");
  el.authScreen.classList.add("flex");
}

async function showLoading(msg) {
  el.authScreen.classList.add("hidden");
  el.authScreen.classList.remove("flex");
  el.app.classList.add("hidden");
  el.emptyScreen.classList.add("hidden");
  el.loadingText.textContent = msg || "Loading your inbox…";
  el.loadingScreen.classList.remove("hidden");
  el.loadingScreen.classList.add("flex");
}

function showEmpty() {
  el.emptyStat.textContent = `Cleared ${state.originalTotal} email${state.originalTotal === 1 ? "" : "s"} in this batch.`;
  const n = state.skippedItems.length;
  if (n) {
    el.skippedRestoreBtn.textContent = `Bring back ${n} skipped email${n === 1 ? "" : "s"}`;
    el.skippedRestoreBtn.classList.remove("hidden");
  } else {
    el.skippedRestoreBtn.classList.add("hidden");
  }
  el.app.classList.add("hidden");
  el.emptyScreen.classList.remove("hidden");
}

function showMain() {
  el.emptyScreen.classList.add("hidden");
  el.loadingScreen.classList.add("hidden");
  el.loadingScreen.classList.remove("flex");
  el.authScreen.classList.add("hidden");
  el.authScreen.classList.remove("flex");
  el.app.classList.remove("hidden");
}

/* ───────────────────────────── Queue load ──────────────────────── */
async function init() {
  try {
    const status = await api("/api/status");
    if (!status.authenticated) { showAuth(status); return; }
    state.authenticated = true;
    state.email = status.email;
    el.userChip.textContent = state.email || "";
    await loadQueue();
  } catch (e) {
    showAuth({ credentials_found: true });
  }
}

async function loadQueue() {
  state.searchQuery = "";
  state.searchResults = null;
  if (el.searchInput) el.searchInput.value = "";
  await showLoading("Pulling latest unread inbox…");
  try {
    const [data, skippedRes, rulesRes, proposalsRes, wikiRes, todosRes] = await Promise.all([
      api(`/api/queue?max_results=${BATCH}`),
      api("/api/skipped").catch(() => ({ items: [] })),
      api("/api/rules").catch(() => ({ items: [] })),
      api("/api/proposals").catch(() => ({ items: [] })),
      api("/api/wiki/observations").catch(() => ({ items: [] })),
      api("/api/todos").catch(() => ({ items: [] })),
    ]);
    const skipped = skippedRes.items || [];
    state.skippedIds = new Set(skipped.map((i) => i.id));
    state.skippedItems = skipped;
    state.todoIds = new Set((todosRes.items || []).map((i) => i.id));
    const keep = [];
    (data.items || []).forEach((it) => {
      if (state.skippedIds.has(it.id)) state.skippedItems.push(it);
      else keep.push(it);
    });
    state.queue = keep;
    state.originalTotal = state.queue.length;
    state.index = 0;
    state.detail.clear();
    state.fetching.clear();
    state.cacheCount = data.cache_count || 0;
    state.nextPageToken = data.next_page_token || null;
    state.rules = rulesRes.items || [];
    state.proposals = proposalsRes.items || [];
    state.wikiObservations = wikiRes.items || [];
    showMain();
    if (state.queue.length === 0) { showEmpty(); return; }
    await renderAll(true);
    if (data.auto_trashed || data.auto_starred || data.auto_skipped) {
      const counts = [];
      if (data.auto_trashed) counts.push(`${data.auto_trashed} trashed`);
      if (data.auto_starred) counts.push(`${data.auto_starred} starred`);
      if (data.auto_skipped) counts.push(`${data.auto_skipped} skipped`);
      toast(`Rules auto-applied: ${counts.join(", ")}`, "info", { duration: 3500 });
    }
  } catch (e) {
    el.loadingText.textContent = `Failed to load queue: ${e.message}`;
    setTimeout(loadQueue, 2500);
  }
}

async function loadMore() {
  if (state.busy) return;
  if (!state.nextPageToken) { toast("No more unread emails in the inbox", "info", { duration: 1800 }); return; }
  state.searchQuery = "";
  state.searchResults = null;
  if (el.searchInput) el.searchInput.value = "";
  state.busy = true;
  await showLoading("Pulling the next batch…");
  try {
    const data = await api(`/api/queue?max_results=${BATCH}&page_token=${encodeURIComponent(state.nextPageToken)}`);
    const added = (data.items || []).filter((it) =>
      !state.skippedIds.has(it.id) && !state.queue.some((q) => q.id === it.id)
    );
    state.queue.push(...added);
    state.originalTotal += added.length;
    state.nextPageToken = data.next_page_token || null;
    state.cacheCount = data.cache_count || 0;
    showMain();
    renderSidebar();
    await refreshRulesData();
    if (state.queue.length === 0) { showEmpty(); return; }
    renderGroupView();
    toast(`Loaded ${added.length} more · in queue: ${state.queue.length}`, "ok", { duration: 2000 });
    if (data.auto_trashed || data.auto_starred || data.auto_skipped) {
      const counts = [];
      if (data.auto_trashed) counts.push(`${data.auto_trashed} trashed`);
      if (data.auto_starred) counts.push(`${data.auto_starred} starred`);
      if (data.auto_skipped) counts.push(`${data.auto_skipped} skipped`);
      toast(`Rules auto-applied: ${counts.join(", ")}`, "info", { duration: 2600 });
    }
  } catch (e) {
    el.loadingText.textContent = `Failed to load more: ${e.message}`;
    setTimeout(loadMore, 2500);
  } finally {
    state.busy = false;
  }
}

/* ───────────────────────────── Detail + prefetch ───────────────── */
async function fetchDetail(id) {
  if (state.detail.has(id) || state.fetching.has(id)) return state.detail.get(id);
  state.fetching.add(id);
  try {
    const d = await api(`/api/messages/${id}`);
    state.detail.set(id, d);
    pumpDetail(d);
    return d;
  } catch (e) {
    console.warn("detail fetch failed", id, e.message);
    return null;
  } finally {
    state.fetching.delete(id);
  }
}

async function regenerateSummary(id) {
  const it = state.queue.find((x) => x.id === id);
  if (!it) return;
  toast("Regenerating AI summary…", "info", { duration: 4000 });
  try {
    const res = await api(`/api/messages/${id}/summarize`, { method: "POST" });
    if (res.summary) {
      it.summary = res.summary;
      state.detail.set(id, { ...state.detail.get(id), summary: res.summary });
      state.expandedCardId = it.id;
      renderSidebar();
      renderGroupView();
      setTimeout(refreshMainView, 100);
      toast("Summary regenerated ✓", "ok", { duration: 2000 });
    }
  } catch (e) {
    toast(`Regenerate failed: ${e.message}`, "err", { duration: 3000 });
  }
}

function pumpDetail(d) {
  if (!d || !d.id) return;
  const it = state.queue.find((x) => x.id === d.id);
  if (it && d.summary) {
    it.summary = {
      ...(it.summary || {}),
      ...d.summary,
    };
    if (d.preview) it.preview = d.preview;
  }
  const visibleIds = new Set((state.visibleEmails || []).map((x) => x.id));
  if (state.expandedCardId === d.id || visibleIds.has(d.id)) refreshMainView();
}

/* Summarize ungrouped ("singles") emails in the background so their
   cards populate without needing a manual expand. Bounded concurrency. */
let singleSummQueue = [];
let singleSummInFlight = 0;
let singleSummTimer = null;
const SINGLE_SUMM_CONCURRENCY = 6;

function scheduleSingleSummarize(ids) {
  for (const id of ids) {
    if (state.detail.has(id) || state.fetching.has(id) || singleSummQueue.includes(id)) continue;
    const it = state.queue.find((x) => x.id === id);
    if (it && it.summary && it.summary.one_liner) continue;
    singleSummQueue.push(id);
  }
  if (singleSummQueue.length > 200) singleSummQueue.length = 200;
  if (ids.length) {
    state.pendingSummCount = Math.max(state.pendingSummCount, singleSummQueue.length);
    renderSummarizeProgress();
  }
  if (!singleSummTimer) {
    singleSummTimer = setTimeout(bumpSingleSummarize, 400);
  }
}

function renderSummarizeProgress() {
  if (!el.summProgress) return;
  const remaining = singleSummQueue.length + singleSummInFlight;
  if (remaining > 0 && remaining <= state.pendingSummCount) {
    el.summProgress.textContent = `Summarizing ${state.pendingSummCount - remaining + singleSummInFlight}/${state.pendingSummCount}…`;
    el.summProgress.classList.remove("hidden");
  } else if (remaining === 0) {
    el.summProgress.classList.add("hidden");
    state.pendingSummCount = 0;
  }
}

function bumpSingleSummarize() {
  singleSummTimer = null;
  while (singleSummQueue.length && singleSummInFlight < SINGLE_SUMM_CONCURRENCY) {
    const id = singleSummQueue.shift();
    const it = state.queue.find((x) => x.id === id);
    if (!it || (it.summary && it.summary.one_liner)) continue;
    singleSummInFlight++;
    fetchDetail(id).finally(() => {
      singleSummInFlight--;
      renderSummarizeProgress();
      bumpSingleSummarize();
    });
  }
  renderSummarizeProgress();
}

/* ───────────────────────────── Rendering ───────────────────────── */
async function renderAll(initial = false) {
  if (initial && !state.autoSelectedOnce) {
    state.autoSelectedOnce = true;
    if (state.viewMode === "categories") {
      const top = topCategory();
      if (top) { selectCategoryGroup(top); return; }
    }
    if (state.viewMode === "threads") {
      const ts = threads();
      if (ts.length) { selectThread(ts[0].tid); return; }
    }
    const picked = pickDefaultGroup();
    if (picked) { selectGroup(picked); return; }
  }
  renderSidebar();
  if (state.searchResults) renderGroupView();
  else if (state.viewMode === "categories") renderCategoryGroupView();
  else if (state.viewMode === "threads") renderThreadView();
  else renderGroupView();
}

function setViewMode(mode) {
  state.viewMode = mode === "categories" ? "categories" : mode === "threads" ? "threads" : "senders";
  try { localStorage.setItem("gmailer-view-mode", state.viewMode); } catch (e) {}
  state.expandedCardId = null;
  if (state.viewMode === "categories" && !state.activeCatGroup) {
    state.activeCatGroup = topCategory();
  }
  if (state.viewMode === "threads" && !state.activeThreadId) {
    const ts = threads();
    if (ts.length) state.activeThreadId = ts[0].tid;
  }
  renderSidebar();
  if (state.viewMode === "categories") renderCategoryGroupView();
  else if (state.viewMode === "threads") renderThreadView();
  else renderGroupView();
}

function topCategory() {
  const counts = categoryCounts();
  return counts.length ? counts[0][0] : null;
}

/* ───────────────────────────── Sidebar ─────────────────────────── */
function renderSidebar() {
  const remaining = state.queue.length;
  el.remainCount.textContent = remaining;
  el.remainSub.textContent = remaining === 1 ? "email left" : "emails left";
  const pct = state.originalTotal ? Math.round(((state.originalTotal - remaining) / state.originalTotal) * 100) : 0;
  el.progressFill.style.width = `${pct}%`;
  el.queueMeta.textContent = `${pct}% cleared · ${state.originalTotal} in batch`;

  const _keys = groupKeys();
  const _idx = _keys.indexOf(state.activeGroupKey);
  if (state.searchResults) {
    el.position.textContent = "Search";
    el.positionTotal.textContent = `${state.searchResults.length} matches`;
  } else if (state.viewMode === "categories") {
    el.position.textContent = state.activeCatGroup ? `Category: ${state.activeCatGroup}` : "No category selected";
    el.positionTotal.textContent = `${state.visibleEmails.length} shown`;
  } else if (state.viewMode === "threads") {
    el.position.textContent = state.activeThreadId ? "Thread" : "No thread selected";
    el.positionTotal.textContent = `${state.visibleEmails.length} shown`;
  } else if (state.activeGroupKey === null) {
    el.position.textContent = "No group selected";
    el.positionTotal.textContent = `${state.queue.length} in batch`;
  } else if (_idx === -1) {
    el.position.textContent = "Group";
    el.positionTotal.textContent = `${state.visibleEmails.length} shown`;
  } else {
    el.position.textContent = `Group ${_idx + 1}/${_keys.length}`;
    el.positionTotal.textContent = `${state.visibleEmails.length} shown`;
  }

  el.cacheInfo.textContent = state.cacheCount
    ? `local cache: ${state.cacheCount} email${state.cacheCount === 1 ? "" : "s"} on disk`
    : "local cache: empty";

  const nSkip = state.skippedItems.length;
  if (nSkip) {
    el.skippedCount.textContent = nSkip;
    el.skippedBtn.classList.remove("hidden");
  } else {
    el.skippedBtn.classList.add("hidden");
  }

  el.loadMoreBtn.classList.toggle("hidden", !state.nextPageToken);

  if (el.todoCount) el.todoCount.textContent = state.todoIds.size ? `(${state.todoIds.size})` : "";

  renderPendingOps();
  renderGroups();
}

function renderHistory() {
  renderPendingOps();
}

function opLine(o) {
  const icon = o.status === "running" ? "🕐" : o.status === "done" ? "✓" : "✕";
  const tone = o.status === "running"
    ? "text-violet-700 dark:text-violet-300"
    : o.status === "done"
      ? "text-emerald-600 dark:text-emerald-400"
      : "text-red-600 dark:text-red-400";
  const runningCls = o.status === "running" ? "border-violet-500/40 bg-violet-500/10" : "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900/60";
  const dim = o.status === "done" || o.status === "failed" ? " opacity-70" : "";
  return `
    <div class="rounded-lg border px-3 py-1.5 flex items-center gap-2 ${runningCls}${dim}">
      <span class="text-xs ${tone} flex items-center gap-1.5 min-w-0">
        <span class="shrink-0">${icon}</span>
        <span class="truncate">${esc(o.label)}</span>
      </span>
      ${o.status === "running" ? `<span class="ml-auto shrink-0 text-[9px] text-slate-400 italic">in progress…</span>` : ""}
    </div>`;
}

function renderPendingOps() {
  const pendingHTML = state.pendingOps.map(opLine).join("");
  const historyHTML = state.history.slice(0, 3).map((h, i) => `
    <div class="rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900/60 px-3 py-2 flex items-center gap-2">
      <span class="text-xs text-slate-600 dark:text-slate-400 flex-1 truncate">${esc(h.label)}</span>
      <button data-undo="${i}" class="rounded bg-violet-500/15 px-2 py-1 text-[10px] font-bold text-violet-700 dark:text-violet-300 hover:bg-violet-500/30 transition">Undo</button>
    </div>
  `).join("");
  el.historyList.innerHTML = (pendingHTML + historyHTML)
    || `<p class="text-xs text-slate-500 dark:text-slate-600">No actions yet. D to delete, E to archive.</p>`;

  el.historyList.querySelectorAll("[data-undo]").forEach((btn) => {
    btn.addEventListener("click", () => undoIndex(parseInt(btn.dataset.undo, 10)));
  });
}

function scheduleOpRemoval(op) {
  setTimeout(() => {
    const i = state.pendingOps.findIndex((o) => o.id === op.id);
    if (i !== -1) state.pendingOps.splice(i, 1);
    renderPendingOps();
  }, 8000);
}

/* ───────────────────────────── Category remap ──────────────────── */
async function remapCategories() {
  if (!el.remapCatsBtn || el.remapCatsBtn.disabled) return;
  el.remapCatsBtn.disabled = true;
  el.remapCatsBtn.classList.add("opacity-50");
  toast("Re-evaluating categories…", "info", { duration: 2000 });
  try {
    const res = await api("/api/categories/remap", { method: "POST", body: "{}" });
    toast(`Remapped ${res.scanned} emails · ${res.changed} changed${res.locked ? ` · ${res.locked} locked kept` : ""}`, "ok", { duration: 3000 });
  } catch (e) {
    toast(`Remap failed: ${e.message}`, "err", { duration: 4000 });
  } finally {
    el.remapCatsBtn.disabled = false;
    el.remapCatsBtn.classList.remove("opacity-50");
  }
  await loadQueue();
}

async function remapOne(id) {
  if (!id) return;
  try {
    const res = await api("/api/categories/remap-one", { method: "POST", body: JSON.stringify({ id }) });
    if (res.changed) toast(`${res.old || "?"} → ${res.category}`, "ok", { duration: 2200 });
    else toast(`Already ${res.category || "categorized"} — no change`, "info", { duration: 1800 });
    applyCategoryLocally(id, res.category, false);
    refreshMainView();
  } catch (e) {
    toast(`Re-evaluate failed: ${e.message}`, "err", { duration: 3000 });
  }
}

async function openRecatModal(id) {
  const item = currentItemOf(id);
  if (!item) { toast("That email is no longer in the queue", "err", { duration: 2000 }); return; }
  if (!state.recatCats.length) {
    try {
      state.recatCats = (await api("/api/categories")).items || [];
    } catch (e) {
      toast(`Could not load categories: ${e.message}`, "err", { duration: 3000 });
      return;
    }
  }
  const current = emailCategory(item);
  state.pendingRecatId = id;
  el.recatModalSub.textContent = item.subject || "(no subject)";
  el.recatReason.value = "";
  el.recatSelect.innerHTML = state.recatCats.map((c) =>
    `<option value="${esc(c)}"${c === current ? " selected" : ""}>${esc(c)}${c === current ? " (current)" : ""}</option>`).join("");
  el.recatModal.classList.remove("hidden");
  el.recatModal.classList.add("flex");
  setTimeout(() => el.recatSelect.focus(), 50);
}

function closeRecatModal() {
  state.pendingRecatId = null;
  el.recatModal.classList.add("hidden");
  el.recatModal.classList.remove("flex");
}

function applyCategoryLocally(id, category, locked = false) {
  for (const list of [state.queue, state.searchResults || []]) {
    const it = (list || []).find((x) => x && x.id === id);
    if (it) {
      it.category = category;
      it.category_locked = locked;
      if (it.summary) it.summary.category = category;
    }
  }
  const d = state.detail.get(id);
  if (d) {
    d.category = category;
    d.category_locked = locked;
    if (d.summary) d.summary.category = category;
  }
}

async function confirmRecatModal() {
  const id = state.pendingRecatId;
  if (!id) { closeRecatModal(); return; }
  try {
    const row = await api("/api/categories/set", {
      method: "POST",
      body: JSON.stringify({ id, category: el.recatSelect.value, subject_contains: el.recatReason.value }),
    });
    closeRecatModal();
    applyCategoryLocally(id, row.category, true);
    const lm = row.learned_mapping;
    const learned = lm
      ? ` · future mail from ${lm.pattern}${lm.subject_contains ? ` with “${lm.subject_contains}”` : ""} auto-maps here` : "";
    toast(`Set to ${row.category} (locked)${learned}`, "ok", { duration: 3200 });
    refreshMainView();
  } catch (e) {
    toast(`Set failed: ${e.message}`, "err", { duration: 3000 });
  }
}

/* ───────────────────────────── Groups ──────────────────────────── */
function currentGroups() {
  const seen = new Map();
  for (const it of state.queue) {
    if (it.bundle_count <= 1) continue;
    const key = it.bundle_key;
    if (!seen.has(key)) {
      seen.set(key, {
        key,
        label: it.bundle_sender_name || it.sender_name || it.bundle_key,
        ids: [],
      });
    }
    seen.get(key).ids.push(it.id);
  }
  return [...seen.values()].sort((a, b) => b.ids.length - a.ids.length);
}

function groupKeys() {
  const keys = [];
  if (state.queue.some((i) => (i.bundle_count || 1) <= 1)) keys.push("singles");
  for (const g of currentGroups()) keys.push(g.key);
  return keys;
}

function pickDefaultGroup() {
  if (state.queue.some((i) => (i.bundle_count || 1) <= 1)) return "singles";
  const g = currentGroups()[0];
  return g ? g.key : null;
}

function categoryCounts() {
  const m = new Map();
  for (const it of state.queue) {
    const c = emailCategory(it) || "Uncategorized";
    m.set(c, (m.get(c) || 0) + 1);
  }
  return [...m.entries()].sort((a, b) => b[1] - a[1]);
}

function paintViewToggle() {
  const on = "bg-violet-500/20 text-violet-700 dark:text-violet-300";
  const off = "text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200";
  const cls = (active) => `rounded-md px-2 py-1 text-[10px] font-bold transition ${active ? on : off}`;
  if (el.viewSendersBtn) el.viewSendersBtn.className = cls(state.viewMode === "senders");
  if (el.viewCatsBtn) el.viewCatsBtn.className = cls(state.viewMode === "categories");
  if (el.viewThreadsBtn) el.viewThreadsBtn.className = cls(state.viewMode === "threads");
}

function threads() {
  const map = new Map();
  for (const it of state.queue) {
    const tid = it.thread_id || it.id;
    if (!map.has(tid)) map.set(tid, []);
    map.get(tid).push(it);
  }
  const list = [...map.entries()].map(([tid, items]) => {
    items.sort((a, b) => (a.internal_date_ms || 0) - (b.internal_date_ms || 0));
    const latest = items[items.length - 1];
    const names = [...new Set(items.map((x) => x.sender_name || x.sender_email || "?"))];
    return {
      tid, items, latest, names,
      lastMs: latest.internal_date_ms || 0,
      subject: latest.subject || "(no subject)",
    };
  });
  list.sort((a, b) => b.lastMs - a.lastMs);
  return list;
}

function renderThreadRows() {
  const ts = threads();
  el.groupsCount.textContent = ts.length
    ? `${ts.length} thread${ts.length === 1 ? "" : "s"}` : "";
  state.groupsSig = JSON.stringify(ts.map((t) => [t.tid, t.items.map((i) => i.id).join(",")]));
  el.groupList.innerHTML = ts.length ? ts.map((t) => {
    const active = state.activeThreadId === t.tid;
    const when = t.lastMs ? timeAgo(new Date(t.lastMs).toISOString()) : "";
    return `
    <li class="rounded-lg border ${active ? "border-violet-500/60 bg-violet-500/10" : "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900/60"} px-3 py-2 cursor-pointer select-none transition hover:bg-slate-100 dark:hover:bg-slate-800/70" data-thread="${esc(t.tid)}">
      <div class="flex items-center gap-2">
        <p class="min-w-0 flex-1 ${active ? "text-violet-800 dark:text-violet-200" : "text-slate-800 dark:text-slate-200"}">
          <span class="block truncate text-[13px] font-semibold">${esc(t.subject)}</span>
          <span class="block truncate text-[10px] text-slate-500 dark:text-slate-400">${esc(t.names.slice(0, 3).join(", "))}${t.names.length > 3 ? ` +${t.names.length - 3} more` : ""}</span>
        </p>
        <span class="shrink-0 rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-bold text-violet-700 dark:text-violet-300">${t.items.length}</span>
      </div>
      <div class="mt-0.5 flex items-center gap-2">
        <p class="min-w-0 flex-1 truncate text-[11px] text-slate-500 dark:text-slate-500">${esc(t.latest.snippet || t.latest.preview || "")}</p>
        ${when ? `<span class="shrink-0 text-[10px] text-slate-400 dark:text-slate-600">${esc(when)}</span>` : ""}
      </div>
    </li>`;
  }).join("") : `<li class="text-xs text-slate-500 dark:text-slate-600">No threads yet.</li>`;
}

function selectThread(tid) {
  state.activeThreadId = tid;
  state.expandedCardId = null;
  renderSidebar();
  renderThreadView();
}

function renderThreadView() {
  const t = threads().find((x) => x.tid === state.activeThreadId);
  el.groupHeader.classList.toggle("hidden", !t);
  if (!t) {
    state.visibleEmails = [];
    el.emailCards.innerHTML = `<p class="text-sm text-slate-500 dark:text-slate-600 p-6">Pick a thread on the left.</p>`;
    return;
  }
  state.visibleEmails = t.items;
  el.groupTitle.textContent = t.subject;
  el.groupCount.textContent = `${t.items.length} message${t.items.length === 1 ? "" : "s"}`;
  el.groupOverview.textContent = t.names.join(" · ");
  el.bulkDeleteBtn.classList.add("hidden");
  el.bulkArchiveBtn.classList.add("hidden");
  el.emailCards.innerHTML = t.items.map(emailCard).join("");
}

function renderGroups() {
  paintViewToggle();
  if (state.viewMode === "categories") return renderCategoryRows();
  if (state.viewMode === "threads") return renderThreadRows();
  const groups = currentGroups();
  el.groupsCount.textContent = groups.length
    ? `${groups.length} sender${groups.length === 1 ? "" : "s"}` : "";
  state.groupsSig = JSON.stringify(groups.map((g) => [g.key, g.ids.join(",")]));

  const singleIds = state.queue
    .filter((i) => (i.bundle_count || 1) <= 1)
    .map((i) => i.id);
  let rows = "";
  if (singleIds.length) rows += groupRow({ key: "singles", label: "Singles", ids: singleIds });
  rows += groups.map(groupRow).join("");
  if (!rows) rows = `<li class="text-xs text-slate-500 dark:text-slate-600">No repeat senders yet.</li>`;
  el.groupList.innerHTML = rows;
}

function renderCategoryRows() {
  const counts = categoryCounts();
  el.groupsCount.textContent = counts.length
    ? `${counts.length} ${counts.length === 1 ? "category" : "categories"}` : "";
  state.groupsSig = JSON.stringify(counts);
  el.groupList.innerHTML = counts.length ? counts.map(([cat, n]) => {
    const active = state.activeCatGroup === cat;
    return `
    <li class="rounded-lg border ${active ? "border-violet-500/60 bg-violet-500/10" : "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900/60"} px-3 py-2 flex items-center gap-2 cursor-pointer select-none transition hover:bg-slate-100 dark:hover:bg-slate-800/70" data-catgroup="${esc(cat)}">
      <p class="min-w-0 flex-1 text-[13px] font-semibold ${active ? "text-violet-800 dark:text-violet-200" : "text-slate-800 dark:text-slate-200"} truncate">${esc(cat)}</p>
      <span class="shrink-0 rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-bold text-violet-700 dark:text-violet-300">${n}</span>
    </li>`;
  }).join("") : `<li class="text-xs text-slate-500 dark:text-slate-600">No categorized emails yet.</li>`;
}

function selectCategoryGroup(cat) {
  state.activeCatGroup = cat;
  state.expandedCardId = null;
  renderSidebar();
  renderCategoryGroupView();
}

function renderCategoryGroupView() {
  const cat = state.activeCatGroup;
  el.groupHeader.classList.toggle("hidden", !cat);
  if (!cat) {
    state.visibleEmails = [];
    el.emailCards.innerHTML = `<p class="text-sm text-slate-500 dark:text-slate-600 p-6">Pick a category on the left.</p>`;
    return;
  }
  const emails = state.queue.filter((it) => (emailCategory(it) || "Uncategorized") === cat);
  state.visibleEmails = emails;
  el.bulkDeleteBtn.classList.add("hidden");
  el.bulkArchiveBtn.classList.add("hidden");
  if (!emails.length) {
    el.groupTitle.textContent = cat;
    el.groupCount.textContent = "0 emails";
    el.groupOverview.textContent = `Every “${cat}” email in this batch.`;
    el.emailCards.innerHTML = `<p class="text-sm text-slate-500 dark:text-slate-600 p-6">No emails in this category right now.</p>`;
    return;
  }
  const senders = new Map();
  for (const it of emails) {
    const key = it.bundle_key || it.sender_email || it.sender_name || "unknown";
    if (!senders.has(key)) {
      senders.set(key, {
        label: it.bundle_sender_name || it.sender_name || it.sender_email || key,
        domain: ((it.sender_email || "").split("@")[1] || "").toLowerCase(),
        items: [],
      });
    }
    senders.get(key).items.push(it);
  }
  const groups = [...senders.values()].map((g) => {
    const tmap = new Map();
    for (const it of g.items) {
      const tid = it.thread_id || it.id;
      if (!tmap.has(tid)) tmap.set(tid, []);
      tmap.get(tid).push(it);
    }
    const threadList = [...tmap.values()].map((items) => {
      items.sort((a, b) => (a.internal_date_ms || 0) - (b.internal_date_ms || 0));
      return { items, lastMs: items[items.length - 1].internal_date_ms || 0 };
    }).sort((a, b) => b.lastMs - a.lastMs);
    return { ...g, threads: threadList, lastMs: Math.max(...threadList.map((t) => t.lastMs)) };
  }).sort((a, b) => b.lastMs - a.lastMs);

  el.groupTitle.textContent = cat;
  el.groupCount.textContent = `${emails.length} email${emails.length === 1 ? "" : "s"}`;
  el.groupOverview.textContent = `${groups.length} sender${groups.length === 1 ? "" : "s"} · newest first`;
  el.emailCards.innerHTML = groups.map((g) => `
    <div class="rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
      <div class="flex items-center gap-2 px-3 py-2 border-b border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-900/60">
        <span class="min-w-0 flex-1 truncate text-[13px] font-bold text-slate-800 dark:text-slate-200">${esc(g.label)}</span>
        ${g.domain ? `<span class="shrink-0 rounded-md bg-sky-500/15 px-1.5 py-px font-mono text-[10px] font-bold text-sky-700 dark:text-sky-300">@${esc(g.domain)}</span>` : ""}
        <span class="shrink-0 rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-bold text-violet-700 dark:text-violet-300">${g.items.length}</span>
        ${g.domain ? `<button data-domain-del="${esc(g.domain)}" class="shrink-0 rounded bg-red-500/15 px-1.5 py-0.5 text-[9px] font-bold text-red-700 dark:text-red-300 hover:bg-red-500/30 transition">Trash domain</button>` : ""}
      </div>
      <div class="p-2 space-y-2">
        ${g.threads.map((t) => `
          ${(t.items.length > 1 || g.threads.length > 1) ? `<div class="px-1 pt-1 text-[11px] font-semibold text-slate-500 dark:text-slate-400 truncate">${esc(t.items[t.items.length - 1].subject || "(no subject)")} · ${t.items.length} message${t.items.length === 1 ? "" : "s"}</div>` : ""}
          ${t.items.map(emailCard).join("")}
        `).join("")}
      </div>
    </div>`).join("");
}

function groupRow(g) {
  const active = state.activeGroupKey === g.key;
  let promo = "";
  if (g.key !== "singles") {
    const items = state.queue.filter((i) => g.ids.includes(i.id));
    const nPromo = items.filter((i) => i.promo).length;
    if (items.length && nPromo / items.length > 0.5) {
      promo = `<span class="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-bold text-amber-700 dark:text-amber-300">PROMO</span>`;
    }
  }
  let delBtn = "";
  if (g.key !== "singles") {
    delBtn = `<button data-gdel="${esc(g.key)}" class="shrink-0 rounded-lg bg-red-500/15 px-1.5 py-0.5 text-[9px] font-bold text-red-700 dark:text-red-300 hover:bg-red-500/30 transition" title="Delete all ${g.ids.length} from this sender">🗑</button>`;
  }
  return `
    <li class="group-row rounded-lg border ${active ? "border-violet-500/60 bg-violet-500/10" : "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900/60"} px-3 py-2 flex items-center gap-2 cursor-pointer select-none transition hover:bg-slate-100 dark:hover:bg-slate-800/70" data-group="${esc(g.key)}"${active ? ` data-active="yes"` : ""}>
      <p class="min-w-0 flex-1 text-[13px] font-semibold ${active ? "text-violet-800 dark:text-violet-200" : "text-slate-800 dark:text-slate-200"} truncate">${esc(g.label)}</p>
      ${promo}
      <span class="shrink-0 rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-bold text-violet-700 dark:text-violet-300">${g.ids.length}</span>
      ${delBtn}
    </li>`;
}

async function fetchGroup(key, g) {
  if (state.groupCache.has(key) || state.groupFetching.has(key)) return;
  state.groupFetching.add(key);
  if (!state.searchResults) renderGroupView();
  try {
    const res = await api(`/api/groups/${encodeURIComponent(key)}/summarize`, {
      method: "POST",
      body: JSON.stringify({ message_ids: g.ids, label: g.label }),
    });
    state.groupCache.set(key, res);
    mergeGroupSummaries(res);
  } catch (e) {
    toast(`Group summary failed: ${e.message}`, "err", { duration: 4000 });
  } finally {
    state.groupFetching.delete(key);
  }
  if (!state.searchResults) renderGroupView();
}

function mergeGroupSummaries(res) {
  const byId = new Map((res.summaries || []).map((s) => [s.id, s]));
  for (const it of state.queue) {
    const s = byId.get(it.id);
    if (!s) continue;
    if (it.summary && it.summary.one_liner) continue;
    it.summary = {
      one_liner: s.one_liner || "",
      bullets: Array.isArray(s.bullets) ? s.bullets : [],
      category: s.category || "Unclear",
      action_needed: s.action_needed || "nothing",
      money: s.money || "",
      is_politics: !!s.is_politics,
      opinion_bias: s.opinion_bias || "neutral",
      ai_eng_relevance: s.ai_eng_relevance || "",
      mock: !!s.mock,
    };
    if (s.preview) it.preview = s.preview;
  }
}

function selectGroup(key) {
  state.activeGroupKey = key;
  state.expandedCardId = null;
  state.searchQuery = "";
  state.searchResults = null;
  if (el.searchInput) el.searchInput.value = "";
  renderSidebar();
  renderGroupView();
}

/* ───────────────────────────── Search ──────────────────────────── */
let searchTimer = null;

function queueMatches(it, q) {
  const sum = it.summary || {};
  const hay = [
    it.subject, it.sender_name, it.sender_email, it.snippet, it.preview,
    sum.one_liner, (sum.bullets || []).join(" "), it.category,
  ].filter(Boolean).join("\n").toLowerCase();
  return hay.includes(q);
}

async function runSearch(raw) {
  const q = (raw || "").trim();
  state.searchQuery = q;
  state.expandedCardId = null;
  if (q.length < 2) {
    state.searchResults = null;
    renderSidebar();
    renderGroupView();
    return;
  }
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}&limit=100`);
    if (state.searchQuery !== q) return;
    const seen = new Map();
    for (const it of (data.items || [])) seen.set(it.id, it);
    const ql = q.toLowerCase();
    for (const it of state.queue) {
      if (!seen.has(it.id) && queueMatches(it, ql)) seen.set(it.id, it);
    }
    state.searchResults = [...seen.values()]
      .sort((a, b) => (b.internal_date_ms || 0) - (a.internal_date_ms || 0));
    state.visibleEmails = state.searchResults;
    renderSidebar();
    renderSearchView();
  } catch (e) {
    toast(`Search failed: ${e.message}`, "err", { duration: 3000 });
  }
}

function exitSearch() {
  if (!state.searchResults && !state.searchQuery) return;
  state.searchQuery = "";
  state.searchResults = null;
  if (el.searchInput) el.searchInput.value = "";
  renderSidebar();
  renderGroupView();
}

function renderSearchView() {
  const q = state.searchQuery;
  const results = state.searchResults || [];
  state.visibleEmails = results;
  el.groupHeader.classList.remove("hidden");
  el.groupTitle.textContent = `Search: “${q}”`;
  el.groupCount.textContent = `${results.length} match${results.length === 1 ? "" : "es"}`;
  el.groupOverview.textContent = "Local cache (subject · sender · body) + current batch. Esc clears.";
  el.bulkDeleteBtn.classList.add("hidden");
  el.bulkArchiveBtn.classList.add("hidden");
  el.emailCards.innerHTML = results.length
    ? results.map(emailCard).join("")
    : `<p class="text-sm text-slate-500 dark:text-slate-600 p-6">No matches for “${esc(q)}”.</p>`;
}

/* ───────────────────────────── Group view (middle pane) ──────────── */
function renderGroupView() {
  const key = state.activeGroupKey;
  el.groupHeader.classList.toggle("hidden", key === null);
  if (key === null) {
    el.emailCards.innerHTML = `<p class="text-sm text-slate-500 dark:text-slate-600 p-6">Pick a group on the left, or load more from the inbox.</p>`;
    return;
  }
  if (key === "singles") {
    state.visibleEmails = state.queue.filter((i) => (i.bundle_count || 1) <= 1);
  } else {
    state.visibleEmails = state.queue.filter((i) => i.bundle_key === key && (i.bundle_count || 1) > 1);
    const g = currentGroups().find((x) => x.key === key);
    if (g && !state.groupCache.has(key)) fetchGroup(key, g);
  }
  const visible = state.visibleEmails;
  const first = key === "singles" ? null : state.queue.find((i) => i.bundle_key === key);
  const gs = key !== "singles" ? state.groupCache.get(key) : null;
  let overview = "";
  if (key === "singles") {
    el.groupTitle.textContent = "Singles";
    el.groupCount.textContent = `${visible.length} email${visible.length === 1 ? "" : "s"}`;
    el.bulkDeleteBtn.classList.add("hidden");
    el.bulkArchiveBtn.classList.add("hidden");
  } else {
    el.groupTitle.textContent = (first && (first.bundle_sender_name || first.sender_name)) || key;
    el.groupCount.textContent = `${visible.length} email${visible.length === 1 ? "" : "s"}`;
    el.bulkDeleteBtn.classList.remove("hidden");
    el.bulkArchiveBtn.classList.remove("hidden");
  }
  if (key !== "singles") {
    if (state.groupFetching.has(key)) {
      overview = "Summarizing with gemma3:4b…";
    } else if (gs && gs.overview) {
      overview = gs.overview;
      if (gs.flags && gs.flags.length) overview += "  " + gs.flags.map((f) => `· ${f}`).join("  ");
    }
  }
  el.groupOverview.textContent = overview;
  el.emailCards.innerHTML = visible.length
    ? visible.map(emailCard).join("")
    : `<p class="text-sm text-slate-500 dark:text-slate-600 p-6">This group is empty now.</p>`;

  if (key === "singles") {
    scheduleSingleSummarize(visible.filter((it) => !(it.summary && it.summary.one_liner)).slice(0, 30).map((it) => it.id));
  } else if (singleSummQueue.length) {
    singleSummQueue.length = 0;
  }
}

function emailCategory(it) { return (it.summary && it.summary.category) || it.category || ""; }

function tokenPill(s) {
  if (!s) return "";
  if (s.mock) {
    return `<span class="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-bold text-amber-700 dark:text-amber-300">OFFLINE FALLBACK</span>`;
  }
  if (!s.tokens_in && !s.tokens_out) return "";
  return `<span class="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-bold text-emerald-700 dark:text-emerald-300">in ${s.tokens_in || 0} tok · out ${s.tokens_out || 0} tok · ${((s.latency_ms || 0) / 1000).toFixed(1)}s</span>`;
}

function emailCard(it) {
  const sum = it.summary || {};
  const cat = emailCategory(it);
  const expanded = state.expandedCardId === it.id;
  const sender = it.sender_name || it.sender_email || "Unknown sender";
  const politics = !!(it.is_politics || sum.is_politics);

  const categoryChip = cat
    ? `<span class="shrink-0 rounded bg-violet-500/15 px-1.5 py-0.5 text-[9px] font-bold text-violet-700 dark:text-violet-300" ${it.category_locked ? `title="Manually set — bulk Remap skips it"` : ""}>${it.category_locked ? "🔒 " : ""}${esc(cat)}</span>` : "";
  const promoBadge = it.promo
    ? `<span class="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-bold text-amber-700 dark:text-amber-300">PROMO</span>` : "";
  const isTodo = state.todoIds.has(it.id);
  const todoBadge = isTodo
    ? `<span class="shrink-0 rounded bg-sky-500/15 px-1.5 py-0.5 text-[9px] font-bold text-sky-700 dark:text-sky-300">TODO</span>` : "";
  const inQueue = state.queue.some((x) => x.id === it.id);
  const domain = ((it.sender_email || "").split("@")[1] || "").toLowerCase();
  const attCount = ((it.attachments) || []).length;

  let summaryHTML;
  if (sum.one_liner) {
    const bullets = (sum.bullets || []).length
      ? `<ul class="mt-1 space-y-0.5 text-[12px] text-slate-500 dark:text-slate-500 line-clamp-6">${sum.bullets.map((b) => `<li class="flex gap-1.5"><span class="text-violet-500">▸</span><span>${esc(b)}</span></li>`).join("")}</ul>` : "";
    summaryHTML = `<div class="mt-1.5 flex items-center gap-2 flex-wrap"><p class="text-sm text-slate-700 dark:text-slate-300 font-medium leading-relaxed">${esc(sum.one_liner)}</p>${tokenPill(sum)}</div>${bullets}`;
  } else {
    summaryHTML = `<div class="mt-2 shimmer h-4 w-full"></div>`;
  }

  const previewHTML = it.preview
    ? `<p class="mt-1 text-[12px] text-slate-500 dark:text-slate-500 line-clamp-3 leading-relaxed">${esc(it.preview)}</p>` : "";

  const badges = [];
  if (sum.action_needed === "pay") badges.push(`<span class="rounded bg-red-500/15 px-1.5 py-0.5 text-[9px] font-bold text-red-700 dark:text-red-300">PAYMENT</span>`);
  if (sum.action_needed === "respond" || sum.action_needed === "review") badges.push(`<span class="rounded bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-bold text-amber-700 dark:text-amber-300">${esc(sum.action_needed.toUpperCase())}</span>`);
  if (sum.money) badges.push(`<span class="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[9px] font-bold text-emerald-700 dark:text-emerald-300 max-w-[180px] truncate">${esc(sum.money)}</span>`);
  if (sum.opinion_bias === "negative" || sum.opinion_bias === "biased") badges.push(`<span class="rounded bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-bold text-amber-700 dark:text-amber-300">−ve opinions</span>`);
  if (sum.ai_eng_relevance) badges.push(`<span class="rounded bg-violet-500/15 px-1.5 py-0.5 text-[9px] font-bold text-violet-700 dark:text-violet-300">AI/ENG</span>`);
  if (politics) badges.push(`<span class="rounded bg-slate-500/15 px-1.5 py-0.5 text-[9px] font-bold text-slate-600 dark:text-slate-400">POLITICS (ignored)</span>`);
  const badgesHTML = badges.length ? `<div class="mt-1.5 flex items-center gap-1 flex-wrap">${badges.join("")}</div>` : "";

  const btns = `
    <button data-cardact="archive" data-id="${esc(it.id)}" class="rounded bg-emerald-500/15 px-2 py-1 text-[10px] font-bold text-emerald-700 dark:text-emerald-300 hover:bg-emerald-500/30 transition">Archive</button>
    ${isTodo ? "" : `<button data-cardact="trash" data-id="${esc(it.id)}" class="rounded bg-red-500/15 px-2 py-1 text-[10px] font-bold text-red-700 dark:text-red-300 hover:bg-red-500/30 transition">Delete</button>`}
    <button data-cardact="star" data-id="${esc(it.id)}" class="rounded bg-amber-500/15 px-2 py-1 text-[10px] font-bold text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 transition">Star</button>
    <button data-cardact="skip" data-id="${esc(it.id)}" class="rounded bg-slate-500/15 px-2 py-1 text-[10px] font-bold text-slate-600 dark:text-slate-300 hover:bg-slate-500/30 transition">Skip</button>
    <button data-cardact="todo" data-id="${esc(it.id)}" class="rounded bg-sky-500/15 px-2 py-1 text-[10px] font-bold text-sky-700 dark:text-sky-300 hover:bg-sky-500/30 transition">${isTodo ? "✓ Todo" : "Todo"}</button>
    ${inQueue ? `<button data-cardact="keep" data-id="${esc(it.id)}" class="rounded bg-slate-500/15 px-2 py-1 text-[10px] font-bold text-slate-600 dark:text-slate-300 hover:bg-slate-500/30 transition">Keep</button>` : ""}
    <a href="https://mail.google.com/mail/u/0/#inbox/${esc(it.id)}" target="_blank" rel="noopener noreferrer" class="rounded bg-slate-500/15 px-2 py-1 text-[10px] font-bold text-slate-600 dark:text-slate-300 hover:bg-slate-500/30 transition" title="Open in Gmail">Gmail ↗</a>
    <button data-cardact="recat" data-id="${esc(it.id)}" class="rounded bg-slate-500/15 px-2 py-1 text-[10px] font-bold text-slate-600 dark:text-slate-300 hover:bg-slate-500/30 transition" title="Set or re-evaluate this email's category">⟳</button>
    <button data-cardact="ruleBlock" data-id="${esc(it.id)}" class="rounded bg-red-500/15 px-2 py-1 text-[10px] font-bold text-red-700 dark:text-red-300 hover:bg-red-500/30 transition">Block</button>
    ${it.promo ? `<button data-cardact="rulePromoBlock" data-id="${esc(it.id)}" class="rounded bg-amber-500/15 px-2 py-1 text-[10px] font-bold text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 transition">Auto-del promos</button>` : ""}
    <button data-cardact="regen" data-id="${esc(it.id)}" class="rounded bg-violet-500/15 px-2 py-1 text-[10px] font-bold text-violet-700 dark:text-violet-300 hover:bg-violet-500/30 transition" title="Regenerate AI summary">↻ Regen</button>`;

  return `
    <div class="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/60 p-3 transition group-card ${politics ? "opacity-60" : ""}" data-expand="${esc(it.id)}" data-expanded="${expanded}">
      <div class="flex items-center gap-2 flex-wrap cursor-pointer" data-expand="${esc(it.id)}">
        ${categoryChip} ${promoBadge} ${todoBadge}
        <p class="min-w-0 flex-1 text-sm font-bold text-slate-800 dark:text-slate-200 truncate">${esc(it.subject || "(no subject)")}</p>
      </div>
      <div class="flex items-center gap-1.5 text-[11px] min-w-0 cursor-pointer" data-expand="${esc(it.id)}">
        <span class="truncate font-semibold text-slate-600 dark:text-slate-300">${esc(sender)}</span>
        ${domain ? `<span class="shrink-0 rounded-md bg-sky-500/15 px-1.5 py-px font-mono text-[10px] font-bold text-sky-700 dark:text-sky-300" title="${esc(it.sender_email || "")}">@${esc(domain)}</span>` : ""}
        ${attCount ? `<span class="shrink-0 rounded-md bg-violet-500/15 px-1.5 py-px text-[10px] font-bold text-violet-700 dark:text-violet-300" title="${attCount} attachment${attCount === 1 ? "" : "s"} — expand to download">📎${attCount}</span>` : ""}
      </div>
      ${summaryHTML}
      ${previewHTML}
      ${badgesHTML}
      <div class="mt-2 flex items-center gap-1.5 flex-wrap">
        ${btns}
        <span class="ml-auto text-[10px] text-slate-400 dark:text-slate-600 cursor-pointer" data-expand="${esc(it.id)}">${expanded ? "collapse ▴" : "open ▾"}</span>
      </div>
      ${expanded ? detailHTML(it) : ""}
    </div>`;
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function detailHTML(it) {
  const d = state.detail.get(it.id);
  if (!d) fetchDetail(it.id);
  const atts = ((d && d.attachments) || it.attachments || []);
  const attChip = (a) => {
    const url = `/api/messages/${esc(it.id)}/attachments/${esc(a.attachmentId)}`;
    const mime = a.mimeType || "";
    return `<a href="${url}" target="_blank" rel="noopener noreferrer" class="rounded-lg bg-violet-500/15 px-2 py-1 text-[10px] font-bold text-violet-700 dark:text-violet-300 hover:bg-violet-500/30 transition" title="${esc(mime || "file")} · ${fmtSize(a.size)} — fetches from Gmail only when clicked, never stored">📎 ${esc(a.filename || "attachment")} <span class="opacity-70 font-normal">${fmtSize(a.size)}</span></a>`;
  };
  const attHTML = atts.length ? `<div class="mt-2 flex items-start gap-1.5 flex-wrap">${atts.map(attChip).join("")}</div>` : "";
  const sum = (d && d.summary) || null;
  const sender = (d && d.sender_email) || it.sender_email || "";
  const dateMs = (d && d.internal_date_ms) || it.internal_date_ms;
  const date = dateMs ? timeAgo(new Date(dateMs).toISOString()) : "";
  const bodyHTML = !d
    ? `<div class="mt-2 shimmer h-4 w-full"></div><div class="mt-2 shimmer h-4 w-3/4"></div><div class="mt-2 shimmer h-4 w-5/6"></div>`
    : d.body_text
      ? `<div class="email-body mt-2 max-w-none leading-relaxed text-slate-700 dark:text-slate-300 selection:bg-violet-500/20">${linkify(d.body_text)}</div>${d.body_truncated ? `<p class="mt-2 text-xs text-amber-600 dark:text-amber-400/80">Body truncated at 80k chars for speed.</p>` : ""}`
      : `<div class="email-body mt-2 text-slate-700 dark:text-slate-300">(no extractable text)</div>`;
  return `
    <div class="mt-3 border-t border-slate-200 dark:border-slate-800 pt-3">
      <div class="flex items-center gap-2 flex-wrap text-[12px] text-slate-500 dark:text-slate-400">
        <span class="font-mono truncate">${esc(sender)}</span>
        <span>·</span>
        <span>${esc(date)}</span>
        ${tokenPill(sum)}
      </div>
      ${attHTML}
      ${bodyHTML}
      <div class="mt-2 flex items-center gap-1.5">
        <button data-cardact="ruleBlock" data-id="${esc(it.id)}" class="rounded bg-red-500/15 px-2 py-1 text-[10px] font-bold text-red-700 dark:text-red-300 hover:bg-red-500/30 transition">Block sender</button>
        ${it.promo ? `<button data-cardact="rulePromoBlock" data-id="${esc(it.id)}" class="rounded bg-amber-500/15 px-2 py-1 text-[10px] font-bold text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 transition">Auto-del promos</button>` : ""}
      </div>
    </div>`;
}

function currentItemOf(id) {
  return state.queue.find((i) => i.id === id)
    || (state.searchResults || []).find((i) => i.id === id);
}

function refreshMainView() {
  renderSidebar();
  if (state.searchResults) renderSearchView();
  else if (state.queue.length === 0) showEmpty();
  else if (state.viewMode === "categories") renderCategoryGroupView();
  else if (state.viewMode === "threads") renderThreadView();
  else renderGroupView();
}

/* ───────────────────────────── Actions ─────────────────────────── */
function openTodoModal(id) {
  const item = currentItemOf(id);
  if (!item) { toast("That email is no longer in the queue", "err", { duration: 2000 }); return; }
  state.pendingTodoId = id;
  el.todoModalSub.textContent = item.subject || "(no subject)";
  el.todoDueInput.value = "";
  el.todoModal.classList.remove("hidden");
  el.todoModal.classList.add("flex");
  setTimeout(() => el.todoDueInput.focus(), 50);
}

function closeTodoModal() {
  state.pendingTodoId = null;
  el.todoModal.classList.add("hidden");
  el.todoModal.classList.remove("flex");
}

async function confirmTodoModal() {
  const id = state.pendingTodoId;
  const item = id ? currentItemOf(id) : null;
  if (!item) { closeTodoModal(); return; }
  try {
    await api("/api/todos/add", {
      method: "POST",
      body: JSON.stringify({ item, due_date: el.todoDueInput.value || null }),
    });
    state.todoIds.add(id);
    closeTodoModal();
    toast("Google Task created + parked on the TODO page", "ok", { duration: 2600 });
    refreshMainView();
  } catch (e) {
    toast(`Todo failed: ${e.message}`, "err", { duration: 5000 });
  }
}

async function untodoTodo(id) {
  try {
    await api("/api/todos/remove", { method: "POST", body: JSON.stringify({ id }) });
    state.todoIds.delete(id);
    toast("Removed from TODO (Google Task deleted too)", "info", { duration: 2000 });
    refreshMainView();
  } catch (e) {
    toast(`Todo failed: ${e.message}`, "err", { duration: 3000 });
  }
}

async function toggleTodo(id) {
  if (!id) { toast("Nothing to park — open a group first", "info", { duration: 1600 }); return; }
  if (state.todoIds.has(id)) untodoTodo(id);
  else openTodoModal(id);
}

async function actOn(action, id) {
  if (state.busy) return;
  if (action === "trash" && state.todoIds.has(id)) {
    toast("TODO items can't be deleted — remove from TODO first", "err", { duration: 2600 });
    return;
  }
  const item = currentItemOf(id);
  if (!item) return;
  const wasInQueue = state.queue.some((i) => i.id === id);
  state.busy = true;
  try {
    await api(`/api/messages/${id}/${action}`, { method: "POST", body: "{}" });
    if (wasInQueue) {
      pushHistory(action, [id], [item]);
      removeFromQueue(id);
      toast(`${actionLabel(action)} · synced to Gmail`, action === "star" ? "info" : "ok", { undo: true });
    } else {
      dropFromView(id);
      toast(`${actionLabel(action)} · synced to Gmail`, action === "star" ? "info" : "ok");
    }
  } catch (e) {
    toast(`Action failed: ${e.message}`, "err", { duration: 4000 });
  } finally {
    state.busy = false;
  }
}

function keepEmail(id) {
  const idx = state.queue.findIndex((i) => i.id === id);
  if (idx === -1) return;
  state.queue.splice(idx, 1);
  invalidateGroups();
  if (idx < state.index) state.index -= 1;
  if (state.index >= state.queue.length) state.index = Math.max(0, state.queue.length - 1);
  renderSidebar();
  toast("Kept in inbox · removed from batch", "info", { duration: 1600 });
  if (state.queue.length === 0) { showEmpty(); return; }
  renderGroupView();
}

function restoreToQueue(items) {
  state.queue.push(...items);
  state.queue.sort((a, b) => (b.internal_date_ms || 0) - (a.internal_date_ms || 0));
  invalidateGroups();
}

function queueBulk(action, groupKey) {
  if (!groupKey || groupKey === "singles") return;
  if (state.pendingOps.some((o) => o.groupKey === groupKey && o.status !== "done")) {
    toast("Already processing", "info", { duration: 1500 });
    return;
  }
  const affected = state.queue.filter((i) => i.bundle_key === groupKey && (i.bundle_count || 1) > 1);
  const ids = affected.map((i) => i.id);
  if (!ids.length) {
    toast(action === "trash" ? "Nothing to delete here" : "Nothing to archive here", "info", { duration: 1600 });
    return;
  }
  const first = affected[0];
  const sender = first.bundle_sender_name || first.sender_name || groupKey;
  const label = `${action === "trash" ? "Trash" : "Archive"} «${sender}» (${ids.length} email${ids.length === 1 ? "" : "s"})`;
  const idSet = new Set(ids);

  state.queue = state.queue.filter((i) => !idSet.has(i.id));
  ids.forEach((id) => state.detail.delete(id));
  invalidateGroups();
  state.expandedCardId = null;
  renderSidebar();
  renderGroupView();

  const op = {
    id: (window.crypto && typeof window.crypto.randomUUID === "function" ? window.crypto.randomUUID() : String(Date.now()) + "-" + Math.random()),
    action,
    groupKey,
    label,
    count: ids.length,
    status: "running",
    ts: Date.now(),
  };
  state.pendingOps.unshift(op);
  if (state.pendingOps.length > 20) state.pendingOps.length = 20;
  renderPendingOps();

  api(`/api/bundles/${encodeURIComponent(groupKey)}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message_ids: ids }),
  })
    .then((res) => {
      const failedN = (res.failed || []).length;
      if (failedN) toast(`${failedN} email${failedN === 1 ? "" : "s"} failed — retry`, "err", { duration: 4000 });
      op.status = "done";
      renderPendingOps();
      scheduleOpRemoval(op);
    })
    .catch((e) => {
      op.status = "failed";
      renderPendingOps();
      restoreToQueue(affected);
      refreshMainView();
      toast(`Bulk ${action === "trash" ? "delete" : "archive"} failed — emails restored`, "err", { duration: 4000 });
      scheduleOpRemoval(op);
    });
}

function queueDomainTrash(domain, items) {
  if (!domain || !items || !items.length) return;
  if (state.pendingOps.some((o) => o.groupKey === "__domain__" + domain && o.status !== "done")) {
    toast(`Already trashing ${domain}`, "info", { duration: 1500 });
    return;
  }
  const ids = items.map((i) => i.id);
  const label = `Trash all from @${domain} (${ids.length} email${ids.length === 1 ? "" : "s"})`;

  const idSet = new Set(ids);
  state.queue = state.queue.filter((i) => !idSet.has(i.id));
  ids.forEach((id) => state.detail.delete(id));
  invalidateGroups();
  state.expandedCardId = null;
  renderSidebar();
  if (state.viewMode === "categories") renderCategoryGroupView();
  else if (state.viewMode === "group") renderGroupView();

  const op = {
    id: (window.crypto && typeof window.crypto.randomUUID === "function" ? window.crypto.randomUUID() : String(Date.now()) + "-" + Math.random()),
    action: "trash",
    groupKey: "__domain__" + domain,
    label,
    count: ids.length,
    status: "running",
    ts: Date.now(),
  };
  state.pendingOps.unshift(op);
  if (state.pendingOps.length > 20) state.pendingOps.length = 20;
  renderPendingOps();

  api(`/api/domains/${encodeURIComponent(domain)}/trash`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message_ids: ids }),
  })
    .then((res) => {
      const failedN = (res.failed || []).length;
      if (failedN) toast(`${failedN} email${failedN === 1 ? "" : "s"} failed — retry`, "err", { duration: 4000 });
      op.status = "done";
      renderPendingOps();
      scheduleOpRemoval(op);
    })
    .catch((e) => {
      op.status = "failed";
      renderPendingOps();
      toast(`Trash domain failed — ${e.message}`, "err", { duration: 4000 });
      scheduleOpRemoval(op);
    });
}

function actionLabel(action) {
  return { trash: "Trashed", archive: "Archived", star: "Starred" }[action] || action;
}

function dropFromView(id) {
  state.detail.delete(id);
  if (state.searchResults) {
    state.searchResults = state.searchResults.filter((i) => i.id !== id);
    state.visibleEmails = state.visibleEmails.filter((i) => i.id !== id);
  }
}

function removeFromQueue(id) {
  state.detail.delete(id);
  const idx = state.queue.findIndex((i) => i.id === id);
  if (idx === -1) {
    dropFromView(id);
    refreshMainView();
    return;
  }
  state.queue.splice(idx, 1);
  invalidateGroups();
  if (idx < state.index) state.index -= 1;
  if (state.index >= state.queue.length) state.index = Math.max(0, state.queue.length - 1);
  dropFromView(id);
  refreshMainView();
}

function invalidateGroups() {
  state.groupsSig = null;
  state.groupCache.clear();
}

async function skipForNow(id) {
  const target = id ?? currentTargetId();
  if (!target) { toast("Nothing to skip — open a group first", "info", { duration: 1600 }); return; }
  const item = currentItemOf(target);
  if (!item) { toast("That email is no longer in the queue", "err", { duration: 2000 }); return; }
  removeAndSkip(item);
}

async function removeAndSkip(item) {
  if (state.busy || !item) return;
  state.busy = true;
  try {
    await api("/api/skipped/add", { method: "POST", body: JSON.stringify({ item }) });
    state.skippedIds.add(item.id);
    state.skippedItems.push(item);
    const idx = state.queue.findIndex((i) => i.id === item.id);
    if (idx !== -1) {
      state.queue.splice(idx, 1);
      invalidateGroups();
      if (idx < state.index) state.index -= 1;
      if (state.index >= state.queue.length) state.index = Math.max(0, state.queue.length - 1);
    }
    dropFromView(item.id);
    refreshMainView();
    toast("Skipped for now — hidden until you bring it back", "info", {
      undo: true, undoFn: () => restoreSkipped(item.id), duration: 3600,
    });
  } catch (e) {
    toast(`Skip failed: ${e.message}`, "err", { duration: 4000 });
  } finally {
    state.busy = false;
  }
}

function skipForNowById(id) { removeAndSkip(currentItemOf(id)); }

async function restoreSkipped(id) {
  const idx = state.skippedItems.findIndex((i) => i.id === id);
  if (idx === -1) return;
  const item = state.skippedItems.splice(idx, 1)[0];
  state.skippedIds.delete(id);
  try {
    await api("/api/skipped/remove", { method: "POST", body: JSON.stringify({ id }) });
  } catch (e) {
    toast(`Restore failed: ${e.message}`, "err", { duration: 3000 });
  }
  state.queue.splice(state.index, 0, item);
  state.originalTotal = Math.max(state.originalTotal, state.queue.length + state.skippedItems.length);
  invalidateGroups();
  if (state.searchResults) exitSearch();
  else {
    renderSidebar();
    if (state.queue.length === 0) { showEmpty(); return; }
    renderGroupView();
  }
  toast("Back in the batch", "ok", { duration: 1800 });
}

async function restoreAllSkipped() {
  const items = state.skippedItems.splice(0);
  state.skippedIds.clear();
  try {
    await api("/api/skipped/clear", { method: "POST", body: "{}" });
  } catch (e) {
    toast(`Restore failed: ${e.message}`, "err", { duration: 3000 });
  }
  if (items.length) state.queue.splice(state.index, 0, ...items);
  state.originalTotal = Math.max(state.originalTotal, state.queue.length);
  invalidateGroups();
  if (state.searchResults) exitSearch();
  else {
    renderSidebar();
    if (state.queue.length === 0) { showEmpty(); return; }
    renderGroupView();
  }
  toast(`Brought back ${items.length} skipped email${items.length === 1 ? "" : "s"}`, "ok", { duration: 2200 });
}

async function issueRuleBlock(id) {
  const item = currentItemOf(id);
  if (!item) { toast("This email is no longer in the queue", "err", { duration: 2000 }); return; }
  const email = item.sender_email;
  const name = item.sender_name || email;
  if (!email) { toast("This email has no sender address", "err", { duration: 2200 }); return; }
  const md = ruleMdForSender(email, name, false);
  try {
    const res = await api("/api/rules", { method: "POST", body: JSON.stringify({ markdown: md }) });
    toast(res.appended ? `Blocked ${name} — added to blocklist` : `Blocked ${name} — rule created`, "ok", { duration: 2800 });
    await refreshRulesData();
  } catch (e) {
    toast(`Block failed: ${e.message}`, "err", { duration: 4000 });
  }
}

async function issueRulePromoBlock(id) {
  const item = currentItemOf(id);
  if (!item) { toast("This email is no longer in the queue", "err", { duration: 2000 }); return; }
  const email = item.sender_email;
  const name = item.sender_name || email;
  if (!email) { toast("This email has no sender address", "err", { duration: 2200 }); return; }
  if (!item.promo) { toast("Only promo emails can be auto-deleted (use Block for the whole sender)", "err", { duration: 2600 }); return; }
  const md = ruleMdForSender(email, name, true);
  try {
    const res = await api("/api/rules", { method: "POST", body: JSON.stringify({ markdown: md }) });
    toast(res.appended ? `Promo auto-delete ON for ${name} — added to blocklist` : `Promo auto-delete ON for ${name} — rule created`, "ok", { duration: 2800 });
    await refreshRulesData();
  } catch (e) {
    toast(`Promo block failed: ${e.message}`, "err", { duration: 4000 });
  }
}

function normalizeSender(sender) {
  const s = (sender || "").trim();
  return s.includes("@") ? s.toLowerCase() : s;
}

function ruleMdForSender(email, name, promoOnly) {
  const sender = normalizeSender(email);
  const scope = promoOnly ? "promo_only" : "all_mail";
  const title = `Trash ${promoOnly ? "PROMOS" : "ALL"} from ${sender}`;
  const about = promoOnly
    ? "Automatically trash promo mail from this sender."
    : "Automatically trash all mail from this sender.";
  return [
    "---",
    `name: ${title}`,
    "enabled: true",
    "action: trash",
    `scope: ${scope}`,
    "---",
    "## match",
    `sender: ${sender}`,
    "",
    "## about",
    about,
  ].join("\n");
}

async function refreshRulesData() {
  try {
    const [rulesRes, proposalsRes, wikiRes] = await Promise.all([
      api("/api/rules"),
      api("/api/proposals"),
      api("/api/wiki/observations"),
    ]);
    state.rules = rulesRes.items || [];
    state.proposals = proposalsRes.items || [];
    state.wikiObservations = wikiRes.items || [];
  } catch (e) {
    // non-fatal
  }
}

/* ───────────────────────────── Rules CRUD + editor ─────────────── */
function editorShowErrors(err) {
  const detail = err && err.detail;
  const errors = detail && detail.errors;
  if (errors && Array.isArray(errors)) {
    el.editorErrors.textContent = errors.map((e) => `line ${e.line}: ${e.msg}`).join("\n");
  } else {
    el.editorErrors.textContent = err ? String(err.message || err) : "Save failed";
  }
  el.editorErrors.classList.remove("hidden");
}

function openEditor(ruleId) {
  state.editorOpen = true;
  state.editorRuleId = ruleId ?? null;
  state.editorApplyNow = false;
  el.editorApplyNow.checked = false;
  el.editorErrors.classList.add("hidden");
  el.editorErrors.textContent = "";
  if (ruleId == null) {
    state.editorMarkdown = "---\nname: Untitled rule\nenabled: true\naction: trash\nscope: all_mail\n---\n## match\nsender: \n\n## about\n";
    el.editorTitle.textContent = "New Rule";
    el.editorDelete.classList.add("hidden");
  } else {
    const rule = state.rules.find((r) => r.id === ruleId);
    state.editorMarkdown = rule ? rule.skill_md : "";
    el.editorTitle.textContent = rule && rule.parsed ? `Edit: ${rule.parsed.name}` : `Edit rule #${ruleId}`;
    el.editorDelete.classList.remove("hidden");
  }
  el.editorContent.value = state.editorMarkdown;
  el.normalView.classList.add("hidden");
  el.editorView.classList.remove("hidden");
  el.editorView.classList.add("flex");
  setTimeout(() => el.editorContent.focus(), 50);
}

function cancelEditor() {
  state.editorOpen = false;
  state.editorRuleId = null;
  el.editorView.classList.add("hidden");
  el.editorView.classList.remove("flex");
  el.normalView.classList.remove("hidden");
}

async function saveEditor() {
  const md = el.editorContent.value;
  state.editorMarkdown = md;
  el.editorErrors.classList.add("hidden");
  try {
    if (state.editorRuleId == null) {
      await api("/api/rules", { method: "POST", body: JSON.stringify({ markdown: md }) });
      toast("Rule created", "ok", { duration: 2000 });
    } else {
      await api(`/api/rules/${state.editorRuleId}`, {
        method: "PATCH",
        body: JSON.stringify({ markdown: md }),
      });
      toast("Rule saved", "ok", { duration: 2000 });
      if (state.editorApplyNow) await applyRuleNow(state.editorRuleId, true);
    }
    cancelEditor();
    await refreshRulesData();
  } catch (e) {
    editorShowErrors(e);
  }
}

async function deleteEditorRule() {
  if (state.editorRuleId == null) { cancelEditor(); return; }
  if (!confirm("Delete this rule?")) return;
  try {
    await api(`/api/rules/${state.editorRuleId}`, { method: "DELETE" });
    toast("Rule deleted", "info", { duration: 1800 });
    cancelEditor();
    await refreshRulesData();
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "err", { duration: 3000 });
  }
}

async function toggleRule(id) {
  const rule = state.rules.find((r) => r.id === id);
  try {
    await api(`/api/rules/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: !(rule && rule.enabled) }) });
    await refreshRulesData();
  } catch (e) {
    toast(`Toggle failed: ${e.message}`, "err", { duration: 3000 });
  }
}

async function moveRule(id, dir) {
  try {
    await api(`/api/rules/${id}/move`, { method: "POST", body: JSON.stringify({ direction: dir }) });
    await refreshRulesData();
  } catch (e) {
    toast(`Move failed: ${e.message}`, "err", { duration: 3000 });
  }
}

async function deleteRule(id) {
  if (!confirm("Delete this rule?")) return;
  try {
    await api(`/api/rules/${id}`, { method: "DELETE" });
    toast("Rule deleted", "info", { duration: 1800 });
    if (state.editorRuleId === id) cancelEditor();
    await refreshRulesData();
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "err", { duration: 3000 });
  }
}

async function applyRuleNow(id, silent) {
  try {
    const res = await api(`/api/rules/${id}/apply-now`, { method: "POST", body: "{}" });
    if (!silent) toast(`Applied: ${res.matched || 0} matched (${res.trash || 0} trash · ${res.star || 0} star · ${res.skip || 0} skip)`, "ok", { duration: 3000 });
    return res;
  } catch (e) {
    if (!silent) toast(`Apply failed: ${e.message}`, "err", { duration: 4000 });
    throw e;
  }
}

async function approveProposal(id) {
  try {
    await api(`/api/proposals/${id}/approve`, { method: "POST", body: JSON.stringify({ apply_now: false }) });
    toast("Proposal approved — rule created", "ok", { duration: 2200 });
    await refreshRulesData();
  } catch (e) {
    toast(`Approve failed: ${e.message}`, "err", { duration: 3000 });
  }
}

function editProposal(id) {
  const p = state.proposals.find((x) => x.id === id);
  if (!p) return;
  openEditor(null);
  el.editorTitle.textContent = `New rule from proposal: ${p.label}`;
  el.editorContent.value = p.proposed_skill_md || "";
}

async function rejectProposal(id) {
  try {
    await api(`/api/proposals/${id}/reject`, { method: "POST", body: "{}" });
    toast("Proposal rejected", "info", { duration: 2000 });
    await refreshRulesData();
  } catch (e) {
    toast(`Reject failed: ${e.message}`, "err", { duration: 3000 });
  }
}

async function dismissWiki(id) {
  try {
    await api(`/api/wiki/observations/${id}/dismiss`, { method: "POST", body: "{}" });
    await refreshRulesData();
  } catch (e) {
    toast(`Dismiss failed: ${e.message}`, "err", { duration: 3000 });
  }
}

function createRuleFromWiki(id) {
  const w = state.wikiObservations.find((x) => x.id === id);
  openEditor(null);
  if (w) {
    el.editorTitle.textContent = `New rule from observation: ${w.target}`;
    el.editorContent.value = `---\nname: ${w.target}\nenabled: true\naction: trash\nscope: all_mail\n---\n## match\nsender: ${w.target}\n\n## about\n${w.summary || ""}\n`;
  }
}

/* ───────────────────────────── History / undo ──────────────────── */
function pushHistory(action, ids, items) {
  const first = items[0];
  const sender = first ? (first.sender_name || first.sender_email || "email") : "email";
  const count = ids.length;
  const label = `${actionLabel(action).toLowerCase() === "trashed" ? "Deleted" : actionLabel(action)} ${count > 1 ? `${count} emails` : sender}`;
  state.history.unshift({ action, ids: [...ids], items: [...items], label, at: Date.now() });
  if (state.history.length > 20) state.history.pop();
  renderHistory();
}

async function undoIndex(i) {
  const h = state.history[i];
  if (h) await undoEntry(h, i);
}

async function undoLast() {
  if (!state.history.length) { toast("Nothing to undo", "info", { duration: 1200 }); return; }
  await undoEntry(state.history[0], 0);
}

async function undoEntry(h, i) {
  state.busy = true;
  try {
    const res = await api("/api/undo", {
      method: "POST",
      body: JSON.stringify({ action: h.action, message_ids: h.ids }),
    });
    state.history.splice(i, 1);
    const failedN = (res.failed || []).length;
    const okIds = new Set(h.ids.filter((id) => !(res.failed || []).includes(id)));
    const restoredItems = h.items.filter((item) => okIds.has(item.id));
    restoredItems.forEach((item) => state.detail.delete(item.id));
    state.queue.splice(state.index, 0, ...restoredItems);
    invalidateGroups();
    refreshMainView();
    toast(`Restored ${restoredItems.length} email${restoredItems.length === 1 ? "" : "s"} · synced to Gmail`, "ok");
  } catch (e) {
    toast(`Undo failed: ${e.message}`, "err", { duration: 4000 });
  } finally {
    state.busy = false;
  }
}

/* ───────────────────────────── Keyboard ────────────────────────── */
function currentTargetId() {
  if (state.expandedCardId) return state.expandedCardId;
  if (state.visibleEmails && state.visibleEmails.length) return state.visibleEmails[0].id;
  return null;
}
function currentTargetItem() {
  const id = currentTargetId();
  return id ? currentItemOf(id) : null;
}
function doAction(action) {
  const id = currentTargetId();
  if (!id) { toast("Open a group to act on emails", "info", { duration: 1600 }); return; }
  actOn(action, id);
}
function issueBlock() {
  issueRuleBlock(currentTargetId());
}
function issuePromoBlock() {
  issueRulePromoBlock(currentTargetId());
}
function groupKeys() {
  const keys = [];
  const singleCount = state.queue.filter((i) => (i.bundle_count || 1) <= 1).length;
  if (singleCount) keys.push("singles");
  currentGroups().forEach((g) => keys.push(g.key));
  return keys;
}
function stepGroup(dir) {
  if (state.viewMode === "threads") {
    const ts = threads();
    if (!ts.length) { toast("No threads to switch", "info", { duration: 1600 }); return; }
    const tids = ts.map((t) => t.tid);
    const idx = tids.indexOf(state.activeThreadId);
    selectThread(tids[((idx + dir) % tids.length + tids.length) % tids.length]);
    return;
  }
  if (state.viewMode === "categories") {
    const cats = categoryCounts().map(([c]) => c);
    if (!cats.length) { toast("No categories to switch", "info", { duration: 1600 }); return; }
    const idx = cats.indexOf(state.activeCatGroup);
    selectCategoryGroup(cats[((idx + dir) % cats.length + cats.length) % cats.length]);
    return;
  }
  const keys = groupKeys();
  if (!keys.length) { toast("No groups to switch", "info", { duration: 1600 }); return; }
  const cur = state.activeGroupKey;
  const idx = cur === null ? (dir > 0 ? -1 : 0) : keys.indexOf(cur);
  selectGroup(keys[((idx + dir) % keys.length + keys.length) % keys.length]);
}
window.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (el.app.classList.contains("hidden")) return;

  const key = e.key.toLowerCase();
  switch (key) {
    case "d":
    case "backspace":
      e.preventDefault(); doAction("trash"); break;
    case "e":
    case "a":
      e.preventDefault(); doAction("archive"); break;
    case "s":
      e.preventDefault(); doAction("star"); break;
    case "arrowright":
      e.preventDefault(); stepGroup(1); break;
    case "arrowleft":
      e.preventDefault(); stepGroup(-1); break;
    case "x":
      e.preventDefault(); skipForNow(currentTargetId()); break;
    case "t":
      e.preventDefault(); toggleTodo(currentTargetId()); break;
    case "b":
      e.preventDefault(); issueBlock(); break;
    case "p":
      e.preventDefault(); issuePromoBlock(); break;
    case "u":
      e.preventDefault(); undoLast(); break;
    case "enter":
      e.preventDefault();
      toast("Reply composer ships in Phase 2", "info", { duration: 1600 });
      break;
    case "/":
      e.preventDefault();
      if (el.searchInput) el.searchInput.focus();
      break;
    case "escape":
      exitSearch();
      clearToasts();
      break;
  }
});

/* ───────────────────────────── Wiring ──────────────────────────── */
el.authBtn.addEventListener("click", () => { window.location.href = "/auth"; });
el.logoutBtn.addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST", body: "{}" }).catch(() => {});
  window.location.reload();
});
el.reloadBtn.addEventListener("click", loadQueue);
el.emptyReload.addEventListener("click", () => { if (state.nextPageToken) loadMore(); else loadQueue(); });
el.loadMoreBtn.addEventListener("click", loadMore);
el.clearCacheBtn.addEventListener("click", async () => {
  const clearing = toast("Clearing local cache…", "info", { duration: 0 });
  try {
    const res = await api("/api/cache/clear", { method: "POST", body: "{}" });
    toast(`Cleared ${res.cleared || 0} cached emails — reloading fresh from Gmail`, "ok", { duration: 3200 });
  } catch (e) {
    toast(`Clear cache failed: ${e.message}`, "err", { duration: 4000 });
  }
  await loadQueue();
});
el.undoTopBtn.addEventListener("click", undoLast);
el.themeBtn.addEventListener("click", toggleTheme);
el.skipForNowBtn.addEventListener("click", () => skipForNow());
el.skippedBtn.addEventListener("click", restoreAllSkipped);
el.skippedRestoreBtn.addEventListener("click", restoreAllSkipped);
el.editorCancel.addEventListener("click", cancelEditor);
el.editorSave.addEventListener("click", saveEditor);
el.editorDelete.addEventListener("click", deleteEditorRule);
el.editorApplyNow.addEventListener("change", (e) => { state.editorApplyNow = e.target.checked; });
applyThemeUI();
el.bulkDeleteBtn.addEventListener("click", () => { if (state.activeGroupKey && state.activeGroupKey !== "singles") queueBulk("trash", state.activeGroupKey); else toast("Open a group to bulk delete", "info", { duration: 1600 }); });
el.bulkArchiveBtn.addEventListener("click", () => { if (state.activeGroupKey && state.activeGroupKey !== "singles") queueBulk("archive", state.activeGroupKey); else toast("Open a group to bulk archive", "info", { duration: 1600 }); });
el.groupList.addEventListener("click", (e) => {
  const th = e.target.closest("[data-thread]");
  if (th) { selectThread(th.dataset.thread); return; }
  const c = e.target.closest("[data-catgroup]");
  if (c) { selectCategoryGroup(c.dataset.catgroup); return; }
  const d = e.target.closest("[data-gdel]");
  if (d) {
    e.stopPropagation();
    queueBulk("trash", d.dataset.gdel);
    return;
  }
  const t = e.target.closest("[data-group]");
  if (t) selectGroup(t.dataset.group);
});
el.viewSendersBtn.addEventListener("click", () => setViewMode("senders"));
el.viewCatsBtn.addEventListener("click", () => setViewMode("categories"));
el.viewThreadsBtn.addEventListener("click", () => setViewMode("threads"));

if (el.searchInput) {
  el.searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => runSearch(el.searchInput.value), 300);
  });
  el.searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); clearTimeout(searchTimer); runSearch(el.searchInput.value); }
    else if (e.key === "Escape") { e.preventDefault(); el.searchInput.blur(); exitSearch(); }
  });
}
el.todoModalSave.addEventListener("click", confirmTodoModal);
el.todoModalCancel.addEventListener("click", closeTodoModal);
el.todoModal.addEventListener("click", (e) => { if (e.target === el.todoModal) closeTodoModal(); });
el.todoDueInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); confirmTodoModal(); }
  else if (e.key === "Escape") { e.preventDefault(); closeTodoModal(); }
});
el.recatModalSave.addEventListener("click", confirmRecatModal);
el.recatModalCancel.addEventListener("click", closeRecatModal);
el.recatModalAuto.addEventListener("click", () => {
  const id = state.pendingRecatId;
  closeRecatModal();
  remapOne(id);
});
el.recatModal.addEventListener("click", (e) => { if (e.target === el.recatModal) closeRecatModal(); });
el.recatSelect.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); confirmRecatModal(); }
  else if (e.key === "Escape") { e.preventDefault(); closeRecatModal(); }
});
el.remapCatsBtn.addEventListener("click", remapCategories);

el.emailCards.addEventListener("click", (e) => {
  const t = e.target.closest("[data-expand]");
  if (!t || e.target.closest("button,a")) return;
  const id = t.dataset.expand;
  state.expandedCardId = state.expandedCardId === id ? null : id;
  refreshMainView();
});
el.emailCards.addEventListener("click", (e) => {
  const b = e.target.closest("[data-domain-del]");
  if (!b) return;
  const domain = b.dataset.domainDel;
  const cat = state.activeCatGroup;
  const affected = state.queue.filter(
    (i) => (emailCategory(i) || "Uncategorized") === cat &&
      ((i.sender_email || "").split("@")[1] || "").toLowerCase() === domain
  );
  if (affected.length) queueDomainTrash(domain, affected);
});

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-cardact]");
  if (!b) return;
  const id = b.dataset.id;
  const act = b.dataset.cardact;
  if (act === "skip") skipForNowById(id);
  else if (act === "recat") openRecatModal(id);
  else if (act === "regen") regenerateSummary(id);
  else if (act === "todo") toggleTodo(id);
  else if (act === "ruleBlock") issueRuleBlock(id);
  else if (act === "rulePromoBlock") issueRulePromoBlock(id);
  else if (act === "keep") keepEmail(id);
  else actOn(act, id);
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.authenticated && el.app.classList.contains("hidden") === false) {
    renderSidebar();
  }
});

init();

// Note: action buttons should never keep focus, or Enter/Space would re-trigger them.
document.addEventListener("click", (e) => {
  if (e.target && e.target.closest("button")) e.target.blur();
});