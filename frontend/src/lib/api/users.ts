import type { PublicUser } from "@/lib/api/auth";
import { apiRequest } from "@/lib/api/client";

export interface ProfileUpdate {
  /** `null` clears the name. */
  full_name: string | null;
}

export interface PasswordChange {
  current_password: string;
  new_password: string;
}

/** The logged-in user; throws an `ApiError` with status `401` when there is no session. */
export function getCurrentUser(): Promise<PublicUser> {
  return apiRequest<PublicUser>("/api/v1/users/me");
}

export function updateProfile(update: ProfileUpdate): Promise<PublicUser> {
  return apiRequest<PublicUser>("/api/v1/users/me", {
    method: "PATCH",
    body: { full_name: update.full_name },
  });
}

/**
 * Changes the password. The backend replaces the session cookie with a fresh
 * one; the access token in the response body is discarded, as with login.
 */
export async function changePassword(change: PasswordChange): Promise<void> {
  await apiRequest<unknown>("/api/v1/users/me/password", {
    method: "POST",
    body: { current_password: change.current_password, new_password: change.new_password },
  });
}
