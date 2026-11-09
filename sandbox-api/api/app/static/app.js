/* ============================================================================
 * sandbox-api v2 console - a dependency-free SPA.
 *
 * Mental model (mirrors the backend):
 *   GET  /status                       -> header pills + pool bar
 *   GET  /sandboxes                    -> the cards
 *   GET  /sandboxes/{id}/containers    -> "what's inside" (drawer)
 *   GET  /sandboxes/{id}/events        -> timeline (drawer + create log)
 *   GET  /events (SSE)                 -> the live log at the bottom
 *   POST /sandboxes/{id}/exec          -> run commands (agent)
 *   POST /sandboxes/{id}/sessions ...  -> durable terminal (agent)
 *   GET  /sandboxes/{id}/fs            -> file listing (agent)
 *   GET  /sandboxes/{id}/fingerprint   -> merkle root (agent)
 *
 * Cards/status poll every 2 s; events arrive live over SSE.
 * ==========================================================================*/

const $ = (id) => document.getElementById(id);
const api = async (path, opts = {}) => {
  const r = await fetch(path, opts);
  if (r.status === 204) return null;
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, detail: body.detail || body.error || r.statusText };
  return body;
};
const esc = (s) => String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

let selectedId = null;
let creatingId = null;
let lastEventSeq = {};
let term = { session: null, since: 0, timer: null };

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.innerHTML = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

/* -- header + pool ----------------------------------------------------------*/
async function refreshStatus() {
  try {
    const s = await api("/status");
    setPill("pill-api", `api: ${s.api}`, s.api === "up");
    setPill("pill-dind", `dind: ${s.dind}`, s.dind === "up");
    setPill("pill-node", `node: ${s.node}`, s.node === "up");
    setPill("pill-informer", `informer: ${s.informer.connected ? "watching" : "down"} (${s.informer.events_seen} ev)`, s.informer.connected);
    $("pill-count").textContent = `sandboxes: ${s.sandboxes}/${s.max_sandboxes}`;
    $("pill-iso").textContent = `isolation: ${s.isolation_available.join(", ")}`;
    renderPool(s.pool);
  } catch {
    setPill("pill-api", "api: unreachable", false);
  }
}
function setPill(id, text, up) {
  const el = $(id);
  el.textContent = text;
  el.className = "pill " + (up ? "up" : "down");
}
function renderPool(pool) {
  const used = new Set(pool.used);
  $("pool").innerHTML = "";
  for (let p = pool.start; p <= pool.end; p++) {
    const cell = document.createElement("div");
    cell.className = "cell" + (used.has(p) ? " used" : "");
    cell.textContent = p;
    $("pool").appendChild(cell);
  }
}

/* -- sandbox cards ----------------------------------------------------------*/
async function refreshSandboxes() {
  const list = await api("/sandboxes");
  $("empty").style.display = list.length ? "none" : "block";
  const box = $("list");
  box.innerHTML = "";
  for (const s of list) box.appendChild(card(s));
}

function card(s) {
  const el = document.createElement("div");
  el.className = "card";
  const live = s.state === "READY" || s.state === "DEGRADED";
  const urlBtn = s.url && live
    ? `<a href="${s.url}" target="_blank"><button>open app</button></a>` : "";
  const stopStart = live
    ? `<button class="ghost" data-act="stop">stop</button>` +
      (s.state === "DEGRADED" ? `<button class="ghost" data-act="start">heal</button>` : "")
    : s.state === "STOPPED" ? `<button class="ghost" data-act="start">start</button>` : "";
  const ttl = s.ttl_seconds ? ` . ttl ${s.ttl_seconds}s` : "";
  const dead = s.detail && s.detail.dead ? Object.entries(s.detail.dead).map(([k, v]) => `${k} ${v}`).join(", ") : "";
  el.innerHTML = `
    <h3>${esc(s.name)} <span class="badge ${s.state}">${s.state}</span>
      <span class="iso ${s.isolation}">${s.isolation}</span></h3>
    <div class="meta">
      template <code>${s.template}</code> . id <code>${s.id}</code>
      ${s.port ? ` . port <code>${s.port}</code>` : " . no port"}${ttl}
      ${s.error ? `<div class="err">[!] ${esc(s.error)}</div>` : ""}
      ${dead ? `<div class="err">dead: ${esc(dead)}</div>` : ""}
    </div>
    <div class="actions">
      ${urlBtn}
      <button class="ghost" data-act="details">details &gt;</button>
      ${stopStart}
      <button class="danger" data-act="delete">delete</button>
    </div>`;
  el.querySelectorAll("button[data-act]").forEach(b =>
    b.addEventListener("click", () => act(s, b.dataset.act)));
  return el;
}

