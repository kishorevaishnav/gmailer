"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const el = {
  rulesList: $("rulesList"), rulesEmpty: $("rulesEmpty"), ruleCount: $("ruleCount"),
  proposalsList: $("proposalsList"), propsEmpty: $("propsEmpty"), propBadge: $("propBadge"),
  wikiList: $("wikiList"), wikiEmpty: $("wikiEmpty"),
  tracesList: $("tracesList"), traceRuleFilter: $("traceRuleFilter"), traceReload: $("traceReload"),
  editorTitle: $("editorTitle"), editorId: $("editorId"), ruleMarkdown: $("ruleMarkdown"),
  editorErrors: $("editorErrors"), saveBtn: $("saveBtn"), cancelBtn: $("cancelBtn"),
  deleteBtn: $("deleteBtn"), applyNow: $("applyNow"), ruleStats: $("ruleStats"),
  fName: $("fName"), fSender: $("fSender"), fAction: $("fAction"), fScope: $("fScope"),
  fSubject: $("fSubject"), fCategory: $("fCategory"), fEpd: $("fEpd"), fEnabled: $("fEnabled"),
  fAbout: $("fAbout"), buildMdBtn: $("buildMdBtn"),
  refreshBtn: $("refreshBtn"), newRuleBtn: $("newRuleBtn"), toasts: $("toasts"),
};

const state = { rules: [], proposals: [], wiki: [], editingId: null, tab: "rules" };

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail ?? data;
    const msg = typeof detail === "string" ? detail
      : detail && detail.errors ? detail.errors.map((e) => `line ${e.line}: ${e.msg}`).join("\n")
      : res.statusText || "Request failed";
    const err = new Error(msg);
    err.detail = detail;
    throw err;
  }
  return data;
}

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

function showErrors(err) {
  const d = err && err.detail;
  el.editorErrors.textContent = d && d.errors
    ? d.errors.map((e) => `line ${e.line}: ${e.msg}`).join("\n")
    : String(err.message || err);
  el.editorErrors.classList.remove("hidden");
}

function setTab(name) {
  state.tab = name;
  for (const t of ["rules", "proposals", "wiki", "traces"]) {
    $("tab-" + t).classList.toggle("hidden", t !== name);
  }
  document.querySelectorAll(".tabBtn").forEach((b) => {
    const active = b.dataset.tab === name;
    b.className = "tabBtn rounded-lg px-3 py-1.5 font-bold " + (active
      ? "bg-violet-500/20 text-violet-700 dark:text-violet-300"
      : "text-slate-500 hover:text-slate-700 dark:hover:text-slate-300");
  });
}

function matchSummary(p) {
  const parts = [];
  if (p.sender) parts.push("from " + p.sender);
  if (p.subject && p.subject.length) parts.push("subj: " + p.subject.join(", "));
  if (p.category && p.category.length) parts.push("cat: " + p.category.join(", "));
  if (p.emails_per_day) parts.push("≥" + p.emails_per_day + "/day");
  return parts.join(" · ") || "matches everything";
}

