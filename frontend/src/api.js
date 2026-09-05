const BASE_URL = "http://localhost:8000/api";

async function request(path) {
  const res = await fetch(`${BASE_URL}${path}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const error = new Error(body.detail || `Request to ${path} failed with ${res.status}`);
    error.status = res.status;
    throw error;
  }
  return res.json();
}

export const api = {
  health: () => request("/health"),
  metrics: () => request("/metrics"),
  decisions: () => request("/decisions"),
  decision: (paymentId) => request(`/decisions/${encodeURIComponent(paymentId)}`),
  exceptions: () => request("/exceptions"),
};
