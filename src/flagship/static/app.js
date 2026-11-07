// Flagship web UI. Plain ES module, no build step. Talks to the JSON API on the same origin.

const ENVS = ["dev", "staging", "prod"];
const OPS = [
  "equals", "in", "contains", "starts_with", "ends_with", "matches", "lt", "lte", "gt", "gte", "between",
  "semver_eq", "semver_lt", "semver_gt", "before", "after", "segment_match",
];
const view = document.getElementById("view");
const actorSel = document.getElementById("actor");
const state = { flags: [], segments: [], editor: null };

try { actorSel.value = localStorage.getItem("flagship.actor") || "alice"; } catch { /* storage blocked */ }
actorSel.addEventListener("change", () => {
  try { localStorage.setItem("flagship.actor", actorSel.value); } catch { /* storage blocked */ }
  route();
});

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmt = (v) => (typeof v === "string" ? v : JSON.stringify(v));
const pct = (x, d = 1) => (x == null ? "n/a" : `${(x * 100).toFixed(d)}%`);
const ago = (ts) => {
  const s = Date.now() / 1000 - ts;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
};

async function api(path, opts = {}) {
  const headers = { "content-type": "application/json", "x-actor": actorSel.value, ...(opts.headers || {}) };
  const res = await fetch(path, { ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  return { ok: res.ok, status: res.status, data };
}

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 3500);
}

function resultMessage(r) {
  if (r.status === 202) return `Saved as change request ${r.data.change_request.id}. A second person must approve it in prod.`;
  if (r.status === 409) return `Conflict: ${r.data.error}. Reload to see the latest version.`;
  if (r.status === 400) return `Rejected: ${(r.data.errors || [r.data.error]).join("; ")}`;
  if (r.data?.status === "unchanged") return "Nothing changed.";
  if (!r.ok) return `Error ${r.status}: ${r.data?.error || r.data?.detail || ""}`;
  return "Saved.";
}

async function refreshPending() {
  const r = await api("/api/approvals");
  document.getElementById("pending-count").textContent = r.data.change_requests.length || "";
}

// ---------- serve / rollout helpers ----------

function serveSummary(flag, serve) {
  if (!serve) return "";
  if ("variation" in serve) return esc(flag.variations[serve.variation]?.name ?? serve.variation);
  const w = serve.rollout?.weights || [];
  return w.map((x, i) => `${esc(flag.variations[i]?.name)} ${x / 1000}%`).join(" / ");
}

function rolloutBar(flag, serve) {
  const colors = ["var(--good)", "var(--muted)", "var(--accent)", "var(--warn)", "var(--bad)"];
  const w = serve && "rollout" in serve ? serve.rollout.weights : flag.variations.map((_, i) => (i === serve?.variation ? 100000 : 0));
  return `<div class="bar" aria-hidden="true">${w.map((x, i) => `<span style="width:${x / 1000}%;background:${colors[i % colors.length]}"></span>`).join("")}</div>`;
}

// ---------- flag list ----------