function renderAll() {
  el.ruleCount.textContent = state.rules.length ? `${state.rules.length} rule${state.rules.length === 1 ? "" : "s"} · first match wins` : "";
  el.propBadge.textContent = state.proposals.length ? `(${state.proposals.length})` : "";

  el.rulesEmpty.classList.toggle("hidden", state.rules.length > 0);
  el.rulesList.innerHTML = state.rules.map((r, i) => {
    const p = r.parsed || {};
    return `<li draggable="true" data-id="${r.id}" class="rule-row rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/60 p-3 cursor-grab">
      <div class="flex items-center gap-2">
        <span class="text-[10px] font-mono text-slate-400 w-5">${i + 1}</span>
        <span class="min-w-0 flex-1">
          <span class="block text-sm font-bold truncate">${esc(p.name || "Untitled")}</span>
          <span class="block text-[11px] text-slate-500 truncate">${esc(matchSummary(p))}</span>
        </span>
        <span class="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold ${p.action === "trash" ? "bg-red-500/15 text-red-700 dark:text-red-300" : p.action === "star" ? "bg-amber-500/15 text-amber-700 dark:text-amber-300" : "bg-slate-500/15 text-slate-600 dark:text-slate-300"}">${esc(p.action || "?")}</span>
        <button data-act="up" data-id="${r.id}" class="rounded border border-slate-300 dark:border-slate-700 px-1.5 py-0.5 text-[10px]" title="Move up">▲</button>
        <button data-act="down" data-id="${r.id}" class="rounded border border-slate-300 dark:border-slate-700 px-1.5 py-0.5 text-[10px]" title="Move down">▼</button>
        <button data-act="toggle" data-id="${r.id}" class="rounded border px-2 py-0.5 text-[10px] font-bold ${r.enabled ? "border-emerald-500/50 text-emerald-700 dark:text-emerald-300" : "border-slate-300 dark:border-slate-700 text-slate-400"}" title="${r.enabled ? "Disable" : "Enable"}">${r.enabled ? "ON" : "OFF"}</button>
      </div>
      <div class="mt-2 flex items-center gap-1.5 flex-wrap">
        <button data-act="edit" data-id="${r.id}" class="rounded bg-violet-500/15 px-2 py-1 text-[10px] font-bold text-violet-700 dark:text-violet-300">Edit</button>
        <button data-act="apply" data-id="${r.id}" class="rounded bg-emerald-500/15 px-2 py-1 text-[10px] font-bold text-emerald-700 dark:text-emerald-300">Apply now</button>
        <button data-act="traces" data-id="${r.id}" class="rounded bg-slate-500/15 px-2 py-1 text-[10px] font-bold text-slate-600 dark:text-slate-300">Activity</button>
        <button data-act="del" data-id="${r.id}" class="rounded bg-red-500/15 px-2 py-1 text-[10px] font-bold text-red-700 dark:text-red-300">Delete</button>
        <span class="ml-auto text-[10px] text-slate-400 font-mono">#${r.id} · ${esc(p.scope || "")}</span>
      </div>
      <div id="traces-${r.id}" class="hidden mt-2 border-t border-slate-200 dark:border-slate-800 pt-2 text-[11px] text-slate-500"></div>
    </li>`;
  }).join("");
  enableDrag();

  el.propsEmpty.classList.toggle("hidden", state.proposals.length > 0);
  el.proposalsList.innerHTML = state.proposals.map((p) => `<li class="rounded-xl border border-violet-500/40 bg-violet-500/5 p-3">
    <p class="text-sm font-bold text-violet-800 dark:text-violet-100">${esc(p.label)}</p>
    <p class="text-[11px] text-slate-500">${esc(p.summary || "")}</p>
    ${p.rationale ? `<p class="text-[11px] text-slate-500 mt-1"><b>Why:</b> ${esc(p.rationale)}</p>` : ""}
    ${p.downside ? `<p class="text-[11px] text-slate-500"><b>Risk:</b> ${esc(p.downside)}</p>` : ""}
    <div class="mt-2 flex gap-1.5">
      <button data-pact="approve" data-id="${p.id}" class="rounded bg-emerald-500/15 px-2 py-1 text-[10px] font-bold text-emerald-700 dark:text-emerald-300">Approve</button>
      <button data-pact="edit" data-id="${p.id}" class="rounded bg-violet-500/15 px-2 py-1 text-[10px] font-bold text-violet-700 dark:text-violet-300">Edit as rule</button>
      <button data-pact="reject" data-id="${p.id}" class="rounded bg-red-500/15 px-2 py-1 text-[10px] font-bold text-red-700 dark:text-red-300">Reject</button>
    </div>
  </li>`).join("");

  el.wikiEmpty.classList.toggle("hidden", state.wiki.length > 0);
  el.wikiList.innerHTML = state.wiki.map((w) => `<li class="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900/60 p-3">
    <p class="text-xs font-bold truncate">${esc(w.summary || w.target)}</p>
    <p class="text-[10px] text-slate-500">${esc(w.kind)} · signal ${(w.signal || 0).toFixed(2)} · ${w.evidence_count || 0} evidence · ${esc(w.status || "open")}</p>
    <div class="mt-2 flex gap-1.5">
      <button data-wact="create" data-id="${w.id}" class="rounded bg-violet-500/15 px-2 py-1 text-[10px] font-bold text-violet-700 dark:text-violet-300">Create rule</button>
      <button data-wact="dismiss" data-id="${w.id}" class="rounded border border-slate-300 dark:border-slate-700 px-2 py-1 text-[10px] text-slate-500">Dismiss</button>
    </div>
  </li>`).join("");

  el.traceRuleFilter.innerHTML = `<option value="">all rules</option>` + state.rules.map((r) => `<option value="${r.id}">${esc((r.parsed && r.parsed.name) || ("#" + r.id))}</option>`).join("");
}

