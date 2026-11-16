/**
 * Client-side checks mirroring the profile rules in
 * `backend/app/schemas/user.py`. The backend stays the source of truth.
 */

import { validatePassword } from "@/lib/validation/auth";

export const FULL_NAME_MAX_LENGTH = 255;

/** The value to send: trimmed, with a blank name clearing it (`null`). */
export function normalizeFullName(fullName: string): string | null {
  const value = fullName.trim();
  return value ? value : null;
}

export function validateFullName(fullName: string): string | undefined {
  // Count code points, as the backend (Python `len`) does.
  if (Array.from(fullName.trim()).length > FULL_NAME_MAX_LENGTH) {
    return `Name must be at most ${FULL_NAME_MAX_LENGTH} characters.`;
  }
  return undefined;
}

export function validateCurrentPassword(password: string): string | undefined {
  return password ? undefined : "Enter your current password.";
}

export function validateNewPassword(password: string): string | undefined {
  return password ? validatePassword(password) : "Enter a new password.";
}

export function validatePasswordConfirmation(
  confirmation: string,
  password: string,
): string | undefined {
  if (!confirmation) {
    return "Confirm your new password.";
  }
  return confirmation === password ? undefined : "The passwords do not match.";
}