async function renderFlags() {
  const [f, s] = await Promise.all([api("/api/flags"), api("/api/segments")]);
  state.flags = f.data.flags;
  state.segments = s.data.segments;
  const rows = state.flags.map((flag) => {
    const prod = flag.environments.prod;
    const toggles = ENVS.map((env) => {
      const cfg = flag.environments[env];
      return `<label class="switch" title="${env}"><input type="checkbox" data-toggle="${esc(flag.key)}" data-env="${env}" ${cfg.on ? "checked" : ""}><span class="track"></span>${env}</label>`;
    }).join("");
    const tags = (flag.tags || []).map((t) => `<span class="chip ${esc(t)}">${esc(t)}</span>`).join("");
    const served = prod.on ? serveSummary(flag, prod.fallthrough) : `off: ${esc(flag.variations[prod.off_variation]?.name)}`;
    return `<tr>
      <td><a class="flag-name" href="#/flag/${esc(flag.key)}">${esc(flag.key)}</a><div class="small muted">${esc(flag.name || "")}</div><div>${tags}</div></td>
      <td class="kind"><span class="chip">${esc(flag.kind)}</span></td>
      <td><div class="env-cells">${toggles}</div></td>
      <td class="small">${served}${prod.rules?.length ? `<div class="muted">${prod.rules.length} rule${prod.rules.length > 1 ? "s" : ""}</div>` : ""}<div style="margin-top:4px">${prod.on ? rolloutBar(flag, prod.fallthrough) : ""}</div></td>
      <td class="num">${flag.evaluations.toLocaleString()}</td>
      <td class="small muted">v${flag.version}, ${ago(flag.updated_at)}</td>
    </tr>`;
  }).join("");
  view.innerHTML = `<section class="panel">
    <div class="row spread"><h2>Flags</h2><span class="small muted">${state.flags.length} flags, ${state.segments.length} segments. Toggles change that environment right away. Critical flags wait for a second approver before they turn on in prod.</span></div>
    <div class="table-scroll"><table class="flags-table">
      <thead><tr><th>Flag</th><th>Kind</th><th>Environments</th><th>Prod default serve</th><th class="num">Evaluations</th><th>Changed</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
  </section>`;
  view.querySelectorAll("[data-toggle]").forEach((el) => el.addEventListener("change", async () => {
    const flag = state.flags.find((x) => x.key === el.dataset.toggle);
    const r = await api(`/api/flags/${flag.key}/environments/${el.dataset.env}`, { method: "POST", body: { on: el.checked, version: flag.version } });
    toast(resultMessage(r));
    refreshPending();
    renderFlags();
  }));
}

// ---------- flag editor ----------

function parseValues(op, text) {
  const parts = text.split(",").map((s) => s.trim()).filter(Boolean);
  if (op === "between") {
    return parts.map((p) => p.split("..").map((x) => Number(x.trim())));
  }
  return parts.map((p) => {
    if (["lt", "lte", "gt", "gte"].includes(op) && !Number.isNaN(Number(p))) return Number(p);
    if (op === "equals" || op === "in") {
      try { const v = JSON.parse(p); if (typeof v !== "object") return v; } catch { /* plain string */ }
    }
    return p;
  });
}
const showValues = (op, values) => (values || []).map((v) => (op === "between" && Array.isArray(v) ? `${v[0]}..${v[1]}` : typeof v === "string" ? v : JSON.stringify(v))).join(", ");

function serveEditor(flag, serve, path) {
  const isRollout = "rollout" in serve;
  const opts = flag.variations.map((v, i) => `<option value="${i}" ${!isRollout && serve.variation === i ? "selected" : ""}>${esc(v.name || fmt(v.value))}</option>`).join("");
  let weights = "";
  if (isRollout) {
    if (flag.kind === "boolean" && flag.variations.length === 2) {
      const ti = flag.variations.findIndex((v) => v.value === true);
      const p = serve.rollout.weights[ti] / 1000;
      weights = `<label class="slider"><span><strong>${p}%</strong> of users get ${esc(flag.variations[ti].name)}</span>
        <input type="range" min="0" max="100" step="1" value="${p}" data-pct="${path}" data-true="${ti}"></label>`;
    } else {
      weights = `<div class="weights">${serve.rollout.weights.map((w, i) => `<label>${esc(flag.variations[i].name)} <input type="number" min="0" max="100" step="0.1" value="${w / 1000}" data-weight="${path}" data-i="${i}">%</label>`).join("")}</div>`;
    }
  }
  return `<div class="serve"><span class="small muted">Serve</span>
    <select data-serve="${path}"><option value="rollout" ${isRollout ? "selected" : ""}>percentage rollout</option>${opts}</select>${weights}</div>
    ${isRollout ? rolloutBar(flag, serve) : ""}`;
}