async function act(s, action) {
  try {
    if (action === "details") return openDrawer(s);
    if (action === "delete") {
      if (!confirm(`Delete ${s.name}?\n\nRemoves containers + network, frees the port, keeps the event log.`)) return;
      await api(`/sandboxes/${s.id}`, { method: "DELETE" });
      toast(`<b>${esc(s.name)}</b> deleted.`);
    } else if (action === "stop") {
      await api(`/sandboxes/${s.id}/stop`, { method: "POST" });
      toast(`<b>${esc(s.name)}</b> stopped - containers frozen, port still reserved.`);
    } else if (action === "start") {
      await api(`/sandboxes/${s.id}/start`, { method: "POST" });
      toast(`<b>${esc(s.name)}</b> started.`);
    }
    refreshSandboxes();
  } catch (e) {
    toast(`[!] ${action} failed: HTTP ${e.status} - ${esc(e.detail)}`, true);
  }
}

/* -- create + live narration ------------------------------------------------*/
$("templates").addEventListener("click", (e) => {
  const card = e.target.closest(".tcard");
  if (!card) return;
  document.querySelectorAll(".tcard").forEach(c => c.classList.remove("sel"));
  card.classList.add("sel");
  card.querySelector("input").checked = true;
});

$("create").addEventListener("click", async () => {
  const name = $("cname").value.trim() || `sb-${Math.random().toString(16).slice(2, 6)}`;
  const template = document.querySelector('input[name="template"]:checked').value;
  const ttl = parseInt($("cttl").value) || null;
  const isolation = $("ciso").value;
  const log = $("createlog");
  log.classList.remove("hidden");
  log.innerHTML = "";
  narrate(`> POST /sandboxes {name:"${name}", template:"${template}", isolation:"${isolation}"${ttl ? `, ttl:${ttl}` : ""}}`);
  try {
    const s = await api("/sandboxes", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, template, ttl_seconds: ttl, isolation }),
    });
    creatingId = s.id;
    lastEventSeq[s.id] = 0;
    narrate(`[ok] accepted: id=${s.id}, state=${s.state} - watching its event log:`);
  } catch (e) {
    narrate(`x HTTP ${e.status}: ${e.detail}`);
    toast(`[!] create failed: ${esc(e.detail)}`, true);
  }
});

