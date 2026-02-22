const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

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
  }

  const res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    credentials: "include",
    body: body ? JSON.stringify(body) : undefined,
  });

  if (res.status === 401 && typeof window !== "undefined") {
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
        if (Array.isArray(errorData.detail)) {
          // Handle Pydantic validation errors (list of objects)
          errorMessage = errorData.detail
            .map((err: any) => {
              const loc = err.loc ? err.loc.join(".") : "";
              return loc ? `${loc}: ${err.msg}` : err.msg;
            })
            .join("\n");
        } else if (typeof errorData.detail === "object") {
          errorMessage = JSON.stringify(errorData.detail);
        } else {
          errorMessage = String(errorData.detail);
        }
      }
    } catch (e) {
      // Use status text if JSON parsing fails
      errorMessage = res.statusText || errorMessage;
    }
    throw new Error(errorMessage);
  }

  if (res.status === 204) {
    return undefined as T;
  }

  return res.json();
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
  }

  const res = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers,
    credentials: "include",
    body: body ? JSON.stringify(body) : undefined,
  });

  if (res.status === 401 && typeof window !== "undefined") {
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

export const apiClient = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  delete: <T>(path: string) => request<T>("DELETE", path),
  stream: (path: string, body?: unknown) => streamRequest(path, body),
};
