// CoreApp frontend. Talks to the backend through same-origin /api/*,
// which the frontend nginx container proxies to the backend service.
const taskList = document.getElementById("task-list");
const taskForm = document.getElementById("task-form");
const taskTitle = document.getElementById("task-title");
const apiStatus = document.getElementById("api-status");
const dbStatus = document.getElementById("db-status");

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  if (response.status === 204) return null;
  return response.json();
}

function setStatus(el, label, ok) {
  el.textContent = label;
  el.className = `pill ${ok ? "pill-ok" : "pill-bad"}`;
}

async function refreshHealth() {
  try {
    const health = await api("/health");
    setStatus(apiStatus, "API: up", true);
    setStatus(dbStatus, `DB: ${health.database}`, health.database === "up");
  } catch {
    setStatus(apiStatus, "API: down", false);
    setStatus(dbStatus, "DB: unknown", false);
  }
}

function renderTask(task) {
  const li = document.createElement("li");
  li.className = task.done ? "task done" : "task";

  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = task.done;
  toggle.addEventListener("change", async () => {
    await api(`/tasks/${task.id}/toggle`, { method: "PATCH" });
    await loadTasks();
  });

  const title = document.createElement("span");
  title.className = "title";
  title.textContent = task.title;

  const remove = document.createElement("button");
  remove.textContent = "×";
  remove.className = "delete";
  remove.title = "Delete task";
  remove.addEventListener("click", async () => {
    await api(`/tasks/${task.id}`, { method: "DELETE" });
    await loadTasks();
  });

  li.append(toggle, title, remove);
  return li;
}

async function loadTasks() {
  try {
    const tasks = await api("/tasks");
    taskList.replaceChildren(...tasks.map(renderTask));
  } catch (err) {
    console.error("Failed to load tasks:", err);
  }
}

taskForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = taskTitle.value.trim();
  if (!title) return;
  await api("/tasks", { method: "POST", body: JSON.stringify({ title }) });
  taskTitle.value = "";
  await loadTasks();
});

refreshHealth();
loadTasks();
setInterval(refreshHealth, 10000);