function narrate(line) {
  const log = $("createlog");
  const div = document.createElement("div");
  div.textContent = line;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function pumpCreateLog() {
  if (!creatingId) return;
  try {
    const events = await api(`/sandboxes/${creatingId}/events`);
    let last = lastEventSeq[creatingId] || 0;
    for (const e of events) {
      if (e.seq > last) {
        narrate(`   ${String(e.seq).padStart(2)} [${e.type}] ${e.message}`);
        last = e.seq;
      }
    }
    lastEventSeq[creatingId] = last;
    const s = await api(`/sandboxes/${creatingId}`);
    if (s.state === "READY") {
      narrate(`[ok] READY - open ${s.url ?? "(no port)"} or click "details >"`);
      creatingId = null;
    } else if (s.state === "FAILED") {
      narrate(`x FAILED - ${s.error}`);
      creatingId = null;
    }
  } catch { /* next tick retries */ }
}

/* -- live SSE log -----------------------------------------------------------*/
function startLive() {
  const log = $("livelog");
  const es = new EventSource("/events?cursor=0&follow=1&timeout=3600&limit=30");
  es.onmessage = (m) => {
    const e = JSON.parse(m.data);
    const line = `${new Date(e.ts * 1000).toLocaleTimeString()}  ${e.sandbox_id}  #${e.seq} [${e.type}] ${e.message}`;
    const div = document.createElement("div");
    div.textContent = line;
    log.appendChild(div);
    while (log.children.length > 200) log.removeChild(log.firstChild);
    log.scrollTop = log.scrollHeight;
  };
  es.addEventListener("end", () => { es.close(); setTimeout(startLive, 1000); });
  es.onerror = () => { es.close(); setTimeout(startLive, 3000); };
}

/* -- drawer -----------------------------------------------------------------*/
async function openDrawer(s) {
  selectedId = s.id;
  $("drawer").classList.remove("hidden");
  stopTerm();
  await refreshDrawer();
}
$("close").addEventListener("click", () => {
  selectedId = null;
  stopTerm();
  $("drawer").classList.add("hidden");
});

async function refreshDrawer() {
  if (!selectedId) return;
  let s;
  try { s = await api(`/sandboxes/${selectedId}`); } catch { return; }
  $("d-title").innerHTML = `${esc(s.name)} <span class="badge ${s.state}">${s.state}</span>`;
  $("d-meta").innerHTML =
    `id <code>${s.id}</code> . template <code>${s.template}</code> . isolation <code>${s.isolation}</code>` +
    (s.url ? ` . <a href="${s.url}" target="_blank">${s.url}</a>` : "") +
    (s.error ? ` . <span class="err">${esc(s.error)}</span>` : "");
  try {
    const cs = await api(`/sandboxes/${selectedId}/containers`);
    $("d-containers").innerHTML = cs.map(c => `
      <tr><td><code>${c.name}</code></td><td><span class="role">${c.role}</span></td>
      <td class="st-${c.status}">${c.status}</td><td>${c.health ?? "-"}</td>
      <td>${c.ip ?? "-"}</td><td>${c.runtime ?? "-"}</td></tr>`).join("");
    const sel = $("exec-container");
    const prev = sel.value;
    sel.innerHTML = `<option value="">(template default)</option>` +
      cs.filter(c => c.status === "running").map(c => `<option>${c.name}</option>`).join("");
    if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
  } catch (e) {
    $("d-containers").innerHTML = `<tr><td colspan="6">engine error: ${esc(e.detail)}</td></tr>`;
  }
  try {
    const ev = await api(`/sandboxes/${selectedId}/events`);
    $("d-events").innerHTML = ev.map(e => `
      <div class="event"><span class="seq">${e.seq}</span>
        <span class="type t-${e.type}">${e.type}</span><span>${esc(e.message)}</span>
        <time>${new Date(e.ts * 1000).toLocaleTimeString()}</time></div>`).join("");
  } catch { /* tolerate */ }
}

/* -- exec -------------------------------------------------------------------*/
$("exec-run").addEventListener("click", async () => {
  if (!selectedId) return;
  const cmdText = $("exec-cmd").value.trim() || "hostname";
  const container = $("exec-container").value || null;
  const timeout = parseInt($("exec-timeout").value) || 30;
  const out = $("exec-out");
  out.classList.remove("hidden");
  out.textContent = `$ ${cmdText}\n(running...)`;
  try {
    const r = await api(`/sandboxes/${selectedId}/exec`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ cmd: ["sh", "-c", cmdText], timeout, container }),
    });
    const flags = [r.exit_code !== null ? `exit=${r.exit_code}` : "exit=?",
      r.timed_out ? "TIMED OUT (killed)" : null, r.truncated ? "TRUNCATED" : null].filter(Boolean).join(" . ");
    out.innerHTML = `<span class="meta">$ ${esc(cmdText)}   [${flags}]   in ${r.container}  session ${r.session_id}</span>\n` +
      esc(r.stdout || "") + (r.stderr ? `<span class="stderr">${esc(r.stderr)}</span>` : "");
  } catch (e) {
    out.textContent = `x HTTP ${e.status}: ${e.detail}`;
  }
});
$("exec-cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") $("exec-run").click(); });

