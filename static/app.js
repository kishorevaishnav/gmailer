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
};

/* ───────────────────────────── Elements ────────────────────────── */
const $ = (id) => document.getElementById(id);

const el = {
  authScreen: $("authScreen"), authBtn: $("authBtn"), authMissingCreds: $("authMissingCreds"), authError: $("authError"), authErrorText: $("authErrorText"),
  loadingScreen: $("loadingScreen"), loadingText: $("loadingText"),
  app: $("app"),
  remainCount: $("remainCount"), remainSub: $("remainSub"),
  progressFill: $("progressFill"), queueMeta: $("queueMeta"),
  upNext: $("upNext"), historyList: $("historyList"),
  groupList: $("groupList"), groupsCount: $("groupsCount"),
  cacheInfo: $("cacheInfo"),
  skippedBtn: $("skippedBtn"), skippedCount: $("skippedCount"), skippedRestoreBtn: $("skippedRestoreBtn"),
  skipForNowBtn: $("skipForNowBtn"),
  blockedBtn: $("blockedBtn"), blockedCount: $("blockedCount"), blockedList: $("blockedList"),
  loadMoreBtn: $("loadMoreBtn"),
  userChip: $("userChip"), logoutBtn: $("logoutBtn"),
  themeBtn: $("themeBtn"), themeIconMoon: $("themeIconMoon"), themeIconSun: $("themeIconSun"),
  position: $("position"), positionTotal: $("positionTotal"),
  undoTopBtn: $("undoTopBtn"), reloadBtn: $("reloadBtn"), clearCacheBtn: $("clearCacheBtn"),
  bundleBanner: $("bundleBanner"), bundleText: $("bundleText"),
  bundleDeleteBtn: $("bundleDeleteBtn"), bundleArchiveBtn: $("bundleArchiveBtn"),
  stage: $("stage"), summaryBox: $("summaryBox"), metaBox: $("metaBox"),
  emailBodyBox: $("emailBodyBox"), toasts: $("toasts"),
  emptyScreen: $("emptyScreen"), emptyStat: $("emptyStat"), emptyReload: $("emptyReload"),
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

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

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
function applyThemeUI() {
  const dark = document.documentElement.classList.contains("dark");
  el.themeIconMoon.classList.toggle("hidden", dark);
  el.themeIconSun.classList.toggle("hidden", !dark);
}

function toggleTheme() {
  const dark = document.documentElement.classList.toggle("dark");
  try { localStorage.setItem("gmailer-theme", dark ? "dark" : "light"); } catch (e) {}
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
  await showLoading("Pulling latest unread inbox…");
  try {
    const [data, skippedRes, blockedRes] = await Promise.all([
      api(`/api/queue?max_results=${BATCH}`),
      api("/api/skipped").catch(() => ({ items: [] })),
      api("/api/blocked").catch(() => ({ items: [] })),
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
    showMain();
    if (state.queue.length === 0) { showEmpty(); return; }
    await renderAll(true);
    if (data.auto_deleted) {
      toast(`Auto-deleted ${data.auto_deleted} email${data.auto_deleted === 1 ? "" : "s"} from blocked senders`, "info", { duration: 3000 });
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
    await showItem();
    toast(`Loaded ${added.length} more · in queue: ${state.queue.length}`, "ok", { duration: 2000 });
    if (data.auto_deleted) {
      toast(`Auto-deleted ${data.auto_deleted} more from blocked senders`, "info", { duration: 2600 });
    }
  } catch (e) {
    el.loadingText.textContent = `Failed to load more: ${e.message}`;
    setTimeout(loadMore, 2500);
  } finally {
    state.busy = false;
  }
}

/* ───────────────────────────── Detail + prefetch ───────────────── */
function currentItem() {
  return state.queue[state.index] || null;
}

async function fetchDetail(id) {
  if (state.detail.has(id) || state.fetching.has(id)) return state.detail.get(id);
  state.fetching.add(id);
  try {
    const d = await api(`/api/messages/${id}`);
    state.detail.set(id, d);
    if (state.renderedId === id) pumpDetail(d);
    return d;
  } catch (e) {
    console.warn("detail fetch failed", id, e.message);
    return null;
  } finally {
    state.fetching.delete(id);
  }
}

async function ensurePreload() {
  for (let i = state.index + 1; i <= state.index + 2; i++) {
    const item = state.queue[i];
    if (item) fetchDetail(item.id);
  }
}

function pumpDetail(d) {
  if (state.renderedId !== d.id) return;
  renderSummary(d);
  renderBody(d);
}

/* ───────────────────────────── Rendering ───────────────────────── */
async function renderAll(initial = false) {
  renderSidebar();
  if (initial) {
    const item = currentItem();
    state.renderedId = null;
    renderStage(item);
    if (item) fetchDetail(item.id);
    ensurePreload();
  } else {
    await showItem();
  }
}

async function showItem() {
  const item = currentItem();
  if (!item) {
    if (state.queue.length === 0) { showEmpty(); return; }
    return;
  }
  el.stage.classList.add("slide-out");
  await wait(170);
  renderStage(item);
  el.stage.classList.remove("slide-out");
  el.stage.classList.add("slide-in");
  ensurePreload();
}

function renderStage(item) {
  state.renderedId = item.id;
  renderBundleBanner(item);
  renderSidebar();
  renderMeta(item);
  const cached = state.detail.get(item.id);
  if (cached) { renderSummary(cached); renderBody(cached); }
  else {
    if (item.summary) renderSummary({ summary: item.summary });
    else renderShimmerSummary();
    renderShimmerBody();
    fetchDetail(item.id);
  }
}

function renderMeta(item) {
  const sender = item.sender_name || "Unknown sender";
  const addr = item.sender_email ? `<span class="text-slate-600 dark:text-slate-500">${esc(item.sender_email)}</span>` : "";
  const date = item.internal_date_ms ? timeAgo(new Date(item.internal_date_ms).toISOString()) : "";
  const promo = item.promo
    ? `<span class="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-bold text-amber-700 dark:text-amber-300" title="Gmail category: Promotions">PROMO</span>`
    : "";
  el.metaBox.innerHTML = `
    <h2 class="text-4xl font-black tracking-tight leading-tight">${esc(sender)}</h2>
    <div class="mt-1 flex items-baseline gap-3 flex-wrap">
      ${addr}
      <span class="text-slate-600 dark:text-slate-500 text-sm">${esc(date)}</span>
    </div>
    <h3 class="mt-3 text-2xl font-bold text-slate-800 dark:text-slate-200 flex items-center gap-2">${esc(item.subject)} ${promo}</h3>
    ${item.sender_email ? `
    <button data-blocksender="${esc(item.sender_email)}" data-blocksendername="${esc(sender)}"
      class="mt-3 rounded-lg border border-red-500/50 px-2.5 py-1 text-[11px] font-bold text-red-600 dark:text-red-300 transition hover:bg-red-500/10 hover:border-red-500"
      title="Trash all current + future mail from ${esc(item.sender_email)} on every pull">Block — auto-delete all from this sender</button>` : ""}
  `;
}

function renderSummary(d) {
  const s = d.summary || {};
  const latency = (((s.latency_ms || 0) / 1000)).toFixed(1);
  const pill = s.mock
    ? `<span class="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-bold text-amber-700 dark:text-amber-300">OFFLINE FALLBACK</span>`
    : `<span class="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-bold text-emerald-700 dark:text-emerald-300">in ${s.tokens_in || 0} tok · out ${s.tokens_out || 0} tok · ${latency}s</span>
       <span class="text-[10px] text-slate-500 dark:text-slate-400">${esc(s.model || "")}</span>`;
  el.summaryBox.innerHTML = `
    <div class="flex items-center gap-2 mb-2 flex-wrap">
      <svg viewBox="0 0 24 24" class="h-3.5 w-3.5 fill-violet-600 dark:fill-violet-400" aria-hidden="true"><path d="M20 7h-4V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2zM14 5v2h-4V5h4z"/></svg>
      <span class="text-[11px] font-black tracking-[0.2em] text-violet-700 dark:text-violet-300">AI TL;DR SUMMARY</span>
      ${pill}
    </div>
    <p class="text-lg font-semibold text-violet-800 dark:text-violet-100">${esc(s.one_liner || "")}</p>
    <ul class="mt-2 space-y-1 text-sm text-slate-600 dark:text-slate-300">
      ${(s.bullets || []).map((b) => `<li class="flex gap-2"><span class="text-violet-600 dark:text-violet-400">▸</span><span>${esc(b)}</span></li>`).join("")}
    </ul>
  `;
}

function renderBody(d) {
  const body = (d.body_text || "(no extractable text)");
  const truncated = d.body_truncated
    ? `<p class="mt-3 text-xs text-amber-600 dark:text-amber-400/80">Body truncated at 80k chars for speed.</p>` : "";
  el.emailBodyBox.innerHTML = `<div class="email-body">${linkify(body)}</div>${truncated}`;
}

function renderShimmerSummary() {
  el.summaryBox.innerHTML = `
    <div class="flex items-center gap-2 mb-2"><span class="shimmer h-3 w-28"></span></div>
    <div class="shimmer h-5 w-full max-w-lg"></div>
    <div class="mt-2 shimmer h-4 w-full max-w-md"></div>
    <div class="mt-1 shimmer h-4 w-full max-w-sm"></div>
  `;
}

function renderShimmerBody() {
  el.emailBodyBox.innerHTML = `
    <div class="shimmer h-4 w-full"></div>
    <div class="mt-2 shimmer h-4 w-3/4"></div>
    <div class="mt-2 shimmer h-4 w-5/6"></div>
    <div class="mt-2 shimmer h-4 w-2/3"></div>
  `;
}

/* ─────────────── Bundle banner ─────────────── */
function renderBundleBanner(item) {
  const remaining = state.queue.filter((i) => i.bundle_key === item.bundle_key && i.bundle_count > 1).length;
  if (!item.in_bundle || remaining < 2) {
    el.bundleBanner.classList.add("hidden");
    return;
  }
  const name = item.bundle_sender_name || item.sender_name || item.sender_email;
  el.bundleText.textContent = `You have ${remaining} emails from ${name}. Mow them all down:`;
  el.bundleBanner.classList.remove("hidden");
}

/* ───────────────────────────── Sidebar ─────────────────────────── */
function renderSidebar() {
  const remaining = state.queue.length;
  el.remainCount.textContent = remaining;
  el.remainSub.textContent = remaining === 1 ? "email left" : "emails left";
  const pct = state.originalTotal ? Math.round(((state.originalTotal - remaining) / state.originalTotal) * 100) : 0;
  el.progressFill.style.width = `${pct}%`;
  el.queueMeta.textContent = `${pct}% cleared · ${state.originalTotal} in batch`;

  el.position.textContent = Math.min(state.index + 1, state.queue.length);
  el.positionTotal.textContent = state.queue.length;

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

  el.loadMoreBtn.classList.toggle("hidden", !state.nextPageToken);

  const five = [];
  for (let i = state.index; i < state.queue.length && five.length < 5; i++) five.push({ n: i, item: state.queue[i] });

  el.upNext.innerHTML = five.map(({ n, item }) => `
    <li class="up-next-row ${n === state.index ? "current" : ""} rounded-lg px-2 py-1.5 flex items-start gap-2">
      <span class="text-xs text-slate-500 dark:text-slate-600 font-mono tabular-nums mt-0.5 w-5 text-right">${n + 1}</span>
      <div class="min-w-0 flex-1">
        <p class="text-[13px] font-semibold text-slate-800 dark:text-slate-200 truncate">${esc(item.sender_name || item.sender_email)}</p>
        <p class="text-xs text-slate-600 dark:text-slate-500 truncate">${esc(item.subject)}</p>
      </div>
      ${item.bundle_count > 1 ? `<span class="shrink-0 rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-bold text-violet-700 dark:text-violet-300">×${item.bundle_count}</span>` : ""}
    </li>
  `).join("") || `<li class="text-xs text-slate-500 dark:text-slate-600">End of queue</li>`;

  renderHistory();
  renderGroups();
}

function renderHistory() {
  el.historyList.innerHTML = state.history.slice(0, 3).map((h, i) => `
    <div class="rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900/60 px-3 py-2 flex items-center gap-2">
      <span class="text-xs text-slate-600 dark:text-slate-400 flex-1 truncate">${esc(h.label)}</span>
      <button data-undo="${i}" class="rounded bg-violet-500/15 px-2 py-1 text-[10px] font-bold text-violet-700 dark:text-violet-300 hover:bg-violet-500/30 transition">Undo</button>
    </div>
  `).join("") || `<p class="text-xs text-slate-500 dark:text-slate-600">No actions yet. D to delete, E to archive.</p>`;

  el.historyList.querySelectorAll("[data-undo]").forEach((btn) => {
    btn.addEventListener("click", () => undoIndex(parseInt(btn.dataset.undo, 10)));
  });
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

function renderGroups() {
  const groups = currentGroups();
  el.groupsCount.textContent = groups.length
    ? `${groups.length} sender${groups.length === 1 ? "" : "s"}` : "";
  const sig = JSON.stringify(groups.map((g) => [g.key, g.ids.join(",")]));
  if (sig !== state.groupsSig) {
    state.groupsSig = sig;
    el.groupList.innerHTML = groups.length
      ? groups.map(groupRow).join("")
      : `<li class="text-xs text-slate-500 dark:text-slate-600">No repeat senders yet.</li>`;
  }
  groups.forEach((g) => { if (state.expandedGroups.has(g.key)) paintGroup(g.key); });
}

function groupRow(g) {
  const open = state.expandedGroups.has(g.key);
  return `
    <li class="rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900/60">
      <div class="flex items-center gap-2 px-3 py-2 cursor-pointer select-none" data-toggle="${esc(g.key)}" title="Show summaries">
        <svg viewBox="0 0 16 16" class="h-3.5 w-3.5 shrink-0 text-slate-500 dark:text-slate-400 transition-transform ${open ? "rotate-90" : ""}" id="chev_${g.key}"><path fill="currentColor" d="M6 4l4 4-4 4z"/></svg>
        <p class="min-w-0 flex-1 text-[13px] font-semibold text-slate-800 dark:text-slate-200 truncate">${esc(g.label)}</p>
        <span class="shrink-0 rounded bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-bold text-violet-700 dark:text-violet-300">×${g.ids.length}</span>
      </div>
      <div id="grp_${g.key}" class="px-3 pb-2.5 space-y-2 ${open ? "" : "hidden"}"></div>
    </li>`;
}

function paintGroup(key) {
  const g = currentGroups().find((x) => x.key === key);
  const chev = document.getElementById(`chev_${key}`);
  const box = document.getElementById(`grp_${key}`);
  if (chev) chev.classList.toggle("rotate-90", state.expandedGroups.has(key));
  if (!box) return;
  if (!state.expandedGroups.has(key)) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  const cache = state.groupCache.get(key);
  if (!cache || !cache.summaries) {
    box.innerHTML = `<div class="px-1 py-1 text-[11px] text-slate-500 dark:text-slate-500 animate-pulse">Summarizing ${g ? g.ids.length : ""} emails…</div>`;
    if (g) fetchGroup(key, g);
    return;
  }
  box.innerHTML = (cache.summaries || []).map(groupEmailRow).join("") + `
    ${cache.truncated ? `<div class="px-1 text-[10px] text-slate-500 dark:text-slate-600">Only the first 15 emails of this group are shown.</div>` : ""}
    <button data-del="${esc(key)}" class="w-full rounded-lg bg-red-500/85 px-2 py-1.5 text-[11px] font-bold text-white hover:bg-red-500 transition">
      Delete all ${g ? g.ids.length : cache.count} emails from sender
    </button>`;
  box.querySelectorAll("[data-goto]").forEach((r) => r.addEventListener("click", () => gotoEmail(r.dataset.goto)));
  box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    const id = b.dataset.id;
    if (b.dataset.act === "keep") keepEmail(id);
    else actOn(b.dataset.act, id);
  }));
  box.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", (e) => { e.stopPropagation(); bulkAction("trash", b.dataset.del); }));
}

function groupEmailRow(s) {
  const promo = s.promo
    ? `<span class="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-[9px] font-bold text-amber-700 dark:text-amber-300">PROMO</span>`
    : "";
  const preview = s.preview
    ? `<p class="mt-1 text-[11px] text-slate-500 dark:text-slate-500 line-clamp-3">${esc(s.preview)}</p>`
    : "";
  return `
    <div class="rounded-lg bg-slate-100 dark:bg-slate-800/60 px-2.5 py-2 transition hover:bg-slate-200 dark:hover:bg-slate-700/60 group-row">
      <div class="flex items-center gap-2 cursor-pointer" data-goto="${esc(s.id)}">
        <p class="min-w-0 flex-1 text-[11px] font-bold text-slate-700 dark:text-slate-300 truncate">${esc(s.subject || "(no subject)")}</p>
        ${promo}
      </div>
      <div class="mt-1 cursor-pointer" data-goto="${esc(s.id)}">
        <p class="text-xs text-slate-600 dark:text-slate-400">${esc(s.one_liner || "")}</p>
        <ul class="mt-1 space-y-0.5 text-[11px] text-slate-500 dark:text-slate-500">
          ${(s.bullets || []).map((b) => `<li class="flex gap-1.5"><span class="text-violet-500">▸</span><span>${esc(b)}</span></li>`).join("")}
        </ul>
        ${preview}
      </div>
      <div class="mt-2 flex items-center gap-1.5">
        <button data-act="archive" data-id="${esc(s.id)}" class="rounded bg-emerald-500/15 px-2 py-1 text-[10px] font-bold text-emerald-700 dark:text-emerald-300 hover:bg-emerald-500/30 transition">Archive</button>
        <button data-act="trash" data-id="${esc(s.id)}" class="rounded bg-red-500/15 px-2 py-1 text-[10px] font-bold text-red-700 dark:text-red-300 hover:bg-red-500/30 transition">Delete</button>
        <button data-act="keep" data-id="${esc(s.id)}" class="rounded bg-slate-500/15 px-2 py-1 text-[10px] font-bold text-slate-600 dark:text-slate-300 hover:bg-slate-500/30 transition">Keep</button>
        <span class="ml-auto text-[9px] text-slate-400 dark:text-slate-600">click to open</span>
      </div>
    </div>`;
}

async function fetchGroup(key, g) {
  if (state.groupCache.has(key) || state.groupFetching.has(key)) return;
  state.groupFetching.add(key);
  try {
    const res = await api(`/api/groups/${encodeURIComponent(key)}/summarize`, {
      method: "POST",
      body: JSON.stringify({ message_ids: g.ids, label: g.label }),
    });
    state.groupCache.set(key, res);
  } catch (e) {
    toast(`Group summary failed: ${e.message}`, "err", { duration: 4000 });
  } finally {
    state.groupFetching.delete(key);
  }
  paintGroup(key);
}

function toggleGroup(key) {
  if (state.expandedGroups.has(key)) state.expandedGroups.delete(key);
  else state.expandedGroups.add(key);
  renderGroups();
}

function gotoEmail(id) {
  const idx = state.queue.findIndex((i) => i.id === id);
  if (idx === -1) { toast("That email is no longer in the queue", "err", { duration: 2000 }); return; }
  state.index = idx;
  showItem();
}

/* ───────────────────────────── Actions ─────────────────────────── */
function doAction(action) {
  const item = currentItem();
  if (item) actOn(action, item.id);
}

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
  showItem();
}

async function bulkAction(action, bundleKey) {
  if (state.busy) return;
  const items = state.queue.filter((i) => i.bundle_key === bundleKey && i.bundle_count > 1);
  if (!items.length) return;
  state.busy = true;
  const ids = items.map((i) => i.id);
  try {
    const res = await api(`/api/bundles/${encodeURIComponent(bundleKey)}/${action}`, {
      method: "POST",
      body: JSON.stringify({ message_ids: ids }),
    });
    ids.forEach((id) => state.detail.delete(id));
    state.queue = state.queue.filter((i) => !ids.includes(i.id));
    invalidateGroups();
    if (state.index >= state.queue.length) state.index = Math.max(0, state.queue.length - 1);
    const failedN = (res.failed || []).length;
    if (failedN) toast(`${failedN} failed — retry`, "err", { duration: 4000 });
    pushHistory(action, ids.filter((id) => !(res.failed || []).includes(id)), items);
    toast(`${actionLabel(action)} ${ids.length} emails · synced to Gmail`, "ok", { undo: true });
    if (state.queue.length === 0) { showEmpty(); return; }
    await showItem();
  } catch (e) {
    toast(`Bulk ${action} failed: ${e.message}`, "err", { duration: 4000 });
  } finally {
    state.busy = false;
  }
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
  showItem();
}

function invalidateGroups() {
  state.groupsSig = null;
  state.groupCache.clear();
}

function skip(delta) {
  const next = state.index + delta;
  if (next < 0 || next >= state.queue.length) { toast(delta > 0 ? "End of queue" : "Already at start", "info", { duration: 1200 }); return; }
  state.index = next;
  showItem();
}

async function skipForNow() {
  if (state.busy) return;
  const item = currentItem();
  if (!item) return;
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
    if (state.queue.length === 0) { renderSidebar(); showEmpty(); return; }
    showItem();
    toast("Skipped for now — hidden until you bring it back", "info", {
      undo: true, undoFn: () => restoreSkipped(item.id), duration: 3600,
    });
  } catch (e) {
    toast(`Skip failed: ${e.message}`, "err", { duration: 4000 });
  } finally {
    state.busy = false;
  }
}

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
  showItem();
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
  showItem();
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
    showItem();
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
    await showItem();
    toast(`Restored ${restoredItems.length} email${restoredItems.length === 1 ? "" : "s"} · synced to Gmail`, "ok");
  } catch (e) {
    toast(`Undo failed: ${e.message}`, "err", { duration: 4000 });
  } finally {
    state.busy = false;
  }
}

