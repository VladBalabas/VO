export const modalities = ["CT", "MRI", "XR", "US", "PET"];

export const statuses = [
  { value: "created", label: "Створено" },
  { value: "scheduled", label: "Заплановано" },
  { value: "completed", label: "Завершено" },
  { value: "cancelled", label: "Скасовано" },
];

export function statusLabel(value) {
  return statuses.find((status) => status.value === value)?.label ?? value;
}

export function normalizeReferral(raw) {
  return {
    patient_code: raw.patient_code.trim(),
    modality: raw.modality,
    priority: Number(raw.priority),
    doctor: raw.doctor.trim(),
    issued_at: raw.issued_at,
    status: raw.status,
  };
}

export function validateReferral(referral) {
  if (!referral.patient_code || referral.patient_code.length > 64) {
    throw new Error("Код пацієнта має містити від 1 до 64 символів.");
  }
  if (!modalities.includes(referral.modality)) {
    throw new Error("Оберіть коректну модальність.");
  }
  if (!Number.isInteger(referral.priority) || referral.priority < 1 || referral.priority > 5) {
    throw new Error("Пріоритет має бути цілим числом від 1 до 5.");
  }
  if (!referral.doctor || referral.doctor.length > 120) {
    throw new Error("Поле лікаря має містити від 1 до 120 символів.");
  }
  if (!referral.issued_at || !statuses.some((status) => status.value === referral.status)) {
    throw new Error("Заповніть дату й оберіть коректний статус.");
  }
  return referral;
}
