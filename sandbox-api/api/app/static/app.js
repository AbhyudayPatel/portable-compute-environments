/* ============================================================================
 * sandbox-api console - a dependency-free SPA.
 *
 * Mental model (mirrors the backend):
 *   GET  /status                      -> header pills + pool bar
 *   GET  /sandboxes                   -> the cards
 *   GET  /sandboxes/{id}/containers   -> "what's inside" (drawer)
 *   GET  /sandboxes/{id}/events       -> timeline (drawer + create log)
 *   POST /sandboxes/{id}/exec         -> run commands from the browser
 *
 * Everything polls every 2s so you WATCH things happen instead of guessing.
 * ==========================================================================*/

const $ = (id) => document.getElementById(id);
const api = async (path, opts = {}) => {
  const r = await fetch(path, opts);
  if (r.status === 204) return null;
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, detail: body.detail || r.statusText };
  return body;
};

let selectedId = null;          // which sandbox the drawer shows
let creatingId = null;          // sandbox whose creation we're narrating
let lastEventSeq = {};          // sandboxId -> last seq rendered (create log)

/* -- toasts: small explanations of what just happened -----------------------*/
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
    $("pill-count").textContent =
      `sandboxes: ${s.sandboxes}/${s.max_sandboxes}`;
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
    cell.title = used.has(p) ? `port ${p}: in use` : `port ${p}: free`;
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
  const urlBtn = s.url && s.state === "READY"
    ? `<a href="${s.url}" target="_blank"><button>open app </button></a>` : "";
  const stopStart = s.state === "READY"
    ? `<button class="ghost" data-act="stop">stop</button>`
    : s.state === "STOPPED"
      ? `<button class="ghost" data-act="start">start</button>` : "";
  const ttl = s.ttl_seconds ? ` .  ttl ${s.ttl_seconds}s` : "";
  el.innerHTML = `
    <h3>${s.name} <span class="badge ${s.state}">${s.state}</span></h3>
    <div class="meta">
      template <code>${s.template}</code> . id <code>${s.id}</code>
      ${s.port ? ` . port <code>${s.port}</code>` : " . no port"}${ttl}
      ${s.error ? `<div style="color:#fca5a5">[!] ${s.error}</div>` : ""}
    </div>
    <div class="actions">
      ${urlBtn}
      <button class="ghost" data-act="details">details ></button>
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
      if (!confirm(`Delete ${s.name}?\n\nThis removes its containers + network, frees its port, and keeps the event log.`)) return;
      await api(`/sandboxes/${s.id}`, { method: "DELETE" });
      toast(` <b>${s.name}</b> deleted - containers & network removed, port ${s.port ?? " - "} freed.`);
    } else if (action === "stop") {
      await api(`/sandboxes/${s.id}/stop`, { method: "POST" });
      toast(` <b>${s.name}</b> stopped - containers frozen, port still reserved.`);
    } else if (action === "start") {
      await api(`/sandboxes/${s.id}/start`, { method: "POST" });
      toast(`> <b>${s.name}</b> started.`);
    }
    refreshSandboxes();
  } catch (e) {
    toast(`[!] ${action} failed: HTTP ${e.status} - ${e.detail}`, true);
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
document.querySelector('.tcard[data-t="web"]').classList.add("sel");

$("create").addEventListener("click", async () => {
  const name = $("cname").value.trim() ||
    `sb-${Math.random().toString(16).slice(2, 6)}`;
  const template = document.querySelector('input[name="template"]:checked').value;
  const ttl = parseInt($("cttl").value) || null;
  const log = $("createlog");
  log.classList.remove("hidden");
  log.innerHTML = "";
  narrate(`> POST /sandboxes {name:"${name}", template:"${template}"${ttl ? `, ttl:${ttl}` : ""}}`);
  try {
    const s = await api("/sandboxes", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, template, ttl_seconds: ttl }),
    });
    creatingId = s.id;
    lastEventSeq[s.id] = 0;
    narrate(`[ok] accepted: id=${s.id}, state=CREATING - now watch its event log:`);
  } catch (e) {
    narrate(`x HTTP ${e.status}: ${e.detail}`);
    toast(`[!] create failed: ${e.detail}`, true);
  }
});

function narrate(line) {
  const log = $("createlog");
  const div = document.createElement("div");
  div.textContent = line;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

/* While creating, stream the new sandbox's events into the create log  - 
 * the user literally watches: port allocated -> image built -> containers
 * started -> READY. */
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
  } catch { /* sandbox may not exist yet; next tick retries */ }
}

/* -- drawer: what's inside + exec + timeline -------------------------------*/
async function openDrawer(s) {
  selectedId = s.id;
  $("drawer").classList.remove("hidden");
  await refreshDrawer();
}
$("close").addEventListener("click", () => {
  selectedId = null;
  $("drawer").classList.add("hidden");
});

async function refreshDrawer() {
  if (!selectedId) return;
  let s;
  try { s = await api(`/sandboxes/${selectedId}`); }
  catch { return; }                       // deleted under us; next tick closes
  $("d-title").innerHTML =
    `${s.name} <span class="badge ${s.state}">${s.state}</span>`;
  $("d-meta").innerHTML =
    `id <code>${s.id}</code> . template <code>${s.template}</code>` +
    (s.url ? ` . <a href="${s.url}" target="_blank" style="color:#38bdf8">${s.url}</a>` : "") +
    (s.error ? ` . <span style="color:#fca5a5">${s.error}</span>` : "");

  // containers (what's inside)
  try {
    const cs = await api(`/sandboxes/${selectedId}/containers`);
    $("d-containers").innerHTML = cs.map(c => `
      <tr>
        <td><code>${c.name}</code></td>
        <td><span class="role">${c.role}</span></td>
        <td>${c.image}</td>
        <td class="st-${c.status}">${c.status}</td>
        <td>${c.health ?? " - "}</td>
      </tr>`).join("");
    // keep the exec container picker in sync
    const sel = $("exec-container");
    const prev = sel.value;
    sel.innerHTML = cs.filter(c => c.status === "running")
      .map(c => `<option>${c.name}</option>`).join("");
    if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
  } catch (e) {
    $("d-containers").innerHTML =
      `<tr><td colspan="5">engine error: ${e.detail}</td></tr>`;
  }

  // events (timeline)
  try {
    const ev = await api(`/sandboxes/${selectedId}/events`);
    $("d-events").innerHTML = ev.map(e => `
      <div class="event">
        <span class="seq">${e.seq}</span>
        <span class="type t-${e.type}">${e.type}</span>
        <span>${e.message}</span>
        <time>${new Date(e.ts * 1000).toLocaleTimeString()}</time>
      </div>`).join("");
  } catch { /* tolerate */ }
}

/* -- exec from the browser -------------------------------------------------*/
$("exec-run").addEventListener("click", async () => {
  if (!selectedId) return;
  const cmdText = $("exec-cmd").value.trim() || "hostname";
  const container = $("exec-container").value || null;
  const timeout = parseInt($("exec-timeout").value) || 30;
  const out = $("exec-out");
  out.classList.remove("hidden");
  out.textContent = `$ ${cmdText}\n(running in ${container ?? "app container"}...)`;
  try {
    const r = await api(`/sandboxes/${selectedId}/exec`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      // the console always wraps your text in sh -c so pipes/; just work
      body: JSON.stringify({ cmd: ["sh", "-c", cmdText], timeout, container }),
    });
    const flags = [
      r.exit_code !== null ? `exit=${r.exit_code}` : "exit=?",
      r.timed_out ? "TIMED OUT" : null,
      r.truncated ? "TRUNCATED at 64KiB" : null,
    ].filter(Boolean).join(" . ");
    out.innerHTML =
      `<span class="meta">$ ${cmdText}   [${flags}]   in ${r.container}</span>\n` +
      `${r.stdout ? escapeHtml(r.stdout) : ""}` +
      (r.stderr ? `<span class="stderr">${escapeHtml(r.stderr)}</span>` : "");
  } catch (e) {
    out.textContent = `x HTTP ${e.status}: ${e.detail}`;
  }
});
function escapeHtml(s) {
  return s.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
$("exec-cmd").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("exec-run").click();
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
