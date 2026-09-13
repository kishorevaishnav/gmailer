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
  nextPageToken: null,    // Gmail pagination token for "load next batch"
  blockedSenders: [],     // [{email, sender_name}] auto-deleted on every pull
  showBlockedList: false,
  promoBlockedSenders: [],  // [{email, sender_name}] promo mail auto-deleted on every pull
  showPromoBlockedList: false,
  activeGroupKey: null,    // "singles" | bundle_key | null
  expandedCardId: null,    // currently inline-expanded email id
  activeCategory: "all",   // category filter
  categories: [],          // allowed category names from /api/categories
  pendingOps: [],          // [{id, action, groupKey, label, count, status, ts}] background bulk ops
  visibleEmails: [],       // emails currently shown in the middle pane (group view)
  autoSelectedOnce: false, // pick a default group only on the very first queue load
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
  groupList: $("groupList"), groupsCount: $("groupsCount"),
  cacheInfo: $("cacheInfo"),
  skippedBtn: $("skippedBtn"), skippedCount: $("skippedCount"), skippedRestoreBtn: $("skippedRestoreBtn"),
  skipForNowBtn: $("skipForNowBtn"),
  blockedBtn: $("blockedBtn"), blockedCount: $("blockedCount"), blockedList: $("blockedList"),
  promoBlockedBtn: $("promoBlockedBtn"), promoBlockedCount: $("promoBlockedCount"), promoBlockedList: $("promoBlockedList"),
  loadMoreBtn: $("loadMoreBtn"),
  userChip: $("userChip"), logoutBtn: $("logoutBtn"),
  themeBtn: $("themeBtn"), themeIconMoon: $("themeIconMoon"), themeIconSun: $("themeIconSun"), themeIconApple: $("themeIconApple"),
  position: $("position"), positionTotal: $("positionTotal"),
  undoTopBtn: $("undoTopBtn"), reloadBtn: $("reloadBtn"), clearCacheBtn: $("clearCacheBtn"),
  toasts: $("toasts"),
  emptyScreen: $("emptyScreen"), emptyStat: $("emptyStat"), emptyReload: $("emptyReload"),
  groupHeader: $("groupHeader"), groupTitle: $("groupTitle"), groupCount: $("groupCount"), groupOverview: $("groupOverview"), bulkDeleteBtn: $("bulkDeleteBtn"), bulkArchiveBtn: $("bulkArchiveBtn"), emailCards: $("emailCards"), categoryChips: $("categoryChips"), categoryInput: $("categoryInput"), categoryAddBtn: $("categoryAddBtn"), categoriesCount: $("categoriesCount"),
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
  if (!res.ok) throw new Error(data.detail || res.statusText || "Request failed");
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
const THEMES = ["dark", "light", "apple"];

function currentTheme() {
  if (document.documentElement.classList.contains("dark")) return "dark";
  if (document.documentElement.getAttribute("data-theme") === "apple") return "apple";
  return "light";
}

function applyThemeUI() {
  const t = currentTheme();
  el.themeIconMoon.classList.toggle("hidden", t !== "dark");
  el.themeIconSun.classList.toggle("hidden", t !== "light");
  el.themeIconApple.classList.toggle("hidden", t !== "apple");
  el.themeBtn.title = t === "dark" ? "Theme: dark — click for light"
    : t === "light" ? "Theme: light — click for Apple-like"
    : "Theme: Apple — click for dark";
}

function toggleTheme() {
  const next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
  document.documentElement.classList.toggle("dark", next === "dark");
  if (next === "apple") document.documentElement.setAttribute("data-theme", "apple");
  else document.documentElement.removeAttribute("data-theme");
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
    loadCategories();
    await loadQueue();
  } catch (e) {
    showAuth({ credentials_found: true });
  }
}

