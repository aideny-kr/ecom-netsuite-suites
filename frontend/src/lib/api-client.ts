const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

/**
 * Thrown by `request()` on any non-OK response. Carries the real HTTP
 * `status` code alongside `.message` (which `request()` overwrites with the
 * backend's own `detail` text when present — see below) so a caller can
 * tell "this resource genuinely doesn't exist" (404) apart from every other
 * failure by checking `.status`, without pattern-matching text the backend
 * controls and which routinely contains no digits at all (e.g. `{"detail":
 * "Flow not found"}` never contains the literal string "404").
 */
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// ── Silent token refresh on 401 ────────────────────────────────
let isRefreshing = false;
let refreshPromise: Promise<boolean> | null = null;

async function tryRefreshToken(): Promise<boolean> {
  if (isRefreshing && refreshPromise) return refreshPromise;
  isRefreshing = true;
  refreshPromise = (async () => {
    try {
      const res = await fetch(`${BASE_URL}/api/v1/auth/refresh`, {
        method: "POST",
        credentials: "include", // sends HttpOnly refresh_token cookie
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) return false;
      const data = await res.json();
      localStorage.setItem("access_token", data.access_token);
      document.cookie = `access_token=${data.access_token}; path=/; max-age=${60 * 60 * 24 * 7}; samesite=lax`;
      return true;
    } catch {
      return false;
    } finally {
      isRefreshing = false;
      refreshPromise = null;
    }
  })();
  return refreshPromise;
}

/** The backend's own `detail` text for a non-OK response, falling back to
 * `fallback` when the body isn't JSON or carries no detail. Pydantic
 * validation errors arrive as a LIST of `{loc, msg}` objects and render as
 * one `loc: msg` line each. Extracted so the two places that build an error
 * from a response — the ordinary non-OK path and `retryFailure` below —
 * cannot drift into reporting the same failure two different ways. */
async function readErrorMessage(res: Response, fallback: string): Promise<string> {
  try {
    const errorData = await res.json();
    if (!errorData?.detail) return fallback;
    if (Array.isArray(errorData.detail)) {
      return errorData.detail
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        .map((err: any) => {
          const loc = err.loc ? err.loc.join(".") : "";
          return loc ? `${loc}: ${err.msg}` : err.msg;
        })
        .join("\n");
    }
    if (typeof errorData.detail === "object") return JSON.stringify(errorData.detail);
    return String(errorData.detail);
  } catch {
    return res.statusText || fallback;
  }
}

/** A non-OK RETRY after a token refresh that SUCCEEDED. Only a 401 here means
 * the session is genuinely dead and the logout path is the right answer;
 * every other status is an ordinary request failure and must surface as one.
 *
 * It used to surface as a logout: the retry's `if (retry.ok)` simply fell
 * through, so a 404 (a flow id that genuinely isn't in the last sync) wiped
 * the access token and bounced the reader to `/login` — losing their session
 * over a missing record, and hiding the real status from the page that knew
 * how to render it (`celigo-flow-page.tsx`'s `is404`).
 *
 * Returns the error to throw, or `null` when the caller SHOULD fall through
 * to logging out. Shared by all four request shapes so the rule is one
 * decision rather than four copies that can drift. */
async function retryFailure(res: Response, fallback: string): Promise<ApiError | null> {
  if (res.status === 401) return null;
  return new ApiError(await readErrorMessage(res, fallback), res.status);
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };

  if (typeof window !== "undefined") {
    const token = localStorage.getItem("access_token");
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }
    // Send browser timezone for date-aware queries (income statement periods,
    // "last N months", etc.). Previously only sent on streamRequest(); after
    // PR #23 moved chat to background tasks, non-streaming POSTs silently
    // dropped the header and the backend had no current-date reference.
    try {
      headers["X-Timezone"] = Intl.DateTimeFormat().resolvedOptions().timeZone;
    } catch {
      // Fallback silently if Intl API unavailable
    }
  }

  const res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    credentials: "include",
    body: body ? JSON.stringify(body) : undefined,
  });

  // On 401, attempt silent token refresh before redirecting to login
  if (res.status === 401 && typeof window !== "undefined") {
    const refreshed = await tryRefreshToken();
    if (refreshed) {
      // Retry the original request with the new token
      headers["Authorization"] = `Bearer ${localStorage.getItem("access_token")}`;
      const retry = await fetch(`${BASE_URL}${path}`, {
        method,
        headers,
        credentials: "include",
        body: body ? JSON.stringify(body) : undefined,
      });
      if (retry.ok) {
        if (retry.status === 204) return undefined as T;
        return retry.json();
      }
      const failure = await retryFailure(retry, `Request failed: ${retry.status}`);
      if (failure) throw failure;
    }
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    window.location.href = "/login";
    throw new Error("Unauthorized");
  }

  if (!res.ok) {
    throw new ApiError(await readErrorMessage(res, `Request failed: ${res.status}`), res.status);
  }

  if (res.status === 204) {
    return undefined as T;
  }

  return res.json();
}

/**
 * Make a GET request that returns the response body as raw text (not JSON).
 * Same bearer/refresh/401 logic as `request`. Used for endpoints that return
 * non-JSON content (e.g. the report `/view` endpoint returns `text/html`).
 */
