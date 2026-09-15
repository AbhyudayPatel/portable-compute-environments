/* ============================================================================
 * sandbox-scheduler console - dependency-free SPA.
 *
 *   GET  /status     -> header pills + capacity bars + tenant chips
 *   GET  /queue      -> queued jobs with LIVE effective priority (aging)
 *   GET  /jobs       -> jobs table (live + recent terminal)
 *   GET  /metrics    -> metric cards
 *   POST /jobs       -> submit form (task/service, priority, gang count)
 *   POST /jobs/{id}/cancel
 *
 * Polls every 2s. Watch the queue's "+aged" column tick upward - that is
 * the starvation-proofing working in front of you.
 * ==========================================================================*/

const $ = (id) => document.getElementById(id);
const api = async (path, opts = {}) => {
  const r = await fetch(path, opts);
  if (r.status === 204) return null;
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, detail: body.detail || r.statusText };
  return body;
};

let selectedJob = null;

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.innerHTML = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

/* -- status + capacity ------------------------------------------------------*/
async function refreshStatus() {
  try {
    const s = await api("/status");
    setPill("pill-sched", `scheduler: ${s.scheduler}`, true);
    setPill("pill-upstream", `upstream: ${s.upstream}`, s.upstream === "up");
    $("pill-queue").textContent = `queue: ${s.queue_depth}`;
    const c = s.capacity;
    bar("cap-cpu", c.cpu_reserved, c.cpu_total,
        `${c.cpu_reserved}/${c.cpu_total} reserved`);
    bar("cap-slots", c.slots_used, c.slots_total,
        `${c.slots_used}/${c.slots_total} used (incl. unmanaged)`);
    bar("cap-ports", c.ports_free, 10, `${c.ports_free} free`);
    $("tenants").innerHTML = Object.entries(s.tenants).map(
      ([t, n]) => `<span class="tenant">${t}: ${n}/${s.max_per_tenant}</span>`
    ).join("") || `<span class="tenant">no tenants active</span>`;
  } catch {
    setPill("pill-sched", "scheduler: unreachable", false);
  }
}
function setPill(id, text, up) {
  const el = $(id); el.textContent = text; el.className = "pill " + (up ? "up" : "down");
}
function bar(id, used, total, label) {
  $(id).style.width = total > 0 ? Math.min(100, 100 * used / total) + "%" : "0%";
  $(id + "-t").textContent = label;
}

/* -- queue (with live aging) ------------------------------------------------*/
async function refreshQueue() {
  const q = await api("/queue");
  $("queue-empty").style.display = q.length ? "none" : "block";
  $("queue").innerHTML = q.map((j, i) => `
    <tr>
      <td>${i + 1}</td>
      <td>${j.name} <code>${j.id}</code></td>
      <td>${j.tenant}</td>
      <td>${j.template}x${j.count}</td>
      <td>${j.cpu_units * j.count}</td>
      <td>${j.priority}</td>
      <td style="color:#fbbf24">+${j.aging_bonus}</td>
      <td><b>${j.effective_priority}</b></td>
      <td>${j.age_seconds}</td>
      <td><button class="danger" onclick="cancelJob('${j.id}')">x</button></td>
    </tr>`).join("");
}

/* -- jobs table -------------------------------------------------------------*/
async function refreshJobs() {
  const jobs = await api("/jobs");
  $("jobs-empty").style.display = jobs.length ? "none" : "block";
  $("jobs").innerHTML = jobs.map(j => `
    <tr onclick="openDrawer('${j.id}')">
      <td>${j.name} <code>${j.id}</code></td>
      <td>${j.tenant}</td>
      <td>${j.kind}</td>
      <td>${j.template}x${j.count}</td>
      <td><span class="badge ${j.state}">${j.state}</span></td>
      <td>${j.wait_seconds ?? "-"}s</td>
      <td>${(j.sandboxes || []).map(sb =>
        `<span class="sandbox-chip">${sb.name || sb.id.slice(0,8)} ${sb.state || ""}</span>`).join("")}</td>
      <td>${["QUEUED","ADMITTED","RUNNING"].includes(j.state)
        ? `<button class="danger" onclick="event.stopPropagation();cancelJob('${j.id}')">cancel</button>` : ""}</td>
    </tr>`).join("");
}

async function cancelJob(jid) {
  try {
    const j = await api(`/jobs/${jid}/cancel`, { method: "POST" });
    toast(`job <code>${jid}</code> -> ${j.state}`);
    tick();
  } catch (e) { toast(`cancel failed: HTTP ${e.status} - ${e.detail}`, true); }
}

/* -- submit -----------------------------------------------------------------*/
$("f-prio").addEventListener("input", () => $("f-prio-v").textContent = $("f-prio").value);

