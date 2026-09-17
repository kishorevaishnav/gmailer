"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const escAttr = (s) => String(s ?? "").replace(/"/g, "&quot;");

const el = {
  stats: {
    total: $("statTotal"),
    done: $("statDone"),
    todo: $("statTodo"),
    detail: $("summaryDetail"),
  },
  saveBtn: $("saveBtn"),
  resetAllBtn: $("resetAllBtn"),
  promptsContainer: $("promptsContainer"),
  toasts: $("toasts"),
  refreshBtn: $("refreshBtn"),
};

let originals = {};

function toast(msg, tone = "info") {
  const t = document.createElement("div");
  t.className = "rounded-xl border px-4 py-2 text-sm shadow-xl " + (tone === "err"
    ? "bg-red-50 border-red-300 text-red-700 dark:bg-red-950 dark:border-red-600/60 dark:text-red-100"
    : tone === "ok"
      ? "bg-emerald-50 border-emerald-300 text-emerald-700 dark:bg-emerald-950 dark:border-emerald-600/60 dark:text-emerald-100"
      : "bg-white border-slate-300 text-slate-700 dark:bg-slate-800 dark:border-slate-700 dark:text-slate-100");
  t.textContent = msg;
  el.toasts.appendChild(t);
  setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 300); }, 2600);
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : res.statusText || "Request failed");
  return data;
}

async function loadStats() {
  try {
    const data = await api("/api/messages?limit=1");
    const total = data.total || 0;
    let doneCount = 0;
    let mockCount = 0;
    let todoCount = 0;

    const pageSize = 2000;
    let offset = 0;
    while (offset < total) {
      const batch = await api(`/api/messages?limit=${pageSize}&offset=${offset}`);
      for (const m of batch.items) {
        const summary = m.summary;
        if (summary && typeof summary === "object") {
          if (summary.mock) {
            mockCount += 1;
            todoCount += 1;
          } else {
            doneCount += 1;
          }
        } else if (!summary) {
          todoCount += 1;
        }
      }
      offset += pageSize;
      if (batch.count < pageSize) break;
    }

    el.stats.total.textContent = total;
    el.stats.done.textContent = doneCount;
    el.stats.todo.textContent = todoCount + mockCount;
    el.stats.detail.textContent = `${doneCount} AI-summarized · ${mockCount} mock/heuristic fallback · ${todoCount + mockCount} need real AI`;
    el.stats.detail.classList.toggle("text-red-500", (todoCount + mockCount) > 0);
    el.stats.detail.classList.toggle("text-slate-500", (todoCount + mockCount) === 0);
  } catch (err) {
    el.stats.total.textContent = "—";
    el.stats.done.textContent = "—";
    el.stats.todo.textContent = "—";
    el.stats.detail.textContent = `Load failed: ${err.message}`;
    el.stats.detail.className = "text-[11px] text-red-500";
  }
}

async function loadPrompts() {
  try {
    const data = await api("/api/settings/prompts");
    const prompts = data.prompts || [];
    originals = {};
    for (const p of prompts) {
      const ta = el.promptsContainer.querySelector(`textarea[data-key="${escAttr(p.key)}"]`);
      if (ta) {
        ta.value = p.value || "";
        originals[p.key] = p.value || "";
      }
    }
  } catch (err) {
    toast(`Failed to load prompts: ${err.message}`, "err");
  }
}

async function savePrompts() {
  const changes = {};
  for (const key of Object.keys(originals)) {
    const ta = el.promptsContainer.querySelector(`textarea[data-key="${escAttr(key)}"]`);
    if (ta && ta.value !== originals[key]) {
      changes[key] = ta.value;
    }
  }
  if (!Object.keys(changes).length) {
    toast("No changes to save", "info");
    return;
  }
  try {
    await api("/api/settings/prompts", { method: "PUT", body: JSON.stringify(changes) });
    for (const key of Object.keys(changes)) {
      originals[key] = changes[key];
    }
    toast("Prompts saved", "ok");
  } catch (err) {
    toast(`Save failed: ${err.message}`, "err");
  }
}

async function resetPrompt(key) {
  try {
    await api("/api/settings/prompts", { method: "PUT", body: JSON.stringify({ [key]: "" }) });
    toast(`Reset ${key} to default`, "ok");
  } catch (err) {
    toast(`Reset failed: ${err.message}`, "err");
  }
  await loadPrompts();
}

async function resetAll() {
  if (!confirm("Reset all prompts to built-in defaults?")) return;
  const changes = {};
  for (const key of Object.keys(originals)) {
    changes[key] = "";
  }
  try {
    await api("/api/settings/prompts", { method: "PUT", body: JSON.stringify(changes) });
    toast("All prompts reset to defaults", "ok");
  } catch (err) {
    toast(`Reset failed: ${err.message}`, "err");
  }
  await loadPrompts();
}

el.saveBtn.addEventListener("click", savePrompts);
el.resetAllBtn.addEventListener("click", resetAll);
el.refreshBtn.addEventListener("click", () => { loadPrompts(); loadStats(); });

el.promptsContainer.addEventListener("click", (e) => {
  const btn = e.target.closest(".resetBtn");
  if (!btn) return;
  const key = btn.dataset.reset;
  if (key) resetPrompt(key);
});

el.promptsContainer.addEventListener("keydown", (e) => {
  if (e.target.tagName === "TEXTAREA" && (e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    savePrompts();
  }
});

loadPrompts();
loadStats();
