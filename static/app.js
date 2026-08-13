const STATUSES = [
  { id: "new", label: "Новый", color: "#8c9285" },
  { id: "proposal_sent", label: "Предложение отправлено", color: "#5c78d8" },
  { id: "interested", label: "Есть интерес", color: "#b58c1c" },
  { id: "follow_up", label: "Дожим", color: "#b6632f" },
  { id: "diagnostics", label: "Диагностика", color: "#8a65c7" },
  { id: "proposal", label: "КП", color: "#d1773f" },
  { id: "negotiations", label: "Переговоры", color: "#257f7b" },
  { id: "won", label: "Успех", color: "#438a43" },
  { id: "lost", label: "Отказ", color: "#a65a50" },
];

const state = { leads: [], search: "", draggedId: null, view: "board", period: "week", dashboard: null };
const board = document.querySelector("#board");
const metrics = document.querySelector("#metrics");
const notice = document.querySelector("#notice");
const dialog = document.querySelector("#leadDialog");
const form = document.querySelector("#leadForm");
const statusSelect = document.querySelector("#statusSelect");
const loginOverlay = document.querySelector("#loginOverlay");
const loginForm = document.querySelector("#loginForm");
const loginError = document.querySelector("#loginError");
const logoutButton = document.querySelector("#logoutButton");
const boardView = document.querySelector("#boardView");
const dashboardView = document.querySelector("#dashboardView");
const dashboardCards = document.querySelector("#dashboardCards");
const timelineChart = document.querySelector("#timelineChart");
const funnelList = document.querySelector("#funnelList");
const activityList = document.querySelector("#activityList");
const dashboardPeriod = document.querySelector("#dashboardPeriod");
const searchWrap = document.querySelector(".search-wrap");
const addLeadButton = document.querySelector("#addLeadButton");

const STATUS_BY_ID = Object.fromEntries(STATUSES.map((status) => [status.id, status]));

statusSelect.innerHTML = STATUSES.map((status) => `<option value="${status.id}">${status.label}</option>`).join("");

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function formatMoney(value) {
  return value ? `${new Intl.NumberFormat("ru-RU").format(value)} ₽` : "";
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(date);
}

function formatFullDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function isOverdue(value) {
  return value && new Date(value).getTime() < Date.now();
}

function showNotice(message, isError = false) {
  notice.textContent = message;
  notice.style.background = isError ? "#ffe0da" : "#e8f6cf";
  notice.classList.remove("hidden");
  clearTimeout(showNotice.timer);
  showNotice.timer = setTimeout(() => notice.classList.add("hidden"), 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const apiError = new Error(error.detail || `Ошибка ${response.status}`);
    apiError.status = response.status;
    throw apiError;
  }
  return response.status === 204 ? null : response.json();
}

function visibleLeads() {
  const query = state.search.trim().toLocaleLowerCase("ru");
  if (!query) return state.leads;
  return state.leads.filter((lead) => [lead.company, lead.vacancy, lead.offer, lead.notes, lead.contact_name]
    .some((value) => (value || "").toLocaleLowerCase("ru").includes(query)));
}

function cardTemplate(lead) {
  const nextDate = formatDate(lead.next_action_at);
  return `
    <article class="lead-card" draggable="true" data-id="${lead.id}" tabindex="0">
      <div class="card-top">
        <h3>${escapeHtml(lead.company)}</h3>
        <span class="source-badge">${escapeHtml(lead.source || "лид")}</span>
      </div>
      ${lead.vacancy ? `<p class="vacancy">${escapeHtml(lead.vacancy)}</p>` : ""}
      ${lead.offer ? `<p class="offer">${escapeHtml(lead.offer)}</p>` : ""}
      <div class="card-footer">
        <span class="next-action ${isOverdue(lead.next_action_at) ? "overdue" : ""}">
          ${nextDate ? `◷ ${escapeHtml(nextDate)}` : lead.next_action ? "→ Есть следующий шаг" : ""}
        </span>
        <span class="budget">${formatMoney(lead.budget)}</span>
      </div>
    </article>`;
}

