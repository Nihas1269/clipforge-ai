/**
 * Typed `fetch` wrapper for the ClipForge backend.
 *
 * Authentication relies on the HttpOnly `access_token` cookie set by the
 * backend, so every request is sent with `credentials: "include"`. Tokens are
 * never read, stored, or logged by the frontend. The client never redirects:
 * a `401` is surfaced as an `ApiError` and the caller decides what to do,
 * which keeps pages such as `/login` free of redirect loops.
 */

/** Used when `NEXT_PUBLIC_API_BASE_URL` is not set (local development). */
export const DEFAULT_API_BASE_URL = "http://localhost:8000";

/** Requests (including reading the response body) are aborted after this. */
export const REQUEST_TIMEOUT_MS = 15_000;

export const GENERIC_ERROR_MESSAGE = "Something went wrong. Please try again.";

export type ApiErrorDetails = unknown;

/** The backend error shape: `{"error": {"code", "message", "details"}}`. */
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details: ApiErrorDetails;
  };
}

export class ApiError extends Error {
  /** HTTP status, or `0` when no response was received. */
  readonly status: number;
  /** Machine-readable code from the backend, or a client-side code such as `network_error`. */
  readonly code: string;
  readonly details: ApiErrorDetails;

  constructor(status: number, code: string, message: string, details: ApiErrorDetails = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface ApiRequestOptions {
  method?: HttpMethod;
  /** Serialized as JSON when provided. */
  body?: unknown;
  timeoutMs?: number;
}

export function getApiBaseUrl(): string {
  // Referenced literally so Next.js can inline the value into the bundle.
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  return (configured || DEFAULT_API_BASE_URL).replace(/\/+$/, "");
}

export function isApiErrorBody(value: unknown): value is ApiErrorBody {
  if (typeof value !== "object" || value === null || !("error" in value)) {
    return false;
  }
  const error = (value as { error: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    typeof (error as { code?: unknown }).code === "string" &&
    typeof (error as { message?: unknown }).message === "string"
  );
}

const INVALID_JSON = Symbol("invalid-json");

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    return undefined;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return INVALID_JSON;
  }
}

/** Builds the `ApiError` for a non-2xx response from its parsed body (or `undefined`). */
export function toApiError(status: number, payload: unknown): ApiError {
  if (isApiErrorBody(payload)) {
    const { code, message, details } = payload.error;
    return new ApiError(status, code, message, details ?? null);
  }
  // Non-standard bodies (e.g. a plain-text CORS rejection or a proxy error page).
  return new ApiError(status, "unknown_error", GENERIC_ERROR_MESSAGE);
}

/**
 * Sends a request to `path` (e.g. `/api/v1/auth/login`) and returns the
 * parsed JSON body, or `undefined` for an empty body such as a `204`.
 * Throws `ApiError` for non-2xx responses, timeouts, and network failures.
 */
export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const { method = "GET", body, timeoutMs = REQUEST_TIMEOUT_MS } = options;

  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${getApiBaseUrl()}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "include",
      signal: controller.signal,
    });
    const payload = await readJson(response);

    if (!response.ok) {
      throw toApiError(response.status, payload === INVALID_JSON ? undefined : payload);
    }
    if (payload === INVALID_JSON) {
      throw new ApiError(response.status, "invalid_response", GENERIC_ERROR_MESSAGE);
    }
    return payload as T;
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    if (controller.signal.aborted) {
      throw new ApiError(0, "timeout", "The server took too long to respond. Please try again.");
    }
    throw new ApiError(
      0,
      "network_error",
      "Could not reach the server. Check your connection and try again.",
    );
  } finally {
    clearTimeout(timer);
  }
}

/** A user-facing message for any error thrown while calling the API. */
export function getErrorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : GENERIC_ERROR_MESSAGE;
}
