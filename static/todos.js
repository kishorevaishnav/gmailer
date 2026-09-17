"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const el = {
  todoList: $("todoList"), todoEmpty: $("todoEmpty"), todoCount: $("todoCount"),
  refreshBtn: $("refreshBtn"), toasts: $("toasts"),
};

const state = {
  items: [],
  sort: localStorage.getItem("gmailer-todo-sort") || "manual",
};

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : res.statusText || "Request failed");
  return data;
}

function toast(msg, tone = "info") {
  const t = document.createElement("div");
  t.className = "rounded-xl border px-4 py-2 text-sm shadow-xl " + (tone === "err"
    ? "bg-red-50 border-red-300 text-red-700 dark:bg-red-950 dark:border-red-700 dark:text-red-100"
    : tone === "ok"
      ? "bg-emerald-50 border-emerald-300 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-700 dark:text-emerald-100"
      : "bg-white dark:bg-slate-900 border-border text-foreground");
  t.textContent = msg;
  el.toasts.appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 300); }, 2600);
}

function todayStr() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function ordered() {
  const items = [...state.items];
  if (state.sort === "date") {
    items.sort((a, b) => {
      if (a.due_date && !b.due_date) return -1;
      if (!a.due_date && b.due_date) return 1;
      if (a.due_date && b.due_date && a.due_date !== b.due_date) return a.due_date < b.due_date ? -1 : 1;
      return (a.position || 0) - (b.position || 0);
    });
  }
  return items;
}

function paintSortBtns() {
  document.querySelectorAll(".sortBtn").forEach((b) => {
    const active = b.dataset.sort === state.sort;
    b.className = "sortBtn btn font-bold " + (active
      ? "bg-primary/20 text-primary"
      : "text-muted-foreground hover:text-foreground");
  });
}

function render() {
  paintSortBtns();
  const items = ordered();
  const today = todayStr();
  el.todoCount.textContent = state.items.length ? `${state.items.length} item${state.items.length === 1 ? "" : "s"}` : "";
  el.todoEmpty.classList.toggle("hidden", items.length > 0);
  el.todoList.innerHTML = items.map((it, i) => {
    const overdue = it.due_date && it.due_date < today;
    const dueCls = overdue
      ? "border-red-500/60 text-red-700 dark:text-red-300"
      : "border-input text-muted-foreground";
    const moveBtns = state.sort === "manual" ? `
        <button data-act="up" data-id="${esc(it.id)}" class="btn btn-ghost text-xs" title="Move up">▲</button>
        <button data-act="down" data-id="${esc(it.id)}" class="btn btn-ghost text-xs" title="Move down">▼</button>` : "";
    return `<li class="rounded-xl border border-border bg-card p-3 ${overdue ? "border-l-4 border-l-red-500" : ""}">
      <div class="flex items-center gap-2">
        <span class="text-[10px] font-mono text-muted-foreground w-5">${state.sort === "manual" ? i + 1 : "·"}</span>
        <button data-act="done" data-id="${esc(it.id)}" class="shrink-0 h-6 w-6 rounded-full border-2 border-emerald-500/60 text-emerald-600 dark:text-emerald-400 font-bold text-xs hover:bg-emerald-500/15 transition" title="Mark done">✓</button>
        <span class="min-w-0 flex-1">
          <span class="block text-sm font-bold truncate">${esc(it.subject || "(no subject)")}</span>
          <span class="block text-xs text-muted-foreground truncate">${esc(it.sender_name || it.sender_email || "")}</span>
        </span>
        ${moveBtns}
        <a href="https://mail.google.com/mail/u/0/#inbox/${esc(it.id)}" target="_blank" rel="noopener noreferrer" class="shrink-0 btn btn-ghost text-xs font-bold text-muted-foreground hover:bg-muted transition">Open</a>
        <button data-act="untodo" data-id="${esc(it.id)}" class="shrink-0 btn btn-outline text-xs text-muted-foreground" title="Remove from TODO — email stays in Gmail">Remove</button>
      </div>
      <div class="mt-2 flex items-center gap-2 flex-wrap pl-8">
        <label class="flex items-center gap-1.5 text-xs text-muted-foreground">Due
          <input type="date" data-due="${esc(it.id)}" value="${esc(it.due_date || "")}" class="rounded border px-1.5 py-0.5 text-xs bg-transparent ${dueCls}" />
        </label>
        ${overdue ? `<span class="rounded bg-red-500/15 px-1.5 py-0.5 text-[9px] font-bold text-red-700 dark:text-red-300">OVERDUE</span>` : ""}
        ${it.due_date && !overdue ? `<span class="text-xs text-muted-foreground">due ${esc(it.due_date)}</span>` : ""}
      </div>
    </li>`;
  }).join("");
}

async function load() {
  try {
    const data = await api("/api/todos");
    state.items = data.items || [];
    render();
  } catch (err) { toast(`Load failed: ${err.message}`, "err"); }
}

document.querySelectorAll(".sortBtn").forEach((b) => b.addEventListener("click", () => {
  state.sort = b.dataset.sort;
  try { localStorage.setItem("gmailer-todo-sort", state.sort); } catch (e) {}
  render();
}));

el.refreshBtn.addEventListener("click", load);

el.todoList.addEventListener("change", async (e) => {
  const inp = e.target.closest("[data-due]");
  if (!inp) return;
  try {
    await api(`/api/todos/${encodeURIComponent(inp.dataset.due)}`, {
      method: "PATCH", body: JSON.stringify({ due_date: inp.value || "" }),
    });
    const it = state.items.find((x) => x.id === inp.dataset.due);
    if (it) it.due_date = inp.value || null;
    render();
    toast(inp.value ? `Due ${inp.value}` : "Due date cleared", "ok");
  } catch (err) { toast(`Date failed: ${err.message}`, "err"); load(); }
});

el.todoList.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  const id = b.dataset.id;
  const act = b.dataset.act;
  try {
    if (act === "up" || act === "down") {
      await api(`/api/todos/${encodeURIComponent(id)}/move`, { method: "POST", body: JSON.stringify({ direction: act }) });
      await load();
    } else if (act === "done") {
      await api("/api/todos/done", { method: "POST", body: JSON.stringify({ id }) });
      toast("Done — email stays in Gmail", "ok");
      await load();
    } else if (act === "untodo") {
      await api("/api/todos/remove", { method: "POST", body: JSON.stringify({ id }) });
      toast("Removed from TODO — email stays in Gmail", "info");
      await load();
    }
  } catch (err) { toast(`Failed: ${err.message}`, "err"); }
});

load();