function renderEditorBody() {
  const { flag, env } = state.editor;
  const cfg = flag.environments[env];
  const segOpts = state.segments.map((s) => s.key).join(", ");
  const rules = (cfg.rules || []).map((rule, ri) => `<div class="rule">
      <div class="rule-head"><strong>Rule ${ri + 1}</strong> <input data-rdesc="${ri}" value="${esc(rule.description || "")}" placeholder="description" style="flex:1 1 160px">
        <span class="row"><button class="ghost" data-up="${ri}" ${ri === 0 ? "disabled" : ""} title="Move up">Up</button><button class="ghost" data-del="${ri}">Remove</button></span></div>
      ${(rule.clauses || []).map((c, ci) => `<div class="clause">
        <input data-cattr="${ri}.${ci}" value="${esc(c.attribute || "")}" placeholder="attribute" ${c.op === "segment_match" ? "disabled" : ""}>
        <select data-cop="${ri}.${ci}">${OPS.map((o) => `<option ${o === c.op ? "selected" : ""}>${o}</option>`).join("")}</select>
        <input class="values" data-cval="${ri}.${ci}" value="${esc(showValues(c.op, c.values))}" placeholder="${c.op === "segment_match" ? segOpts : c.op === "between" ? "10..1000" : "comma separated"}">
        <label class="switch small"><input type="checkbox" data-cneg="${ri}.${ci}" ${c.negate ? "checked" : ""}><span class="track"></span>not</label>
        <button class="link" data-cdel="${ri}.${ci}" title="Remove clause">x</button>
      </div>`).join("")}
      <button class="link" data-cadd="${ri}">+ clause</button>
      ${serveEditor(flag, rule, `r${ri}`)}
    </div>`).join("");
  const prereqs = (cfg.prerequisites || []).map((p) => {
    const pf = state.flags.find((x) => x.key === p.key);
    return `<li><a href="#/flag/${esc(p.key)}">${esc(p.key)}</a> must serve <strong>${esc(pf?.variations[p.variation]?.name ?? p.variation)}</strong></li>`;
  }).join("");
  const offName = flag.variations[cfg.off_variation]?.name;
  document.getElementById("editor-body").innerHTML = `
    <div class="row" style="margin-bottom:12px">${ENVS.map((e) => `<button class="${e === env ? "" : "ghost"}" data-env-tab="${e}">${e}</button>`).join("")}</div>
    <div class="row" style="margin-bottom:12px">
      <label class="switch"><input type="checkbox" id="ed-on" ${cfg.on ? "checked" : ""}><span class="track"></span>${env} targeting on</label>
      <label class="switch"><input type="checkbox" id="ed-kill" ${cfg.killed ? "checked" : ""}><span class="track"></span>kill switch</label>
      <span class="small muted">When off or killed, everyone gets <strong>${esc(offName)}</strong>.</span>
    </div>
    ${prereqs ? `<h3>Prerequisites</h3><ul class="small">${prereqs}</ul>` : ""}
    <h3>Rules, evaluated top to bottom</h3>
    ${rules || `<p class="small muted">No rules in ${env}. Everyone gets the default below.</p>`}
    <button class="ghost" id="add-rule">Add rule</button>
    <h3 style="margin-top:16px">Default (fallthrough)</h3>
    ${serveEditor(flag, cfg.fallthrough, "ft")}
    <div class="row" style="margin-top:16px"><button id="save">Save ${env} as ${esc(actorSel.value)}</button><button class="ghost" id="revert">Discard changes</button><span id="save-msg" class="small muted"></span></div>`;
  liveEval();
}

function editorTarget(path) {
  const cfg = state.editor.flag.environments[state.editor.env];
  return path === "ft" ? cfg.fallthrough : cfg.rules[Number(path.slice(1))];
}

