const STATUSES = [
  { id: "new", label: "Новый", color: "#8c9285" },
  { id: "proposal_sent", label: "Предложение отправлено", color: "#5c78d8" },
  { id: "interested", label: "Есть интерес", color: "#b58c1c" },
  { id: "diagnostics", label: "Диагностика", color: "#8a65c7" },
  { id: "proposal", label: "КП", color: "#d1773f" },
  { id: "negotiations", label: "Переговоры", color: "#257f7b" },
  { id: "won", label: "Успех", color: "#438a43" },
  { id: "lost", label: "Отказ", color: "#a65a50" },
];

const state = { leads: [], search: "", draggedId: null };
const board = document.querySelector("#board");
const metrics = document.querySelector("#metrics");
const notice = document.querySelector("#notice");
const dialog = document.querySelector("#leadDialog");
const form = document.querySelector("#leadForm");
const statusSelect = document.querySelector("#statusSelect");

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
    throw new Error(error.detail || `Ошибка ${response.status}`);
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
  const interested = state.leads.filter((lead) => ["interested", "diagnostics", "proposal", "negotiations"].includes(lead.status)).length;
  const pipeline = state.leads.filter((lead) => !["lost"].includes(lead.status)).reduce((sum, lead) => sum + (lead.budget || 0), 0);
  metrics.innerHTML = `
    <div class="metric"><strong>${active}</strong><span>активных</span></div>
    <div class="metric"><strong>${interested}</strong><span>в работе</span></div>
    <div class="metric"><strong>${pipeline ? formatMoney(pipeline) : "—"}</strong><span>в воронке</span></div>`;

  bindBoardEvents();
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

document.querySelector("#addLeadButton").addEventListener("click", openCreate);
document.querySelector("#closeDialog").addEventListener("click", () => dialog.close());
document.querySelector("#cancelDialog").addEventListener("click", () => dialog.close());
document.querySelector("#search").addEventListener("input", (event) => { state.search = event.target.value; render(); });
dialog.addEventListener("click", (event) => {
  const rect = dialog.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
});

async function load() {
  try {
    state.leads = await api("/api/leads");
    render();
  } catch (error) {
    showNotice(`Не удалось загрузить CRM: ${error.message}`, true);
  }
}

load();