async function requestText(path: string): Promise<string> {
  const headers: Record<string, string> = {};

  if (typeof window !== "undefined") {
    const token = localStorage.getItem("access_token");
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }
  }

  const res = await fetch(`${BASE_URL}${path}`, {
    method: "GET",
    headers,
    credentials: "include",
  });

  // On 401, attempt silent token refresh before redirecting to login
  if (res.status === 401 && typeof window !== "undefined") {
    const refreshed = await tryRefreshToken();
    if (refreshed) {
      headers["Authorization"] = `Bearer ${localStorage.getItem("access_token")}`;
      const retry = await fetch(`${BASE_URL}${path}`, {
        method: "GET",
        headers,
        credentials: "include",
      });
      if (retry.ok) return retry.text();
      // Same rule as `request()`: only a 401 on the retry is a dead session.
      const failure = await retryFailure(retry, `Request failed: ${retry.status}`);
      if (failure) throw failure;
    }
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    window.location.href = "/login";
    throw new Error("Unauthorized");
  }

  if (!res.ok) {
    throw new Error(`Request failed: ${res.status}`);
  }

  return res.text();
}

/**
 * Make a POST request that returns a raw Response for SSE streaming.
 * Uses the same auth/base URL logic as the standard request function.
 */
async function streamRequest(path: string, body?: unknown): Promise<Response> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };

  if (typeof window !== "undefined") {
    const token = localStorage.getItem("access_token");
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }
    // Send browser timezone for date-aware queries
    try {
      headers["X-Timezone"] = Intl.DateTimeFormat().resolvedOptions().timeZone;
    } catch {
      // Fallback silently if Intl API unavailable
    }
  }

  const res = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers,
    credentials: "include",
    body: body ? JSON.stringify(body) : undefined,
  });

  // On 401, attempt silent token refresh before redirecting to login
  if (res.status === 401 && typeof window !== "undefined") {
    const refreshed = await tryRefreshToken();
    if (refreshed) {
      // Retry the stream request with the new token
      headers["Authorization"] = `Bearer ${localStorage.getItem("access_token")}`;
      const retry = await fetch(`${BASE_URL}${path}`, {
        method: "POST",
        headers,
        credentials: "include",
        body: body ? JSON.stringify(body) : undefined,
      });
      if (retry.ok) return retry;
      // Same rule as `request()`: only a 401 on the retry is a dead session.
      const failure = await retryFailure(retry, `Request failed: ${retry.status}`);
      if (failure) throw failure;
    }
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    window.location.href = "/login";
    throw new Error("Unauthorized");
  }

  if (!res.ok) {
    let errorMessage = `Request failed: ${res.status}`;
    try {
      const errorData = await res.json();
      if (errorData.detail) {
        errorMessage =
          typeof errorData.detail === "string"
            ? errorData.detail
            : JSON.stringify(errorData.detail);
      }
    } catch {
      errorMessage = res.statusText || errorMessage;
    }
    throw new Error(errorMessage);
  }

  return res;
}

/**
 * Make a POST request that returns a raw Response for binary downloads (Excel, etc.).
 * Uses the same auth/base URL/401-retry logic as streamRequest.
 */
async function downloadRequest(path: string, body?: unknown): Promise<Response> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };

  if (typeof window !== "undefined") {
    const token = localStorage.getItem("access_token");
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }
  }

  const res = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers,
    credentials: "include",
    body: body ? JSON.stringify(body) : undefined,
  });

  if (res.status === 401 && typeof window !== "undefined") {
    const refreshed = await tryRefreshToken();
    if (refreshed) {
      headers["Authorization"] = `Bearer ${localStorage.getItem("access_token")}`;
      const retry = await fetch(`${BASE_URL}${path}`, {
        method: "POST",
        headers,
        credentials: "include",
        body: body ? JSON.stringify(body) : undefined,
      });
      if (retry.ok) return retry;
      // Same rule as `request()`: only a 401 on the retry is a dead session.
      const failure = await retryFailure(retry, `Export failed: ${retry.status}`);
      if (failure) throw failure;
    }
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    window.location.href = "/login";
    throw new Error("Unauthorized");
  }

  if (!res.ok) {
    let errorMessage = `Export failed: ${res.status}`;
    try {
      const errorData = await res.json();
      if (errorData.detail) {
        errorMessage =
          typeof errorData.detail === "string"
            ? errorData.detail
            : JSON.stringify(errorData.detail);
      }
    } catch {
      errorMessage = res.statusText || errorMessage;
    }
    throw new Error(errorMessage);
  }

  return res;
}

export const apiClient = {
  get: <T>(path: string) => request<T>("GET", path),
  getText: (path: string) => requestText(path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  delete: <T>(path: string) => request<T>("DELETE", path),
  stream: (path: string, body?: unknown) => streamRequest(path, body),
  streamGet: async (path: string, signal?: AbortSignal): Promise<Response> => {
    const headers: Record<string, string> = {};
    if (typeof window !== "undefined") {
      const token = localStorage.getItem("access_token");
      if (token) {
        headers["Authorization"] = `Bearer ${token}`;
      }
    }
    const res = await fetch(`${BASE_URL}${path}`, {
      method: "GET",
      headers,
      credentials: "include",
      signal,
    });
    if (res.status === 401 && typeof window !== "undefined") {
      const refreshed = await tryRefreshToken();
      if (refreshed) {
        headers["Authorization"] = `Bearer ${localStorage.getItem("access_token")}`;
        const retry = await fetch(`${BASE_URL}${path}`, {
          method: "GET",
          headers,
          credentials: "include",
          signal,
        });
        if (retry.ok) return retry;
      }
      localStorage.removeItem("access_token");
      localStorage.removeItem("refresh_token");
      window.location.href = "/login";
      throw new Error("Unauthorized");
    }
    if (!res.ok) {
      throw new Error(`Stream request failed: ${res.status}`);
    }
    return res;
  },
  download: (path: string, body?: unknown) => downloadRequest(path, body),
};