function setServe(path, serve) {
  const cfg = state.editor.flag.environments[state.editor.env];
  if (path === "ft") cfg.fallthrough = serve;
  else {
    const rule = cfg.rules[Number(path.slice(1))];
    delete rule.variation;
    delete rule.rollout;
    Object.assign(rule, serve);
  }
}

function onEditorInput(e) {
  const t = e.target;
  const { flag, env } = state.editor;
  const cfg = flag.environments[env];
  const clauseAt = (s) => { const [r, c] = s.split(".").map(Number); return cfg.rules[r].clauses[c]; };
  let rerender = false;
  if (t.id === "ed-on") cfg.on = t.checked;
  else if (t.id === "ed-kill") cfg.killed = t.checked;
  else if (t.dataset.rdesc) cfg.rules[Number(t.dataset.rdesc)].description = t.value;
  else if (t.dataset.cattr) clauseAt(t.dataset.cattr).attribute = t.value;
  else if (t.dataset.cop) {
    const c = clauseAt(t.dataset.cop);
    c.op = t.value;
    if (c.op === "segment_match") c.attribute = "key";
    rerender = true;
  } else if (t.dataset.cval) { const c = clauseAt(t.dataset.cval); c.values = parseValues(c.op, t.value); }
  else if (t.dataset.cneg) clauseAt(t.dataset.cneg).negate = t.checked;
  else if (t.dataset.serve) {
    const n = flag.variations.length;
    if (t.value === "rollout") {
      const w = Array(n).fill(Math.floor(100000 / n));
      w[0] += 100000 - w.reduce((a, b) => a + b, 0);
      setServe(t.dataset.serve, { rollout: { weights: w, bucket_by: "key" } });
    } else setServe(t.dataset.serve, { variation: Number(t.value) });
    rerender = true;
  } else if (t.dataset.pct) {
    const ti = Number(t.dataset.true);
    const w = [0, 0];
    w[ti] = Number(t.value) * 1000;
    w[1 - ti] = 100000 - w[ti];
    editorTarget(t.dataset.pct).rollout.weights = w;
    if (e.type === "change") rerender = true;
    else t.parentElement.querySelector("strong").textContent = `${t.value}%`;
  } else if (t.dataset.weight) {
    editorTarget(t.dataset.weight).rollout.weights[Number(t.dataset.i)] = Math.round(Number(t.value) * 1000);
  }
  if (rerender) renderEditorBody();
  else liveEval();
}

function onEditorClick(e) {
  const t = e.target.closest("button");
  if (!t) return;
  const { flag } = state.editor;
  const cfg = flag.environments[state.editor.env];
  if (t.dataset.envTab) { state.editor.env = t.dataset.envTab; renderEditorBody(); return; }
  if (t.id === "add-rule") {
    cfg.rules = cfg.rules || [];
    cfg.rules.push({ id: `rule-${Date.now().toString(36)}`, description: "", clauses: [{ attribute: "country", op: "in", values: ["US"] }], variation: 0 });
  } else if (t.dataset.del) cfg.rules.splice(Number(t.dataset.del), 1);
  else if (t.dataset.up) { const i = Number(t.dataset.up); [cfg.rules[i - 1], cfg.rules[i]] = [cfg.rules[i], cfg.rules[i - 1]]; }
  else if (t.dataset.cadd) cfg.rules[Number(t.dataset.cadd)].clauses.push({ attribute: "", op: "equals", values: [] });
  else if (t.dataset.cdel) { const [r, c] = t.dataset.cdel.split(".").map(Number); cfg.rules[r].clauses.splice(c, 1); }
  else if (t.id === "save") { save(); return; }
  else if (t.id === "revert") { renderEditor(flag.key); return; }
  else return;
  renderEditorBody();
}