function render() {
  const leads = visibleLeads();
  board.innerHTML = STATUSES.map((status) => {
    const columnLeads = leads.filter((lead) => lead.status === status.id);
    return `
      <section class="column" data-status="${status.id}" style="--column-color:${status.color}">
        <header class="column-header">
          <div class="column-title"><span class="column-dot"></span>${status.label}</div>
          <span class="column-count">${columnLeads.length}</span>
        </header>
        <div class="card-list">
          ${columnLeads.length ? columnLeads.map(cardTemplate).join("") : '<div class="empty-column">Перетащите лид сюда</div>'}
        </div>
      </section>`;
  }).join("");

  const active = state.leads.filter((lead) => !["won", "lost"].includes(lead.status)).length;
  const interested = state.leads.filter((lead) => ["interested", "follow_up", "diagnostics", "proposal", "negotiations"].includes(lead.status)).length;
  const pipeline = state.leads.filter((lead) => !["lost"].includes(lead.status)).reduce((sum, lead) => sum + (lead.budget || 0), 0);
  metrics.innerHTML = `
    <div class="metric"><strong>${active}</strong><span>активных</span></div>
    <div class="metric"><strong>${interested}</strong><span>в работе</span></div>
    <div class="metric"><strong>${pipeline ? formatMoney(pipeline) : "—"}</strong><span>в воронке</span></div>`;

  bindBoardEvents();
}

function metricCard(value, label, note = "") {
  return `<article class="dashboard-card"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span>${note ? `<small>${escapeHtml(note)}</small>` : ""}</article>`;
}

function renderDashboard() {
  const data = state.dashboard;
  if (!data) return;
  const metric = data.metrics;
  dashboardCards.innerHTML = [
    metricCard(metric.created, "Новых лидов", "добавлено за период"),
    metricCard(metric.proposals_sent, "Предложений", "отправлено за период"),
    metricCard(metric.interested, "Есть интерес", "переходов за период"),
    metricCard(metric.follow_ups, "Дожим", "переведено за период"),
    metricCard(metric.won, "Успехов", "оплаченных результатов"),
    metricCard(`${metric.success_rate}%`, "Конверсия в успех", "успехи / новые лиды"),
  ].join("");

  dashboardPeriod.textContent = data.start_at
    ? `${formatFullDate(data.start_at)} — ${formatFullDate(data.end_at)} · ${data.timezone}`
    : `За всё время · обновлено ${formatFullDate(data.end_at)}`;

  const timeline = data.timeline || [];
  const maxActivity = Math.max(1, ...timeline.map((item) => item.activities));
  timelineChart.innerHTML = timeline.length ? timeline.map((item) => `
    <div class="timeline-column" title="${escapeHtml(item.label)}: действий ${item.activities}, новых ${item.created}, успехов ${item.won}">
      <div class="timeline-value">${item.activities}</div>
      <div class="timeline-track">
        <div class="timeline-bar" style="height:${Math.max(8, item.activities / maxActivity * 100)}%"></div>
      </div>
      <div class="timeline-label">${escapeHtml(item.label)}</div>
    </div>`).join("") : '<div class="analytics-empty">За выбранный период действий пока нет</div>';

  const funnel = data.current_funnel || {};
  const maxFunnel = Math.max(1, ...STATUSES.map((status) => funnel[status.id] || 0));
  funnelList.innerHTML = STATUSES.map((status) => {
    const count = funnel[status.id] || 0;
    return `<div class="funnel-row">
      <div class="funnel-name"><span style="background:${status.color}"></span>${status.label}</div>
      <div class="funnel-track"><div style="width:${count / maxFunnel * 100}%;background:${status.color}"></div></div>
      <strong>${count}</strong>
    </div>`;
  }).join("");

  const events = data.recent_events || [];
  activityList.innerHTML = events.length ? events.map((event) => {
    const from = STATUS_BY_ID[event.from_status]?.label;
    const to = STATUS_BY_ID[event.to_status]?.label;
    let action = "Лид добавлен";
    if (event.event_type === "status_changed") action = `${from || "Создан"} → ${to || "—"}`;
    if (event.event_type === "baseline_status") action = `Исходный статус: ${to || "—"}`;
    return `<div class="activity-row">
      <div><strong>${escapeHtml(event.company)}</strong><span>${escapeHtml(action)}</span></div>
      <time>${escapeHtml(formatFullDate(event.occurred_at))}</time>
    </div>`;
  }).join("") : '<div class="analytics-empty">История за выбранный период пуста</div>';
}