async function submitJob(overrides = {}) {
  const body = {
    tenant: $("f-tenant").value.trim() || "default",
    template: $("f-template").value,
    kind: $("f-kind").value,
    priority: parseInt($("f-prio").value),
    count: parseInt($("f-count").value) || 1,
    max_runtime: parseInt($("f-rt").value) || null,
    ...overrides,
  };
  const name = $("f-name").value.trim();
  if (name) body.name = name;
  if (body.kind === "task" && !("cmd" in overrides)) {
    const cmdText = $("f-cmd").value.trim();
    body.cmd = ["sh", "-c", cmdText || "echo hello"];
  }
  const log = $("submitlog");
  log.classList.remove("hidden");
  const line = document.createElement("div");
  try {
    const j = await api("/jobs", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    line.textContent = `> accepted: ${j.name} [${j.id}] ${j.template}x${j.count} prio=${j.priority} -> QUEUED`;
  } catch (e) {
    line.textContent = `> rejected: HTTP ${e.status} - ${e.detail}`;
    line.style.color = "#fca5a5";
  }
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
  tick();
}

$("submit").addEventListener("click", () => submitJob());
$("herd").addEventListener("click", () => {
  for (let i = 0; i < 12; i++) {
    submitJob({ kind: "task", template: "blank",
                cmd: ["sh", "-c", `echo herd-$((Math.random()*1e6|0)); sleep 4`] });
  }
  toast("herd submitted: 12 task jobs - watch the queue + capacity bars");
});
$("fairdemo").addEventListener("click", async () => {
  for (let i = 0; i < 6; i++) {
    await submitJob({ tenant: "tenant-a", kind: "service", template: "blank",
                      name: `fa-${i}`, max_runtime: 180 });
  }
  await submitJob({ tenant: "tenant-b", kind: "service", template: "blank",
                    name: "fb-0", max_runtime: 180 });
  toast("fairness demo: tenant-a floods, tenant-b must still get in. Watch the jobs table.");
});

/* -- drawer -----------------------------------------------------------------*/
async function openDrawer(jid) { selectedJob = jid; $("drawer").classList.remove("hidden"); refreshDrawer(); }
$("close").addEventListener("click", () => { selectedJob = null; $("drawer").classList.add("hidden"); });

async function refreshDrawer() {
  if (!selectedJob) return;
  let j;
  try { j = await api(`/jobs/${selectedJob}`); } catch { return; }
  $("d-title").innerHTML = `${j.name} <code>${j.id}</code> <span class="badge ${j.state}">${j.state}</span>`;
  $("d-meta").innerHTML =
    `tenant <code>${j.tenant}</code> . kind <code>${j.kind}</code> . template <code>${j.template}</code> x${j.count}` +
    ` . prio ${j.priority} . wait ${j.wait_seconds ?? "-"}s` +
    (j.error ? ` . <span style="color:#fca5a5">${j.error}</span>` : "");
  $("d-sandboxes").innerHTML = (j.sandboxes || []).map(sb =>
    `<span class="sandbox-chip">${sb.name || sb.id} <b>${sb.state || ""}</b>${sb.url ? ` <a href="${sb.url}" target="_blank" style="color:#fbbf24">${sb.url}</a>` : ""}</span>`
  ).join("") || '<span class="sandbox-chip">none yet</span>';
  const res = $("d-result");
  if (j.result) {
    res.classList.remove("hidden");
    res.textContent = JSON.stringify(j.result, null, 2);
  } else res.classList.add("hidden");
  try {
    const ev = await api(`/jobs/${selectedJob}/events`);
    $("d-events").innerHTML = ev.map(e => `
      <div class="event">
        <span class="seq">${e.seq}</span>
        <span class="type t-${e.type}">${e.type}</span>
        <span>${e.message}</span>
        <time>${new Date(e.ts * 1000).toLocaleTimeString()}</time>
      </div>`).join("");
  } catch {}
}

/* -- metrics ----------------------------------------------------------------*/
async function refreshMetrics() {
  const m = await api("/metrics");
  const cards = [
    ["queue depth", m.queue_depth],
    ["upstream", m.upstream_down ? "DOWN" : "up"],
    ["wait p50", (m.wait_seconds.p50 ?? "-") + "s"],
    ["wait p95", (m.wait_seconds.p95 ?? "-") + "s"],
    ["wait p99", (m.wait_seconds.p99 ?? "-") + "s"],
    ["finished / 5m", m.throughput_finished_last_5m],
    ...Object.entries(m.state_counts).map(([k, v]) => [k.toLowerCase(), v]),
  ];
  $("metrics").innerHTML = cards.map(([k, v]) =>
    `<div class="mcard"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("");
}

/* -- main loop --------------------------------------------------------------*/
async function tick() {
  await refreshStatus();
  await refreshQueue();
  await refreshJobs();
  await refreshMetrics();
  if (selectedJob) await refreshDrawer();
}
tick();
setInterval(tick, 2000);