async function save() {
  const r = await api(`/api/flags/${state.editor.flag.key}`, { method: "PUT", body: state.editor.flag });
  const msg = resultMessage(r);
  document.getElementById("save-msg").textContent = msg;
  toast(msg);
  refreshPending();
  if (r.status === 200) { state.editor.flag = r.data.flag; renderEditorBody(); renderHistory(); }
}

let evalTimer;
function liveEval() {
  clearTimeout(evalTimer);
  evalTimer = setTimeout(async () => {
    const out = document.getElementById("eval-out");
    let context;
    try { context = JSON.parse(document.getElementById("eval-ctx").value); } catch {
      out.innerHTML = `<div class="result error">User context is not valid JSON.</div>`;
      return;
    }
    const r = await api("/api/evaluate", { method: "POST", body: { env: state.editor.env, draft: state.editor.flag, context } });
    const d = r.data;
    const reason = d.reason || {};
    let why = reason.kind;
    if (reason.kind === "RULE_MATCH") why = `RULE_MATCH: rule ${reason.rule_index + 1}${reason.rule_id ? ` (${esc(reason.rule_id)})` : ""}`;
    if (reason.kind === "PREREQUISITE_FAILED") why = `PREREQUISITE_FAILED: ${esc(reason.prerequisite_key)}`;
    if (reason.kind === "ERROR") why = `ERROR: ${esc(reason.error_kind)}`;
    if (reason.killed) why += " (kill switch)";
    out.innerHTML = `<div class="result ${reason.kind === "ERROR" ? "error" : ""}">
      <div class="small muted">${esc(state.editor.env)} serves</div>
      <div class="value">${esc(fmt(d.value))}</div>
      <div class="small">${d.variation_name ? `variation <strong>${esc(d.variation_name)}</strong>, ` : ""}reason <code>${why}</code></div>
      ${d.bucket != null ? `<div class="small muted">Rollout bucket ${d.bucket.toFixed(3)} of 100. A rollout serves the first variation while the bucket is below its percentage, so raising the percentage only adds users.</div>` : ""}
      ${(d.prerequisites || []).map((p) => `<div class="small muted">prerequisite ${esc(p.key)}: ${esc(fmt(p.value))} (${esc(p.reason.kind)})</div>`).join("")}
    </div>`;
  }, 150);
}

async function renderHistory() {
  const r = await api(`/api/audit?key=${encodeURIComponent(state.editor.flag.key)}&limit=8`);
  document.getElementById("history").innerHTML = auditEntries(r.data.entries);
}

async function renderEditor(key) {
  const [f, fl, s] = await Promise.all([api(`/api/flags/${encodeURIComponent(key)}`), api("/api/flags"), api("/api/segments")]);
  if (!f.ok) { view.innerHTML = `<div class="panel">Flag ${esc(key)} not found.</div>`; return; }
  state.flags = fl.data.flags;
  state.segments = s.data.segments;
  const prev = state.editor?.flag.key === key ? state.editor.env : "prod";
  state.editor = { flag: f.data, env: prev };
  const flag = f.data;
  const sample = { key: "user-00042", country: "US", platform: "ios", app_version: "4.1.9", plan: "pro", email: "shopper42@example.test", orders: 14 };
  view.innerHTML = `<div class="grid-editor">
    <section class="panel">
      <div class="row spread"><div style="min-width:0"><h2 style="margin:0"><code>${esc(flag.key)}</code> <span class="chip">${esc(flag.kind)}</span></h2>
        <p class="sub small">${esc(flag.description || flag.name || "")}</p>
        <div>${(flag.tags || []).map((t) => `<span class="chip ${esc(t)}">${esc(t)}</span>`).join("")}</div></div>
        <a class="small" href="#/flags">All flags</a></div>
      <p class="small muted">Variations: ${flag.variations.map((v, i) => `<code>${i}: ${esc(v.name)} = ${esc(fmt(v.value))}</code>`).join(", ")}. Version ${flag.version}. Salt <code>${esc(flag.salt)}</code>.</p>
      <div id="editor-body"></div>
    </section>
    <aside class="stack sticky">
      <section class="panel">
        <h2>Evaluate for this user</h2>
        <p class="small muted">Runs the same engine the SDK uses, on your unsaved draft.</p>
        <textarea id="eval-ctx" rows="9" spellcheck="false">${esc(JSON.stringify(sample, null, 2))}</textarea>
        <div id="eval-out"></div>
      </section>
      <section class="panel"><h2>Recent changes</h2><div id="history"></div></section>
    </aside>
  </div>`;
  const body = document.getElementById("editor-body");
  body.addEventListener("input", onEditorInput);
  body.addEventListener("change", onEditorInput);
  body.addEventListener("click", onEditorClick);
  document.getElementById("eval-ctx").addEventListener("input", liveEval);
  renderEditorBody();
  renderHistory();
}

