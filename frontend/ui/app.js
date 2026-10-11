import {
  createReferral,
  deleteReferral,
  listReferrals,
  replaceReferral,
} from "../data/referral-api.js";
import {
  modalities,
  normalizeReferral,
  statusLabel,
  statuses,
  validateReferral,
} from "../domain/referral.js";

const elements = {
  form: document.querySelector("#referral-form"),
  formTitle: document.querySelector("#form-title"),
  id: document.querySelector("#referral-id"),
  patientCode: document.querySelector("#patient-code"),
  modality: document.querySelector("#modality"),
  priority: document.querySelector("#priority"),
  doctor: document.querySelector("#doctor"),
  issuedAt: document.querySelector("#issued-at"),
  status: document.querySelector("#status"),
  statusFilter: document.querySelector("#status-filter"),
  cancelEdit: document.querySelector("#cancel-edit"),
  body: document.querySelector("#referrals-body"),
  total: document.querySelector("#total-count"),
  message: document.querySelector("#message"),
  empty: document.querySelector("#empty-state"),
};

let referrals = [];

function option(value, label) {
  const item = document.createElement("option");
  item.value = value;
  item.textContent = label;
  return item;
}

function initializeSelects() {
  modalities.forEach((modality) => elements.modality.append(option(modality, modality)));
  statuses.forEach(({ value, label }) => elements.status.append(option(value, label)));
  elements.statusFilter.append(option("", "Усі статуси"));
  statuses.forEach(({ value, label }) => elements.statusFilter.append(option(value, label)));
  elements.issuedAt.value = new Date().toISOString().slice(0, 10);
}

function showMessage(text, kind = "success") {
  elements.message.textContent = text;
  elements.message.className = `message ${kind}`;
}

function clearMessage() {
  elements.message.className = "message hidden";
}

function cell(text) {
  const item = document.createElement("td");
  item.textContent = text;
  return item;
}

function actionButton(label, action, id, className = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.dataset.action = action;
  button.dataset.id = String(id);
  button.className = `table-action ${className}`.trim();
  return button;
}

function render() {
  elements.body.replaceChildren();
  elements.total.textContent = String(referrals.length);
  elements.empty.classList.toggle("hidden", referrals.length !== 0);

  referrals.forEach((referral) => {
    const row = document.createElement("tr");
    row.append(
      cell(referral.id),
      cell(referral.patient_code),
      cell(referral.modality),
      cell(referral.priority),
      cell(referral.doctor),
      cell(referral.issued_at),
    );

    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `status status-${referral.status}`;
    badge.textContent = statusLabel(referral.status);
    statusCell.append(badge);
    row.append(statusCell);

    const actions = document.createElement("td");
    actions.className = "actions";
    actions.append(
      actionButton("Змінити", "edit", referral.id),
      actionButton("Видалити", "delete", referral.id, "danger"),
    );
    row.append(actions);
    elements.body.append(row);
  });
}

async function refresh() {
  try {
    const result = await listReferrals(elements.statusFilter.value);
    referrals = result.items;
    elements.total.textContent = String(result.total);
    render();
  } catch (error) {
    showMessage(`Не вдалося завантажити дані: ${error.message}`, "error");
  }
}

function resetForm() {
  elements.form.reset();
  elements.id.value = "";
  elements.priority.value = "3";
  elements.status.value = "created";
  elements.issuedAt.value = new Date().toISOString().slice(0, 10);
  elements.formTitle.textContent = "Нове направлення";
  elements.cancelEdit.classList.add("hidden");
}

function editReferral(id) {
  const referral = referrals.find((item) => item.id === id);
  if (!referral) return;
  elements.id.value = String(referral.id);
  elements.patientCode.value = referral.patient_code;
  elements.modality.value = referral.modality;
  elements.priority.value = String(referral.priority);
  elements.doctor.value = referral.doctor;
  elements.issuedAt.value = referral.issued_at;
  elements.status.value = referral.status;
  elements.formTitle.textContent = `Редагування #${referral.id}`;
  elements.cancelEdit.classList.remove("hidden");
  elements.form.scrollIntoView({ behavior: "smooth", block: "start" });
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearMessage();
  try {
    const referral = validateReferral(normalizeReferral({
      patient_code: elements.patientCode.value,
      modality: elements.modality.value,
      priority: elements.priority.value,
      doctor: elements.doctor.value,
      issued_at: elements.issuedAt.value,
      status: elements.status.value,
    }));
    const id = Number(elements.id.value);
    if (id) {
      await replaceReferral(id, referral);
      showMessage(`Направлення #${id} оновлено.`);
    } else {
      await createReferral(referral);
      showMessage("Направлення створено.");
    }
    resetForm();
    await refresh();
  } catch (error) {
    showMessage(error.message, "error");
  }
});

elements.body.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = Number(button.dataset.id);
  if (button.dataset.action === "edit") {
    editReferral(id);
    return;
  }
  if (button.dataset.action === "delete" && window.confirm(`Видалити направлення #${id}?`)) {
    try {
      await deleteReferral(id);
      showMessage(`Направлення #${id} видалено.`);
      await refresh();
    } catch (error) {
      showMessage(error.message, "error");
    }
  }
});

elements.cancelEdit.addEventListener("click", resetForm);
elements.statusFilter.addEventListener("change", refresh);

initializeSelects();
resetForm();
refresh();