/* -- durable terminal (session + pty + resumable stream) --------------------*/
$("term-open").addEventListener("click", async () => {
  if (!selectedId) return;
  const container = $("exec-container").value || null;
  try {
    const s = await api(`/sandboxes/${selectedId}/sessions`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ cmd: ["sh"], tty: true, cols: 100, rows: 30, container }),
    });
    term = { session: s.id, since: 0, timer: null };
    $("term-out").classList.remove("hidden");
    $("term-out").textContent = `[session ${s.id} pid ${s.pid}]\n`;
    $("term-in").disabled = false; $("term-kill").disabled = false;
    $("term-in").focus();
    pumpTerm();
  } catch (e) { toast(`[!] session failed: ${esc(e.detail)}`, true); }
});
async function pumpTerm() {
  if (!term.session) return;
  try {
    const r = await fetch(`/sandboxes/${selectedId}/sessions/${term.session}/stream?since=${term.since}&follow=1&timeout=20`);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const f = JSON.parse(line);
        if (f.seq) term.since = f.seq;
        if (f.d) $("term-out").textContent += atob(f.d);
        if (f.s === "x") { $("term-out").textContent += `\n[exited ${f.code}]`; stopTerm(); return; }
        $("term-out").scrollTop = $("term-out").scrollHeight;
      }
    }
    if (term.session) term.timer = setTimeout(pumpTerm, 100);  // re-attach with since=
  } catch { if (term.session) term.timer = setTimeout(pumpTerm, 1000); }
}
$("term-in").addEventListener("keydown", async (e) => {
  if (e.key !== "Enter" || !term.session) return;
  const line = $("term-in").value + "\n";
  $("term-in").value = "";
  await fetch(`/sandboxes/${selectedId}/sessions/${term.session}/stdin`, { method: "POST", body: line });
});
$("term-kill").addEventListener("click", async () => {
  if (!term.session) return;
  await api(`/sandboxes/${selectedId}/sessions/${term.session}/signal`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ signal: "KILL" }) });
});
function stopTerm() {
  if (term.timer) clearTimeout(term.timer);
  term = { session: null, since: 0, timer: null };
  $("term-in").disabled = true; $("term-kill").disabled = true;
}

/* -- files + fingerprint ----------------------------------------------------*/
$("fs-list").addEventListener("click", async () => {
  if (!selectedId) return;
  const out = $("fs-out"); out.classList.remove("hidden");
  try {
    const r = await api(`/sandboxes/${selectedId}/fs?path=${encodeURIComponent($("fs-path").value || "/")}`);
    out.textContent = r.entries.map(e => `${e.type.padEnd(8)} ${e.mode} ${String(e.size).padStart(9)}  ${e.name}${e.link ? " -> " + e.link : ""}`).join("\n") || "(empty)";
  } catch (e) { out.textContent = `x HTTP ${e.status}: ${e.detail}`; }
});
$("fs-fp").addEventListener("click", async () => {
  if (!selectedId) return;
  const out = $("fs-out"); out.classList.remove("hidden");
  out.textContent = "hashing...";
  try {
    const r = await api(`/sandboxes/${selectedId}/fingerprint?path=${encodeURIComponent($("fs-path").value || "/")}&detail=1`);
    out.textContent = `root ${r.root}\n${r.files} files, ${r.dirs} dirs, ${r.bytes} bytes (in ${r.container})\n\n` +
      r.entries.slice(0, 200).map(e => `${e.type} ${e.hash.slice(0, 12)} ${e.mode} ${e.path}`).join("\n");
  } catch (e) { out.textContent = `x HTTP ${e.status}: ${e.detail}`; }
});

/* -- main loop --------------------------------------------------------------*/
async function tick() {
  await refreshStatus();
  await refreshSandboxes();
  await pumpCreateLog();
  if (selectedId) await refreshDrawer();
}
tick();
setInterval(tick, 2000);
startLive();