async function loadQueue() {
  await showLoading("Pulling latest unread inbox…");
  try {
    const [data, skippedRes, blockedRes, promoRes] = await Promise.all([
      api(`/api/queue?max_results=${BATCH}`),
      api("/api/skipped").catch(() => ({ items: [] })),
      api("/api/blocked").catch(() => ({ items: [] })),
      api("/api/promo-blocked").catch(() => ({ items: [] })),
    ]);
    const skipped = skippedRes.items || [];
    state.skippedIds = new Set(skipped.map((i) => i.id));
    state.skippedItems = skipped;
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
    state.blockedSenders = blockedRes.items || [];
    state.promoBlockedSenders = promoRes.items || [];
    showMain();
    if (state.queue.length === 0) { showEmpty(); return; }
    await renderAll(true);
    if (data.auto_deleted) {
      toast(`Auto-deleted ${data.auto_deleted} email${data.auto_deleted === 1 ? "" : "s"} from blocked / promo-deleted senders`, "info", { duration: 3000 });
    }
  } catch (e) {
    el.loadingText.textContent = `Failed to load queue: ${e.message}`;
    setTimeout(loadQueue, 2500);
  }
}

async function loadMore() {
  if (state.busy) return;
  if (!state.nextPageToken) { toast("No more unread emails in the inbox", "info", { duration: 1800 }); return; }
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
    if (state.queue.length === 0) { showEmpty(); return; }
    renderGroupView();
    toast(`Loaded ${added.length} more · in queue: ${state.queue.length}`, "ok", { duration: 2000 });
    if (data.auto_deleted) {
      toast(`Auto-deleted ${data.auto_deleted} more from blocked / promo-deleted senders`, "info", { duration: 2600 });
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
  if (state.expandedCardId === d.id || visibleIds.has(d.id)) renderGroupView();
}

/* Summarize ungrouped ("singles") emails in the background so their
   cards populate without needing a manual expand. Bounded concurrency. */
let singleSummQueue = [];
let singleSummInFlight = 0;
let singleSummTimer = null;
const SINGLE_SUMM_CONCURRENCY = 2;

function scheduleSingleSummarize(ids) {
  for (const id of ids) {
    if (state.detail.has(id) || state.fetching.has(id) || singleSummQueue.includes(id)) continue;
    const it = state.queue.find((x) => x.id === id);
    if (it && it.summary && it.summary.one_liner) continue;
    singleSummQueue.push(id);
  }
  if (singleSummQueue.length > 200) singleSummQueue.length = 200;
  if (!singleSummTimer) {
    singleSummTimer = setTimeout(bumpSingleSummarize, 400);
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
      bumpSingleSummarize();
    });
  }
}