/* ───────────────────────────── Keyboard ────────────────────────── */
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
      e.preventDefault(); skip(1); break;
    case "arrowleft":
      e.preventDefault(); skip(-1); break;
    case "x":
      e.preventDefault(); skipForNow(); break;
    case "b":
      e.preventDefault();
      { const it = currentItem(); if (it && it.sender_email) blockSender(it.sender_email, it.sender_name); else toast("This email has no sender address to block", "err", { duration: 2200 }); }
      break;
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
el.skipForNowBtn.addEventListener("click", skipForNow);
el.skippedBtn.addEventListener("click", restoreAllSkipped);
el.skippedRestoreBtn.addEventListener("click", restoreAllSkipped);
el.blockedBtn.addEventListener("click", () => { state.showBlockedList = !state.showBlockedList; renderSidebar(); });
applyThemeUI();
el.bundleDeleteBtn.addEventListener("click", () => { const i = currentItem(); if (i) bulkAction("trash", i.bundle_key); });
el.bundleArchiveBtn.addEventListener("click", () => { const i = currentItem(); if (i) bulkAction("archive", i.bundle_key); });
el.groupList.addEventListener("click", (e) => {
  const t = e.target.closest("[data-toggle]");
  if (t) toggleGroup(t.dataset.toggle);
});

document.addEventListener("click", (e) => {
  const bs = e.target.closest("[data-blocksender]");
  if (bs) { blockSender(bs.dataset.blocksender, bs.dataset.blocksendername); return; }
  const ub = e.target.closest("[data-unblock]");
  if (ub) { unblockSender(ub.dataset.unblock); }
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