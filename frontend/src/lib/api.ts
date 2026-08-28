const API_ROOT = "/api/v1";
const CSRF_STORAGE_KEY = "pulse_csrf";

export const remoteEnabled = import.meta.env.VITE_API_MODE === "remote";

let csrfToken = typeof window === "undefined" ? "" : sessionStorage.getItem(CSRF_STORAGE_KEY) ?? "";

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly details?: unknown,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method?.toUpperCase() ?? "GET";
  const multipart = typeof FormData !== "undefined" && init?.body instanceof FormData;
  const response = await fetch(`${API_ROOT}${path}`, {
    credentials: "include",
    headers: {
      ...(!multipart ? { "Content-Type": "application/json" } : {}),
      ...(csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)
        ? { "X-CSRF-Token": csrfToken }
        : {}),
      ...init?.headers,
    },
    ...init,
  });

  if (!response.ok) {
    const details = await response.json().catch(() => undefined);
    throw new ApiError("Не удалось выполнить запрос", response.status, details);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  upload: <T>(path: string, body: FormData) =>
    request<T>(path, { method: "POST", body }),
  delete: (path: string) => request<void>(path, { method: "DELETE" }),
  setCsrf(token: string) {
    csrfToken = token;
    if (token) sessionStorage.setItem(CSRF_STORAGE_KEY, token);
    else sessionStorage.removeItem(CSRF_STORAGE_KEY);
  },
};
