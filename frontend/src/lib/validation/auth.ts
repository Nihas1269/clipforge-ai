/**
 * Client-side checks mirroring the backend rules in
 * `backend/app/schemas/user.py`. The backend stays the source of truth; these
 * only give faster feedback.
 */

export const EMAIL_MAX_LENGTH = 320;
export const PASSWORD_MIN_LENGTH = 8;
export const PASSWORD_MAX_LENGTH = 256;

// Basic shape check: something@domain.tld with no whitespace.
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
// The backend only accepts ASCII addresses (control characters never form a valid one).
const NON_PRINTABLE_ASCII_PATTERN = /[^\x20-\x7E]/;

export function normalizeEmail(email: string): string {
  return email.trim();
}

export function validateEmail(email: string): string | undefined {
  const value = normalizeEmail(email);
  if (!value) {
    return "Enter your email address.";
  }
  if (
    value.length > EMAIL_MAX_LENGTH ||
    NON_PRINTABLE_ASCII_PATTERN.test(value) ||
    !EMAIL_PATTERN.test(value)
  ) {
    return "Enter a valid email address, like name@example.com.";
  }
  return undefined;
}

export function validatePassword(password: string): string | undefined {
  // Count code points, as the backend (Python `len`) does.
  const length = Array.from(password).length;
  if (length === 0) {
    return "Enter your password.";
  }
  if (length < PASSWORD_MIN_LENGTH) {
    return `Password must be at least ${PASSWORD_MIN_LENGTH} characters.`;
  }
  if (length > PASSWORD_MAX_LENGTH) {
    return `Password must be at most ${PASSWORD_MAX_LENGTH} characters.`;
  }
  return undefined;
}