// ---------- audit ----------

function auditEntries(entries) {
  if (!entries.length) return `<p class="small muted">No entries.</p>`;
  return entries.map((e) => {
    const changes = (e.diff || []).filter((d) => !["version", "updated_at", "created_at"].includes(d.path));
    const list = changes.slice(0, 12).map((d) => `<li><code>${esc(d.path)}</code> ${d.before !== null ? `<span class="before mono">${esc(fmt(d.before))}</span>` : ""} ${d.after !== null ? `<span class="after mono">${esc(fmt(d.after))}</span>` : ""}</li>`).join("");
    const more = changes.length > 12 ? `<li class="muted">and ${changes.length - 12} more</li>` : "";
    const created = e.action === "flag.create" ? `<li class="muted">created</li>` : "";
    return `<div class="audit-entry">
      <div class="row spread"><span><strong>${esc(e.actor)}</strong> <code>${esc(e.action)}</code> <a href="#/flag/${esc(e.entity_key)}">${esc(e.entity_key)}</a></span><span class="small muted">${ago(e.ts)}</span></div>
      ${e.note ? `<div class="small muted">${esc(e.note)}</div>` : ""}
      <ul class="diff">${e.action === "flag.create" ? created : list + more}</ul>
    </div>`;
  }).join("");
}

async function renderAudit() {
  const r = await api("/api/audit?limit=60");
  view.innerHTML = `<section class="panel"><div class="row spread"><h2>Audit log</h2><span class="small muted">Append-only: the database rejects UPDATE and DELETE on this table. Red is before, green is after.</span></div>${auditEntries(r.data.entries)}</section>`;
}

// ---------- approvals ----------

