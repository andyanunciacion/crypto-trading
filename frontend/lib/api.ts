/**
 * Calls to the API, which the browser reaches at /api on the dashboard's own origin
 * (next.config.ts rewrites it to the API service). Same origin means the httpOnly
 * session cookie goes along by itself, and no CORS is needed (D44).
 */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail =
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      typeof body.detail === "string"
        ? body.detail
        : `HTTP ${response.status}`;
    throw new ApiError(response.status, detail);
  }
  return response;
}

export async function signIn(username: string, password: string, code: string): Promise<void> {
  await call("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password, code }),
  });
}

export async function signOut(): Promise<void> {
  await call("/auth/logout", { method: "POST" });
}

export type Me = { username: string };

export async function me(): Promise<Me> {
  return (await (await call("/auth/me")).json()) as Me;
}
