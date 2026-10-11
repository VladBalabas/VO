export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers ?? {}) },
    ...options,
  });
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof body?.detail === "string" ? body.detail : `HTTP ${response.status}`;
    throw new ApiError(detail, response.status);
  }
  return body;
}

export function listReferrals(status = "") {
  const query = new URLSearchParams({ limit: "100", offset: "0" });
  if (status) query.set("status", status);
  return request(`/referrals?${query}`);
}

export function createReferral(referral) {
  return request("/referrals", { method: "POST", body: JSON.stringify(referral) });
}

export function replaceReferral(id, referral) {
  return request(`/referrals/${id}`, { method: "PUT", body: JSON.stringify(referral) });
}

export function deleteReferral(id) {
  return request(`/referrals/${id}`, { method: "DELETE" });
}