async function renderApprovals() {
  const [p, all] = await Promise.all([api("/api/approvals"), api("/api/approvals?status=all")]);
  const pending = p.data.change_requests;
  const done = all.data.change_requests.filter((c) => c.status !== "pending").slice(0, 10);
  const card = (cr) => {
    const own = cr.requested_by === actorSel.value;
    return `<div class="audit-entry">
      <div class="row spread"><span><code>${esc(cr.id)}</code> <strong>${esc(cr.requested_by)}</strong> wants <code>${esc(cr.kind)}</code> on <a href="#/flag/${esc(cr.flag_key)}">${esc(cr.flag_key)}</a></span><span class="small muted">${ago(cr.ts)}</span></div>
      <div id="crdiff-${esc(cr.id)}"></div>
      <div class="row" style="margin-top:8px">
        <button data-approve="${esc(cr.id)}" ${own ? "disabled title=\"You cannot approve your own change\"" : ""}>Approve as ${esc(actorSel.value)}</button>
        <button class="ghost" data-reject="${esc(cr.id)}">Reject</button>
        ${own ? `<span class="small muted">Switch "Acting as" to someone else to approve. Four eyes.</span>` : ""}
      </div></div>`;
  };
  view.innerHTML = `<section class="panel"><h2>Pending approvals</h2>
    <p class="small muted">Prod changes to flags tagged <span class="chip critical">critical</span> need a second person. Turning a flag off never waits.</p>
    ${pending.map(card).join("") || `<p class="small muted">Nothing waiting.</p>`}</section>
    <section class="panel" style="margin-top:16px"><h2>Decided</h2>
    ${done.map((c) => `<div class="small audit-entry"><code>${esc(c.id)}</code> ${esc(c.flag_key)}: <span class="pill ${c.status === "approved" ? "on" : "off"}">${esc(c.status)}</span> by ${esc(c.decided_by || "")}, requested by ${esc(c.requested_by)}</div>`).join("") || `<p class="small muted">None yet.</p>`}</section>`;
  for (const cr of pending) {
    if (cr.kind !== "flag.update") continue;
    const cur = await api(`/api/flags/${encodeURIComponent(cr.flag_key)}`);
    const changes = diff(cur.data, cr.payload).filter((d) => !["version", "updated_at"].includes(d.path));
    document.getElementById(`crdiff-${cr.id}`).innerHTML = `<ul class="diff">${changes.map((d) => `<li><code>${esc(d.path)}</code> <span class="before mono">${esc(fmt(d.before))}</span> <span class="after mono">${esc(fmt(d.after))}</span></li>`).join("")}</ul>`;
  }
  view.querySelectorAll("[data-approve],[data-reject]").forEach((b) => b.addEventListener("click", async () => {
    const id = b.dataset.approve || b.dataset.reject;
    const r = await api(`/api/approvals/${id}/${b.dataset.approve ? "approve" : "reject"}`, { method: "POST", body: {} });
    toast(r.ok ? `Change request ${r.data.status}.` : resultMessage(r));
    refreshPending();
    renderApprovals();
  }));
}

function diff(a, b, path = "") {
  if (a && b && typeof a === "object" && typeof b === "object" && Array.isArray(a) === Array.isArray(b) && (!Array.isArray(a) || a.length === b.length)) {
    return [...new Set([...Object.keys(a), ...Object.keys(b)])].flatMap((k) => diff(a[k], b[k], path ? (Array.isArray(a) ? `${path}[${k}]` : `${path}.${k}`) : k));
  }
  return JSON.stringify(a) === JSON.stringify(b) ? [] : [{ path, before: a ?? null, after: b ?? null }];
}

// ---------- experiments ----------

function ciBar(row, scale) {
  if (row.lift == null) return "";
  const x = (v) => 50 + (v / scale) * 50;
  const lo = Math.max(0, x(row.lift_ci_low)), hi = Math.min(100, x(row.lift_ci_high));
  return `<div class="ci" title="95% CI on relative lift"><span class="zero" style="left:50%"></span><span class="range" style="left:${lo}%;width:${hi - lo}%"></span><span class="pt" style="left:calc(${x(row.lift)}% - 1px)"></span></div>`;
}

