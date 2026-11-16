import { apiRequest } from "@/lib/api/client";

export interface Credentials {
  email: string;
  password: string;
}

/** The public user returned by the backend; never includes the password hash. */
export interface PublicUser {
  id: string;
  email: string;
  full_name: string | null;
  is_active: boolean;
  email_verified: boolean;
  created_at: string;
}

export function register(credentials: Credentials): Promise<PublicUser> {
  return apiRequest<PublicUser>("/api/v1/auth/register", {
    method: "POST",
    body: { email: credentials.email, password: credentials.password },
  });
}

/**
 * Logs in. The backend sets the HttpOnly session cookie; the access token in
 * the response body is deliberately discarded so it is never kept in memory,
 * Web Storage, or logs.
 */
export async function login(credentials: Credentials): Promise<void> {
  await apiRequest<unknown>("/api/v1/auth/login", {
    method: "POST",
    body: { email: credentials.email, password: credentials.password },
  });
}

/** Clears the session cookie. */
export async function logout(): Promise<void> {
  await apiRequest<unknown>("/api/v1/auth/logout", { method: "POST" });
}