function enableDrag() {
  let dragId = null;
  el.rulesList.querySelectorAll(".rule-row").forEach((row) => {
    row.addEventListener("dragstart", () => { dragId = Number(row.dataset.id); row.classList.add("dragging"); });
    row.addEventListener("dragend", () => row.classList.remove("dragging"));
    row.addEventListener("dragover", (e) => e.preventDefault());
    row.addEventListener("drop", async (e) => {
      e.preventDefault();
      const targetId = Number(row.dataset.id);
      if (!dragId || dragId === targetId) return;
      const ids = state.rules.map((r) => r.id);
      ids.splice(ids.indexOf(dragId), 1);
      ids.splice(ids.indexOf(targetId), 0, dragId);
      try {
        await api("/api/rules/reorder", { method: "POST", body: JSON.stringify({ rule_ids: ids }) });
        await loadAll(false);
      } catch (err) { toast(`Reorder failed: ${err.message}`, "err"); }
    });
  });
}

function fillForm(parsed) {
  el.fName.value = parsed.name || "";
  el.fSender.value = parsed.sender || "";
  el.fAction.value = parsed.action || "trash";
  el.fScope.value = parsed.scope || "all_mail";
  el.fSubject.value = (parsed.subject || []).join(", ");
  el.fCategory.value = (parsed.category || []).join(", ");
  el.fEpd.value = parsed.emails_per_day || "";
  el.fEnabled.checked = parsed.enabled !== false;
  el.fAbout.value = parsed.about || "";
}

function buildMarkdown() {
  const lines = [
    "---",
    `name: ${el.fName.value.trim() || "Untitled rule"}`,
    `enabled: ${el.fEnabled.checked ? "true" : "false"}`,
    `action: ${el.fAction.value}`,
    `scope: ${el.fScope.value}`,
    "---",
    "## match",
  ];
  if (el.fSender.value.trim()) lines.push(`sender: ${el.fSender.value.trim()}`);
  if (el.fSubject.value.trim()) lines.push(`subject: ${el.fSubject.value.trim()}`);
  if (el.fCategory.value.trim()) lines.push(`category: ${el.fCategory.value.trim()}`);
  if (el.fEpd.value) lines.push(`emails_per_day: ${el.fEpd.value}`);
  lines.push("", "## about", el.fAbout.value.trim() || "");
  el.ruleMarkdown.value = lines.join("\n") + "\n";
}