async function renderExperiments() {
  const list = (await api("/api/experiments")).data.experiments;
  const parts = [];
  for (const f of list) {
    const r = (await api(`/api/experiments/${encodeURIComponent(f.key)}?env=prod`)).data;
    const control = r.variations[r.control];
    const others = r.variations.filter((v) => v.variation !== r.control);
    const scale = Math.max(0.05, ...others.map((v) => Math.max(Math.abs(v.lift_ci_low || 0), Math.abs(v.lift_ci_high || 0)))) * 1.1;
    const total = r.variations.reduce((a, v) => a + v.users, 0);
    const best = others[0];
    const srm = r.srm.mismatch
      ? `<div class="banner bad">Sample ratio mismatch: p = ${r.srm.p_value.toExponential(2)}. Exposures do not match the configured split, so do not trust this readout until the assignment bug is found.</div>`
      : `<div class="banner good">Sample ratio check passed (chi-square ${r.srm.chi2}, p = ${r.srm.p_value.toFixed(3)}). Exposures match the configured split.</div>`;
    parts.push(`<section class="panel" style="margin-bottom:16px">
      <div class="row spread"><h2 style="margin:0"><a class="flag-name" href="#/flag/${esc(f.key)}">${esc(f.key)}</a></h2><span class="small muted">primary metric <code>${esc(r.primary_metric)}</code>, prod</span></div>
      ${r.hypothesis ? `<p class="small muted">${esc(r.hypothesis)}</p>` : ""}
      <div class="kpis">
        <div class="kpi"><div class="v">${total.toLocaleString()}</div><div class="l">users exposed</div></div>
        <div class="kpi"><div class="v">${pct(control.rate, 2)}</div><div class="l">control (${esc(control.name)}) conversion</div></div>
        ${best ? `<div class="kpi"><div class="v">${best.lift >= 0 ? "+" : ""}${pct(best.lift)}</div><div class="l">relative lift, ${esc(best.name)}</div></div>
        <div class="kpi"><div class="v">${best.p_value.toFixed(4)}</div><div class="l">p-value (${best.significant ? "significant at 0.05" : "not significant"})</div></div>` : ""}
      </div>
      ${srm}
      <div class="table-scroll"><table>
        <thead><tr><th>Variation</th><th class="num">Users</th><th class="num">Conversions</th><th class="num">Rate</th><th class="num">Lift</th><th>95% CI on lift</th><th class="num">CI</th></tr></thead>
        <tbody>${r.variations.map((v) => `<tr><td>${esc(v.name)}${v.variation === r.control ? ' <span class="chip">control</span>' : ""}</td>
          <td class="num">${v.users.toLocaleString()}</td><td class="num">${v.conversions.toLocaleString()}</td><td class="num">${pct(v.rate, 2)}</td>
          <td class="num">${v.lift == null ? "" : `${v.lift >= 0 ? "+" : ""}${pct(v.lift)}`}</td>
          <td>${ciBar(v, scale)}</td>
          <td class="num small">${v.lift == null ? "" : `${pct(v.lift_ci_low)} to ${pct(v.lift_ci_high)}`}</td></tr>`).join("")}</tbody>
      </table></div>
      <p class="small muted">Users are counted once, in the variation of their first exposure. Conversions count only after exposure. CI is a normal approximation on the difference in rates, divided by the control rate.</p>
    </section>`);
  }
  view.innerHTML = parts.join("") || `<div class="panel">No experiments.</div>`;
}

// ---------- stale ----------

async function renderStale() {
  const r = (await api("/api/stale?days=30")).data;
  view.innerHTML = `<section class="panel"><h2>Stale flags</h2>
    <p class="small muted">Flags serving one value to everyone in prod for more than ${r.days} days, or never evaluated. Run <code>flagship scan path/</code> in CI to see which ones still have code references.</p>
    <div class="table-scroll"><table><thead><tr><th>Flag</th><th>Finding</th><th>Detail</th><th class="num">Days</th></tr></thead>
    <tbody>${r.stale.map((s) => `<tr><td><a class="flag-name" href="#/flag/${esc(s.key)}">${esc(s.key)}</a></td><td><span class="pill warn">${esc(s.kind.replaceAll("_", " "))}</span></td><td class="small">${esc(s.detail)}</td><td class="num">${s.days}</td></tr>`).join("")}</tbody></table></div>
  </section>`;
}

// ---------- router ----------

async function route() {
  const hash = location.hash || "#/flags";
  const [, tab, arg] = hash.split("/");
  document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === (tab === "flag" ? "flags" : tab)));
  refreshPending();
  if (tab === "flag" && arg) return renderEditor(decodeURIComponent(arg));
  if (tab === "audit") return renderAudit();
  if (tab === "approvals") return renderApprovals();
  if (tab === "experiments") return renderExperiments();
  if (tab === "stale") return renderStale();
  return renderFlags();
}

window.addEventListener("hashchange", route);
route();