/* ───────────────────────────── Rendering ───────────────────────── */
async function renderAll(initial = false) {
  if (initial && !state.autoSelectedOnce) {
    state.autoSelectedOnce = true;
    const picked = pickDefaultGroup();
    if (picked) { selectGroup(picked); return; }
  }
  renderSidebar();
  renderGroupView();
  renderCategoryChips();
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
  if (state.activeGroupKey === null) {
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

  const nBlock = state.blockedSenders.length;
  if (nBlock) {
    el.blockedCount.textContent = nBlock;
    el.blockedBtn.classList.remove("hidden");
    el.blockedList.innerHTML = state.blockedSenders.map((b) => `
      <li class="flex items-center gap-2 text-[11px]">
        <span class="min-w-0 flex-1 truncate text-red-600 dark:text-red-300 font-semibold">${esc(b.sender_name || b.email)}</span>
        <span class="max-w-[40%] truncate text-[9px] text-slate-400 dark:text-slate-500">${esc(b.email)}</span>
        <button data-unblock="${esc(b.email)}" class="shrink-0 rounded bg-slate-500/15 px-1.5 py-0.5 text-[10px] font-bold text-slate-600 dark:text-slate-300 hover:bg-slate-500/30 transition">unblock</button>
      </li>`).join("");
    el.blockedList.classList.toggle("hidden", !state.showBlockedList);
  } else {
    el.blockedBtn.classList.add("hidden");
    el.blockedList.classList.add("hidden");
  }

  const nPromo = state.promoBlockedSenders.length;
  if (nPromo) {
    el.promoBlockedCount.textContent = nPromo;
    el.promoBlockedBtn.classList.remove("hidden");
    el.promoBlockedList.innerHTML = state.promoBlockedSenders.map((b) => `
      <li class="flex items-center gap-2 text-[11px]">
        <span class="min-w-0 flex-1 truncate text-amber-700 dark:text-amber-300 font-semibold">${esc(b.sender_name || b.email)}</span>
        <span class="max-w-[40%] truncate text-[9px] text-slate-400 dark:text-slate-500">${esc(b.email)}</span>
        <button data-unpromoblock="${esc(b.email)}" class="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-bold text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 transition">off</button>
      </li>`).join("");
    el.promoBlockedList.classList.toggle("hidden", !state.showPromoBlockedList);
  } else {
    el.promoBlockedBtn.classList.add("hidden");
    el.promoBlockedList.classList.add("hidden");
  }

  el.loadMoreBtn.classList.toggle("hidden", !state.nextPageToken);

  renderPendingOps();
  renderGroups();
  renderCategoryChips();
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

/* ───────────────────────────── Categories ─────────────────────── */
function normalizeCategoryList(data) {
  const list = Array.isArray(data) ? data : (data && data.items) || (data && data.categories) || [];
  return [...new Set(list.map(String).filter(Boolean))];
}

async function loadCategories() {
  try {
    const data = await api("/api/categories", { method: "GET" });
    state.categories = normalizeCategoryList(data);
    renderCategoryChips();
  } catch (e) { /* already toasted by api(); keep state.categories as-is */ }
}

function renderCategoryChips() {
  const countFor = (cat) => state.queue.filter((it) => emailCategory(it) === cat).length;
  const chipCls = (active) =>
    `rounded-lg border px-2 py-1 text-[10px] font-bold transition cursor-pointer select-none flex items-center gap-1 ` +
    (active
      ? `border-violet-500/60 bg-violet-500/10 text-violet-800 dark:text-violet-200`
      : `border-slate-200 bg-white text-slate-700 hover:bg-slate-100 dark:border-slate-800 dark:bg-slate-900/60 dark:text-slate-300 dark:hover:bg-slate-800/70`);

  let html = `
    <button type="button" data-cat="all" class="${chipCls(state.activeCategory === "all")}">
      All <span class="text-[10px] opacity-70">${state.queue.length}</span>
    </button>`;

  for (const cat of state.categories) {
    const n = countFor(cat);
    html += `
      <button type="button" data-cat="${esc(cat)}" class="${chipCls(state.activeCategory === cat)}">
        <span class="truncate max-w-[140px]">${esc(cat)}</span>
        <span class="text-[10px] opacity-70">${n}</span>
        <span data-catremove="${esc(cat)}" class="ml-0.5 rounded px-0.5 text-[11px] leading-none text-slate-400 hover:text-red-500 hover:bg-red-500/10" title="Remove category">×</span>
      </button>`;
  }

  el.categoryChips.innerHTML = html;
  el.categoriesCount.textContent = state.categories.length;
}

async function addCategory() {
  const name = el.categoryInput.value.trim();
  if (!name) return;
  try {
    const data = await api("/api/categories/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    state.categories = normalizeCategoryList(data);
    el.categoryInput.value = "";
    toast("Category added", "ok", { duration: 1800 });
    renderCategoryChips();
  } catch (e) { /* api() already toasted */ }
}

async function removeCategory(name) {
  try {
    const data = await api("/api/categories/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    state.categories = normalizeCategoryList(data);
    if (state.activeCategory === name) state.activeCategory = "all";
    renderCategoryChips();
    renderGroupView();
    toast(`Removed category: ${name}`, "info", { duration: 2000 });
  } catch (e) { /* api() already toasted */ }
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

function renderGroups() {
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
  return `
    <li class="group-row rounded-lg border ${active ? "border-violet-500/60 bg-violet-500/10" : "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900/60"} px-3 py-2 flex items-center gap-2 cursor-pointer select-none transition hover:bg-slate-100 dark:hover:bg-slate-800/70" data-group="${esc(g.key)}"${active ? ` data-active="yes"` : ""}>
      <p class="min-w-0 flex-1 text-[13px] font-semibold ${active ? "text-violet-800 dark:text-violet-200" : "text-slate-800 dark:text-slate-200"} truncate">${esc(g.label)}</p>
      ${promo}
      <span class="shrink-0 rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-bold text-violet-700 dark:text-violet-300">${g.ids.length}</span>
    </li>`;
}

async function fetchGroup(key, g) {
  if (state.groupCache.has(key) || state.groupFetching.has(key)) return;
  state.groupFetching.add(key);
  renderGroupView();
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
  renderGroupView();
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
  renderSidebar();
  renderGroupView();
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
  let visible = state.visibleEmails;
  if (state.activeCategory !== "all") {
    visible = visible.filter((it) => emailCategory(it) === state.activeCategory);
  }
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
    : `<p class="text-sm text-slate-500 dark:text-slate-600 p-6">${state.activeCategory !== "all" ? "No emails match this category." : "This group is empty now."}</p>`;

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
    ? `<span class="shrink-0 rounded bg-violet-500/15 px-1.5 py-0.5 text-[9px] font-bold text-violet-700 dark:text-violet-300">${esc(cat)}</span>` : "";
  const promoBadge = it.promo
    ? `<span class="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-bold text-amber-700 dark:text-amber-300">PROMO</span>` : "";

  let summaryHTML;
  if (sum.one_liner) {
    const bullets = (sum.bullets || []).length
      ? `<ul class="mt-1 space-y-0.5 text-[11px] text-slate-500 dark:text-slate-500">${sum.bullets.map((b) => `<li class="flex gap-1.5"><span class="text-violet-500">▸</span><span>${esc(b)}</span></li>`).join("")}</ul>` : "";
    summaryHTML = `<div class="mt-1.5 flex items-center gap-2 flex-wrap"><p class="text-xs text-slate-600 dark:text-slate-400">${esc(sum.one_liner)}</p>${tokenPill(sum)}</div>${bullets}`;
  } else {
    summaryHTML = `<div class="mt-2 shimmer h-4 w-full"></div>`;
  }

  const previewHTML = it.preview
    ? `<p class="mt-1 text-[11px] text-slate-500 dark:text-slate-500 line-clamp-3">${esc(it.preview)}</p>` : "";

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
    <button data-cardact="trash" data-id="${esc(it.id)}" class="rounded bg-red-500/15 px-2 py-1 text-[10px] font-bold text-red-700 dark:text-red-300 hover:bg-red-500/30 transition">Delete</button>
    <button data-cardact="star" data-id="${esc(it.id)}" class="rounded bg-amber-500/15 px-2 py-1 text-[10px] font-bold text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 transition">Star</button>
    <button data-cardact="skip" data-id="${esc(it.id)}" class="rounded bg-slate-500/15 px-2 py-1 text-[10px] font-bold text-slate-600 dark:text-slate-300 hover:bg-slate-500/30 transition">Skip</button>
    <button data-cardact="keep" data-id="${esc(it.id)}" class="rounded bg-slate-500/15 px-2 py-1 text-[10px] font-bold text-slate-600 dark:text-slate-300 hover:bg-slate-500/30 transition">Keep</button>
    <button data-cardact="block" data-id="${esc(it.id)}" data-email="${esc(it.sender_email || "")}" data-name="${esc(sender)}" class="rounded bg-red-500/15 px-2 py-1 text-[10px] font-bold text-red-700 dark:text-red-300 hover:bg-red-500/30 transition">Block</button>
    ${it.promo ? `<button data-cardact="promoblock" data-id="${esc(it.id)}" data-email="${esc(it.sender_email || "")}" data-name="${esc(sender)}" class="rounded bg-amber-500/15 px-2 py-1 text-[10px] font-bold text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 transition">Auto-del promos</button>` : ""}`;

  return `
    <div class="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/60 p-3 transition group-card ${politics ? "opacity-60" : ""}" data-expand="${esc(it.id)}" data-expanded="${expanded}">
      <div class="flex items-center gap-2 flex-wrap cursor-pointer" data-expand="${esc(it.id)}">
        ${categoryChip} ${promoBadge}
        <p class="min-w-0 flex-1 text-sm font-bold text-slate-800 dark:text-slate-200 truncate">${esc(it.subject || "(no subject)")}</p>
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

function detailHTML(it) {
  const d = state.detail.get(it.id);
  if (!d) fetchDetail(it.id);
  const sum = (d && d.summary) || null;
  const sender = (d && d.sender_email) || it.sender_email || "";
  const dateMs = (d && d.internal_date_ms) || it.internal_date_ms;
  const date = dateMs ? timeAgo(new Date(dateMs).toISOString()) : "";
  const bodyHTML = !d
    ? `<div class="mt-2 shimmer h-4 w-full"></div><div class="mt-2 shimmer h-4 w-3/4"></div><div class="mt-2 shimmer h-4 w-5/6"></div>`
    : d.body_text
      ? `<div class="email-body mt-2 text-sm text-slate-700 dark:text-slate-300">${linkify(d.body_text)}</div>${d.body_truncated ? `<p class="mt-2 text-xs text-amber-600 dark:text-amber-400/80">Body truncated at 80k chars for speed.</p>` : ""}`
      : `<div class="email-body mt-2 text-sm text-slate-700 dark:text-slate-300">(no extractable text)</div>`;
  const sumBlock = sum && sum.one_liner
    ? `<div class="mt-2.5 rounded-lg bg-violet-500/10 border border-violet-500/25 px-3 py-2 text-xs text-violet-900 dark:text-violet-100">
        <span class="font-bold text-violet-700 dark:text-violet-300">AI summary</span>
        <p class="mt-0.5 text-violet-900/90 dark:text-violet-100/90">${esc(sum.one_liner)}</p>
        ${sum.bullets && sum.bullets.length ? `<ul class="mt-1 space-y-0.5 text-[11px] text-violet-900/70 dark:text-violet-100/70">${sum.bullets.map((b) => `<li class="flex gap-1.5"><span class="text-violet-500">▸</span><span>${esc(b)}</span></li>`).join("")}</ul>` : ""}
      </div>` : "";
  return `
    <div class="mt-3 border-t border-slate-200 dark:border-slate-800 pt-3">
      <div class="flex items-center gap-2 flex-wrap text-[11px] text-slate-500 dark:text-slate-400">
        <span class="font-mono truncate">${esc(sender)}</span>
        <span>·</span>
        <span>${esc(date)}</span>
        ${tokenPill(sum)}
      </div>
      ${sumBlock}
      ${bodyHTML}
      <div class="mt-2 flex items-center gap-1.5">
        <button data-cardact="block" data-id="${esc(it.id)}" data-email="${esc(it.sender_email || "")}" data-name="${esc(sender)}" class="rounded bg-red-500/15 px-2 py-1 text-[10px] font-bold text-red-700 dark:text-red-300 hover:bg-red-500/30 transition">Block sender</button>
        ${it.promo ? `<button data-cardact="promoblock" data-id="${esc(it.id)}" data-email="${esc(it.sender_email || "")}" data-name="${esc(sender)}" class="rounded bg-amber-500/15 px-2 py-1 text-[10px] font-bold text-amber-700 dark:text-amber-300 hover:bg-amber-500/30 transition">Auto-del promos</button>` : ""}
      </div>
    </div>`;
}

function currentItemOf(id) { return state.queue.find((i) => i.id === id); }

/* ───────────────────────────── Actions ─────────────────────────── */
async function actOn(action, id) {
  if (state.busy) return;
  const item = state.queue.find((i) => i.id === id);
  if (!item) return;
  state.busy = true;
  try {
    await api(`/api/messages/${id}/${action}`, { method: "POST", body: "{}" });
    pushHistory(action, [id], [item]);
    removeFromQueue(id);
    toast(`${actionLabel(action)} · synced to Gmail`, action === "star" ? "info" : "ok", { undo: true });
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
      renderSidebar();
      renderGroupView();
      toast(`Bulk ${action === "trash" ? "delete" : "archive"} failed — emails restored`, "err", { duration: 4000 });
      scheduleOpRemoval(op);
    });
}

function actionLabel(action) {
  return { trash: "Trashed", archive: "Archived", star: "Starred" }[action] || action;
}

function removeFromQueue(id) {
  state.detail.delete(id);
  const idx = state.queue.findIndex((i) => i.id === id);
  if (idx === -1) return;
  state.queue.splice(idx, 1);
  invalidateGroups();
  if (state.index >= state.queue.length) state.index = Math.max(0, state.queue.length - 1);
  renderSidebar();
  if (state.queue.length === 0) { showEmpty(); return; }
  renderGroupView();
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
    state.detail.delete(item.id);
    const idx = state.queue.findIndex((i) => i.id === item.id);
    if (idx !== -1) state.queue.splice(idx, 1);
    invalidateGroups();
    if (state.index >= state.queue.length) state.index = Math.max(0, state.queue.length - 1);
    renderSidebar();
    if (state.queue.length === 0) { showEmpty(); return; }
    renderGroupView();
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
  renderSidebar();
  if (state.queue.length === 0) { showEmpty(); return; }
  renderGroupView();
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
  renderSidebar();
  if (state.queue.length === 0) { showEmpty(); return; }
  renderGroupView();
  toast(`Brought back ${items.length} skipped email${items.length === 1 ? "" : "s"}`, "ok", { duration: 2200 });
}

async function blockSender(email, senderName) {
  if (state.busy) return;
  state.busy = true;
  const norm = (email || "").toLowerCase();
  try {
    const res = await api("/api/blocked/add", {
      method: "POST", body: JSON.stringify({ sender_email: norm, sender_name: senderName }),
    });
    if (!state.blockedSenders.some((b) => b.email === norm)) {
      state.blockedSenders.push({ email: norm, sender_name: senderName || "" });
    }
    // The server just trashed all unread mail from this sender; drop it locally too.
    const doomedIds = new Set(state.queue.filter((i) => (i.sender_email || "").toLowerCase() === norm).map((i) => i.id));
    doomedIds.forEach((id) => state.detail.delete(id));
    state.queue = state.queue.filter((i) => !doomedIds.has(i.id));
    state.skippedItems = state.skippedItems.filter((i) => (i.sender_email || "").toLowerCase() !== norm);
    state.skippedIds = new Set(state.skippedItems.map((i) => i.id));
    if (state.index >= state.queue.length) state.index = Math.max(0, state.queue.length - 1);
    invalidateGroups();
    renderSidebar();
    const purged = res.purged || 0;
    toast(`Blocked ${senderName || norm} · purged ${purged} unread, auto-deletes the rest forever`, "ok", { duration: 3800 });
    if (state.queue.length === 0) { showEmpty(); return; }
    renderGroupView();
  } catch (e) {
    toast(`Block failed: ${e.message}`, "err", { duration: 4000 });
  } finally {
    state.busy = false;
  }
}

async function unblockSender(email) {
  try {
    await api("/api/blocked/remove", { method: "POST", body: JSON.stringify({ sender_email: email }) });
  } catch (e) {
    toast(`Unblock failed: ${e.message}`, "err", { duration: 3000 });
    return;
  }
  state.blockedSenders = state.blockedSenders.filter((b) => b.email !== email);
  renderSidebar();
  toast("Unblocked — matches from this sender will appear again", "info", { duration: 2400 });
}

async function promoBlockSender(email, senderName) {
  if (state.busy) return;
  state.busy = true;
  const norm = (email || "").toLowerCase();
  if (!norm) { toast("This email has no sender address to mark", "err", { duration: 2200 }); state.busy = false; return; }
  try {
    const res = await api("/api/promo-blocked/add", {
      method: "POST", body: JSON.stringify({ sender_email: norm, sender_name: senderName }),
    });
    if (!state.promoBlockedSenders.some((b) => b.email === norm)) {
      state.promoBlockedSenders.push({ email: norm, sender_name: senderName || "" });
    }
    // The server trashed the vendor's currently-unread PROMO mail; drop those locally.
    const doomedIds = new Set(state.queue.filter((i) =>
      (i.sender_email || "").toLowerCase() === norm && i.promo
    ).map((i) => i.id));
    doomedIds.forEach((id) => state.detail.delete(id));
    state.queue = state.queue.filter((i) => !doomedIds.has(i.id));
    state.skippedItems = state.skippedItems.filter((i) =>
      (i.sender_email || "").toLowerCase() !== norm || !i.promo
    );
    state.skippedIds = new Set(state.skippedItems.map((i) => i.id));
    if (state.index >= state.queue.length) state.index = Math.max(0, state.queue.length - 1);
    invalidateGroups();
    renderSidebar();
    const purged = res.purged || 0;
    const skippedN = res.skipped || 0;
    toast(`Promo auto-delete ON for ${senderName || norm} · purged ${purged} promos now, future promos deleted (${skippedN} non-promo kept)`, "ok", { duration: 4200 });
    if (state.queue.length === 0) { showEmpty(); return; }
    renderGroupView();
  } catch (e) {
    toast(`Promo auto-delete failed: ${e.message}`, "err", { duration: 4000 });
  } finally {
    state.busy = false;
  }
}

async function unpromoBlockSender(email) {
  try {
    await api("/api/promo-blocked/remove", { method: "POST", body: JSON.stringify({ sender_email: email }) });
  } catch (e) {
    toast(`Failed to disable promo auto-delete: ${e.message}`, "err", { duration: 3000 });
    return;
  }
  state.promoBlockedSenders = state.promoBlockedSenders.filter((b) => b.email !== email);
  renderSidebar();
  toast("Promo auto-delete OFF — promos from this vendor will appear again", "info", { duration: 2400 });
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
    renderSidebar();
    renderGroupView();
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
  const it = currentTargetItem();
  if (it && it.sender_email) blockSender(it.sender_email, it.sender_name);
  else toast("This email has no sender address to block", "err", { duration: 2200 });
}
function issuePromoBlock() {
  const it = currentTargetItem();
  if (!it || !it.sender_email) { toast("This email has no sender address to mark", "err", { duration: 2200 }); return; }
  if (!it.promo) { toast("Only promo emails can be marked for auto-delete (use Block for the whole sender)", "err", { duration: 2600 }); return; }
  promoBlockSender(it.sender_email, it.sender_name);
}
function groupKeys() {
  const keys = [];
  const singleCount = state.queue.filter((i) => (i.bundle_count || 1) <= 1).length;
  if (singleCount) keys.push("singles");
  currentGroups().forEach((g) => keys.push(g.key));
  return keys;
}
function stepGroup(dir) {
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
    case "escape":
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
el.blockedBtn.addEventListener("click", () => { state.showBlockedList = !state.showBlockedList; renderSidebar(); });
el.promoBlockedBtn.addEventListener("click", () => { state.showPromoBlockedList = !state.showPromoBlockedList; renderSidebar(); });
applyThemeUI();
el.bulkDeleteBtn.addEventListener("click", () => { if (state.activeGroupKey && state.activeGroupKey !== "singles") queueBulk("trash", state.activeGroupKey); else toast("Open a group to bulk delete", "info", { duration: 1600 }); });
el.bulkArchiveBtn.addEventListener("click", () => { if (state.activeGroupKey && state.activeGroupKey !== "singles") queueBulk("archive", state.activeGroupKey); else toast("Open a group to bulk archive", "info", { duration: 1600 }); });
el.groupList.addEventListener("click", (e) => {
  const t = e.target.closest("[data-group]");
  if (t) selectGroup(t.dataset.group);
});

el.categoryAddBtn.addEventListener("click", addCategory);
el.categoryInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); addCategory(); }
});
el.categoryChips.addEventListener("click", (e) => {
  const rm = e.target.closest("[data-catremove]");
  if (rm) { removeCategory(rm.dataset.catremove); return; }
  const chip = e.target.closest("[data-cat]");
  if (chip) {
    state.activeCategory = chip.dataset.cat;
    renderCategoryChips();
    renderGroupView();
  }
});

el.emailCards.addEventListener("click", (e) => {
  const t = e.target.closest("[data-expand]");
  if (!t || e.target.closest("button")) return;
  const id = t.dataset.expand;
  state.expandedCardId = state.expandedCardId === id ? null : id;
  renderGroupView();
});

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-cardact]");
  if (!b) return;
  const id = b.dataset.id;
  const act = b.dataset.cardact;
  if (act === "skip") skipForNowById(id);
  else if (act === "block") blockSender(b.dataset.email || (currentItemOf(id) || {}).sender_email || "", b.dataset.name);
  else if (act === "promoblock") promoBlockSender(b.dataset.email || (currentItemOf(id) || {}).sender_email || "", b.dataset.name);
  else if (act === "keep") keepEmail(id);
  else actOn(act, id);
});

document.addEventListener("click", (e) => {
  const bs = e.target.closest("[data-blocksender]");
  if (bs) { blockSender(bs.dataset.blocksender, bs.dataset.blocksendername); return; }
  const ub = e.target.closest("[data-unblock]");
  if (ub) { unblockSender(ub.dataset.unblock); return; }
  const upb = e.target.closest("[data-unpromoblock]");
  if (upb) { unpromoBlockSender(upb.dataset.unpromoblock); }
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