function newRule(presetMd) {
  state.editingId = null;
  el.editorTitle.textContent = "New Rule";
  el.editorId.textContent = "";
  el.deleteBtn.classList.add("hidden");
  el.ruleStats.textContent = "";
  el.editorErrors.classList.add("hidden");
  el.applyNow.checked = false;
  if (presetMd) {
    el.ruleMarkdown.value = presetMd;
  } else {
    fillForm({ name: "", sender: "", action: "trash", scope: "all_mail", subject: [], category: [], enabled: true, about: "" });
    buildMarkdown();
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
  setTimeout(() => el.fName.focus(), 60);
}

function editRule(id) {
  const r = state.rules.find((x) => x.id === id);
  if (!r) return;
  state.editingId = id;
  el.editorTitle.textContent = `Edit: ${(r.parsed && r.parsed.name) || "#" + id}`;
  el.editorId.textContent = `id ${id} · precedence ${r.precedence}`;
  el.deleteBtn.classList.remove("hidden");
  el.editorErrors.classList.add("hidden");
  el.ruleMarkdown.value = r.skill_md || "";
  fillForm({ ...(r.parsed || {}), enabled: r.enabled });
  loadRuleStats(id);
}

async function loadRuleStats(id) {
  try {
    const data = await api(`/api/traces?limit=5&rule_id=${id}`);
    const items = data.items || [];
    el.ruleStats.textContent = items.length
      ? `Recent: ${items.map((t) => `${t.action} ${t.subject || t.message_id || ""}`.slice(0, 60)).join(" · ")}`
      : "No auto-actions recorded yet.";
  } catch { el.ruleStats.textContent = ""; }
}

async function saveRule() {
  const md = el.ruleMarkdown.value;
  el.editorErrors.classList.add("hidden");
  try {
    let saved;
    if (state.editingId == null) {
      saved = await api("/api/rules", { method: "POST", body: JSON.stringify({ markdown: md, enabled: el.fEnabled.checked }) });
    } else {
      saved = await api(`/api/rules/${state.editingId}`, { method: "PATCH", body: JSON.stringify({ markdown: md, enabled: el.fEnabled.checked }) });
    }
    toast(state.editingId == null ? "Rule created" : "Rule saved", "ok");
    if (el.applyNow.checked && saved && saved.id) {
      try {
        const res = await api(`/api/rules/${saved.id}/apply-now`, { method: "POST", body: "{}" });
        toast(`Applied: ${res.matched || 0} matched`, "ok");
      } catch (err) { toast(`Apply failed: ${err.message}`, "err"); }
    }
    await loadAll(false);
    editRule(saved.id);
  } catch (err) { showErrors(err); }
}

async function loadAll(resetEditor = true) {
  try {
    const [rules, props, wiki] = await Promise.all([
      api("/api/rules"), api("/api/proposals"), api("/api/wiki/observations"),
    ]);
    state.rules = rules.items || [];
    state.proposals = (props.items || []).filter((p) => p.status === "pending");
    state.wiki = wiki.items || [];
    renderAll();
    if (resetEditor && state.editingId != null) editRule(state.editingId);
    if (resetEditor && state.editingId == null && !el.ruleMarkdown.value) newRule();
  } catch (err) { toast(`Load failed: ${err.message}`, "err"); }
}

async function loadTraces() {
  const rid = el.traceRuleFilter.value;
  const q = rid ? `?limit=50&rule_id=${rid}` : "?limit=50";
  try {
    const data = await api("/api/traces" + q);
    const items = data.items || [];
    el.tracesList.innerHTML = items.length ? items.map((t) => `<li class="rounded border border-slate-200 dark:border-slate-800 px-2 py-1 text-[11px] text-slate-600 dark:text-slate-400">
      <span class="font-bold">${esc(t.action)}</span> · ${esc(t.subject || t.message_id || "")}
      <span class="text-slate-400">· ${esc(t.sender_email || "")}${t.rule_id ? " · rule #" + t.rule_id : ""}</span>
    </li>`).join("") : `<li class="text-xs text-slate-500">No activity yet.</li>`;
  } catch (err) { toast(`Traces failed: ${err.message}`, "err"); }
}

document.querySelectorAll(".tabBtn").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
el.refreshBtn.addEventListener("click", () => loadAll(false));
el.newRuleBtn.addEventListener("click", () => newRule());
el.buildMdBtn.addEventListener("click", buildMarkdown);
el.cancelBtn.addEventListener("click", () => newRule());
el.saveBtn.addEventListener("click", saveRule);
el.traceReload.addEventListener("click", loadTraces);
el.deleteBtn.addEventListener("click", async () => {
  if (state.editingId == null || !confirm("Delete this rule?")) return;
  try {
    await api(`/api/rules/${state.editingId}`, { method: "DELETE" });
    toast("Rule deleted", "info");
    newRule();
    await loadAll(false);
  } catch (err) { toast(`Delete failed: ${err.message}`, "err"); }
});

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act]");
  if (b) {
    const id = Number(b.dataset.id);
    const act = b.dataset.act;
    if (act === "edit") editRule(id);
    else if (act === "toggle") {
      const r = state.rules.find((x) => x.id === id);
      await api(`/api/rules/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: !(r && r.enabled) }) }).catch((err) => toast(err.message, "err"));
      await loadAll(false);
    }
    else if (act === "up" || act === "down") {
      await api(`/api/rules/${id}/move`, { method: "POST", body: JSON.stringify({ direction: act }) }).catch((err) => toast(err.message, "err"));
      await loadAll(false);
    }
    else if (act === "del") {
      if (!confirm("Delete this rule?")) return;
      await api(`/api/rules/${id}`, { method: "DELETE" }).catch((err) => toast(err.message, "err"));
      if (state.editingId === id) newRule();
      await loadAll(false);
    }
    else if (act === "apply") {
      try {
        const res = await api(`/api/rules/${id}/apply-now`, { method: "POST", body: "{}" });
        toast(`Matched ${res.matched || 0} (${res.trash || 0} trash · ${res.star || 0} star · ${res.skip || 0} skip)`, "ok");
      } catch (err) { toast(`Apply failed: ${err.message}`, "err"); }
    }
    else if (act === "traces") {
      const box = $("traces-" + id);
      if (!box) return;
      if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
      try {
        const data = await api(`/api/traces?limit=5&rule_id=${id}`);
        const items = data.items || [];
        box.innerHTML = items.length ? items.map((t) => `<div class="truncate">${esc(t.action)} · ${esc(t.subject || t.message_id || "")}</div>`).join("") : "No auto-actions yet.";
      } catch { box.textContent = "Failed to load."; }
      box.classList.remove("hidden");
    }
    return;
  }
  const p = e.target.closest("[data-pact]");
  if (p) {
    const id = Number(p.dataset.id);
    if (p.dataset.pact === "approve") {
      await api(`/api/proposals/${id}/approve`, { method: "POST", body: JSON.stringify({ apply_now: false }) }).then(() => toast("Approved — rule created", "ok")).catch((err) => toast(err.message, "err"));
      await loadAll(false);
    } else if (p.dataset.pact === "reject") {
      await api(`/api/proposals/${id}/reject`, { method: "POST", body: "{}" }).catch(() => {});
      await loadAll(false);
    } else if (p.dataset.pact === "edit") {
      const prop = state.proposals.find((x) => x.id === id);
      if (prop) { newRule(prop.proposed_skill_md); el.editorTitle.textContent = `New rule from proposal: ${prop.label}`; }
    }
    return;
  }
  const w = e.target.closest("[data-wact]");
  if (w) {
    const id = Number(w.dataset.id);
    if (w.dataset.wact === "dismiss") {
      await api(`/api/wiki/observations/${id}/dismiss`, { method: "POST", body: "{}" }).catch(() => {});
      await loadAll(false);
    } else if (w.dataset.wact === "create") {
      const obs = state.wiki.find((x) => x.id === id);
      newRule(obs ? `---\nname: ${obs.target}\nenabled: true\naction: trash\nscope: all_mail\n---\n## match\nsender: ${obs.target}\n\n## about\n${obs.summary || ""}\n` : undefined);
    }
  }
});

setTab("rules");
loadAll(true);