async function loadDashboard() {
  dashboardCards.innerHTML = '<div class="analytics-empty">Загружаем аналитику…</div>';
  try {
    state.dashboard = await api(`/api/dashboard?period=${state.period}`);
    renderDashboard();
  } catch (error) {
    showNotice(`Не удалось загрузить дашборд: ${error.message}`, true);
  }
}

async function switchView(view) {
  state.view = view;
  const isDashboard = view === "dashboard";
  boardView.classList.toggle("hidden", isDashboard);
  dashboardView.classList.toggle("hidden", !isDashboard);
  searchWrap.classList.toggle("hidden", isDashboard);
  addLeadButton.classList.toggle("hidden", isDashboard);
  metrics.classList.toggle("hidden", isDashboard);
  document.querySelectorAll(".view-button").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  document.querySelector("#heroEyebrow").textContent = isDashboard ? "Аналитика продаж" : "Воронка продаж";
  document.querySelector("#heroTitle").textContent = isDashboard ? "Результаты и эффективность" : "Лиды и следующие шаги";
  document.querySelector("#heroText").textContent = isDashboard
    ? "Сколько лидов найдено, обработано и доведено до результата."
    : "От первого контакта до оплаченного AI-проекта — без потерянных договорённостей.";
  if (isDashboard) await loadDashboard();
}

function bindBoardEvents() {
  document.querySelectorAll(".lead-card").forEach((card) => {
    card.addEventListener("click", () => openEdit(Number(card.dataset.id)));
    card.addEventListener("keydown", (event) => { if (event.key === "Enter") openEdit(Number(card.dataset.id)); });
    card.addEventListener("dragstart", () => {
      state.draggedId = Number(card.dataset.id);
      card.classList.add("dragging");
    });
    card.addEventListener("dragend", () => {
      state.draggedId = null;
      card.classList.remove("dragging");
      document.querySelectorAll(".column").forEach((column) => column.classList.remove("drag-over"));
    });
  });

  document.querySelectorAll(".column").forEach((column) => {
    column.addEventListener("dragover", (event) => { event.preventDefault(); column.classList.add("drag-over"); });
    column.addEventListener("dragleave", () => column.classList.remove("drag-over"));
    column.addEventListener("drop", async (event) => {
      event.preventDefault();
      column.classList.remove("drag-over");
      if (!state.draggedId) return;
      await moveLead(state.draggedId, column.dataset.status);
    });
  });
}

async function moveLead(id, status) {
  const lead = state.leads.find((item) => item.id === id);
  if (!lead || lead.status === status) return;
  const previous = lead.status;
  lead.status = status;
  render();
  try {
    const updated = await api(`/api/leads/${id}`, { method: "PATCH", body: JSON.stringify({ status }) });
    Object.assign(lead, updated);
  } catch (error) {
    lead.status = previous;
    render();
    showNotice(error.message, true);
  }
}

function resetForm() {
  form.reset();
  form.elements.source.value = "hh.ru";
  form.elements.status.value = "new";
  form.elements.id.value = "";
}

function openCreate() {
  resetForm();
  document.querySelector("#dialogEyebrow").textContent = "Новый лид";
  document.querySelector("#dialogTitle").textContent = "Добавить компанию";
  document.querySelector("#deleteLeadButton").classList.add("hidden");
  document.querySelector("#sourceLink").classList.add("hidden");
  dialog.showModal();
  form.elements.company.focus();
}

function toLocalDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 16);
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function openEdit(id) {
  const lead = state.leads.find((item) => item.id === id);
  if (!lead) return;
  resetForm();
  Object.entries(lead).forEach(([key, value]) => {
    if (form.elements[key]) form.elements[key].value = key === "next_action_at" ? toLocalDateTime(value) : (value ?? "");
  });
  document.querySelector("#dialogEyebrow").textContent = "Карточка лида";
  document.querySelector("#dialogTitle").textContent = lead.company;
  document.querySelector("#deleteLeadButton").classList.remove("hidden");
  const sourceLink = document.querySelector("#sourceLink");
  if (lead.source_url) {
    sourceLink.href = lead.source_url;
    sourceLink.classList.remove("hidden");
  } else {
    sourceLink.classList.add("hidden");
  }
  dialog.showModal();
}

function formPayload() {
  const data = Object.fromEntries(new FormData(form).entries());
  delete data.id;
  data.budget = data.budget ? Number(data.budget) : null;
  data.next_action_at = data.next_action_at ? new Date(data.next_action_at).toISOString() : null;
  data.source_url = data.source_url || null;
  return data;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = Number(form.elements.id.value);
  try {
    const lead = await api(id ? `/api/leads/${id}` : "/api/leads", {
      method: id ? "PATCH" : "POST",
      body: JSON.stringify(formPayload()),
    });
    if (id) state.leads[state.leads.findIndex((item) => item.id === id)] = lead;
    else state.leads.unshift(lead);
    dialog.close();
    render();
    showNotice(id ? "Карточка обновлена" : "Лид добавлен");
  } catch (error) {
    showNotice(error.message, true);
  }
});

document.querySelector("#deleteLeadButton").addEventListener("click", async () => {
  const id = Number(form.elements.id.value);
  const lead = state.leads.find((item) => item.id === id);
  if (!lead || !window.confirm(`Удалить лид «${lead.company}»?`)) return;
  try {
    await api(`/api/leads/${id}`, { method: "DELETE" });
    state.leads = state.leads.filter((item) => item.id !== id);
    dialog.close();
    render();
    showNotice("Лид удалён");
  } catch (error) { showNotice(error.message, true); }
});

addLeadButton.addEventListener("click", openCreate);
document.querySelector("#closeDialog").addEventListener("click", () => dialog.close());
document.querySelector("#cancelDialog").addEventListener("click", () => dialog.close());
document.querySelector("#search").addEventListener("input", (event) => { state.search = event.target.value; render(); });
dialog.addEventListener("click", (event) => {
  const rect = dialog.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
});

document.querySelectorAll(".view-button").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});

document.querySelectorAll(".period-button").forEach((button) => {
  button.addEventListener("click", async () => {
    state.period = button.dataset.period;
    document.querySelectorAll(".period-button").forEach((item) => item.classList.toggle("active", item === button));
    await loadDashboard();
  });
});

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.classList.add("hidden");
  try {
    await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ password: document.querySelector("#loginPassword").value }),
    });
    document.querySelector("#loginPassword").value = "";
    loginOverlay.classList.add("hidden");
    logoutButton.classList.remove("hidden");
    await loadLeads();
  } catch (error) {
    loginError.textContent = error.message;
    loginError.classList.remove("hidden");
  }
});

logoutButton.addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  state.leads = [];
  render();
  loginOverlay.classList.remove("hidden");
  logoutButton.classList.add("hidden");
  document.querySelector("#loginPassword").focus();
});

async function loadLeads() {
  try {
    state.leads = await api("/api/leads");
    render();
  } catch (error) {
    if (error.status === 401) {
      loginOverlay.classList.remove("hidden");
      return;
    }
    showNotice(`Не удалось загрузить CRM: ${error.message}`, true);
  }
}

async function bootstrap() {
  try {
    const auth = await api("/api/auth/status");
    if (auth.required) logoutButton.classList.toggle("hidden", !auth.authenticated);
    if (!auth.configured || !auth.authenticated) {
      loginOverlay.classList.remove("hidden");
      if (!auth.configured) {
        loginError.textContent = "В настройках Vercel необходимо добавить переменную CRM_PASSWORD.";
        loginError.classList.remove("hidden");
      }
      return;
    }
    await loadLeads();
  } catch (error) {
    showNotice(`Не удалось запустить CRM: ${error.message}`, true);
  }
}

bootstrap();
