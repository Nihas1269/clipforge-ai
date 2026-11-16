import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  DEFAULT_API_BASE_URL,
  apiRequest,
  getApiBaseUrl,
  getErrorMessage,
} from "@/lib/api/client";

function fakeResponse(status: number, body?: unknown): Response {
  const text = body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => text,
  } as unknown as Response;
}

async function catchError(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error("Expected the promise to reject.");
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://api.test");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("getApiBaseUrl", () => {
  it("uses NEXT_PUBLIC_API_BASE_URL without a trailing slash", () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://api.test/");

    expect(getApiBaseUrl()).toBe("http://api.test");
  });

  it("falls back to the local default when unset", () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "");

    expect(getApiBaseUrl()).toBe(DEFAULT_API_BASE_URL);
  });
});

describe("apiRequest", () => {
  it("parses a successful JSON response and includes credentials", async () => {
    fetchMock.mockResolvedValue(fakeResponse(200, { status: "ok" }));

    await expect(apiRequest("/api/v1/ping")).resolves.toEqual({ status: "ok" });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.test/api/v1/ping");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("include");
    expect(init.body).toBeUndefined();
    expect(init.headers).toEqual({ Accept: "application/json" });
  });

  it("sends a JSON body", async () => {
    fetchMock.mockResolvedValue(fakeResponse(201, { id: "1" }));

    await apiRequest("/api/v1/things", { method: "POST", body: { name: "clip" } });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(init.body).toBe(JSON.stringify({ name: "clip" }));
    expect(init.headers).toEqual({
      Accept: "application/json",
      "Content-Type": "application/json",
    });
  });

  it("returns undefined for an empty body such as 204", async () => {
    fetchMock.mockResolvedValue(fakeResponse(204));

    await expect(apiRequest("/api/v1/auth/logout", { method: "POST" })).resolves.toBeUndefined();
  });

  it("parses the backend error shape into an ApiError", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse(409, {
        error: {
          code: "email_taken",
          message: "An account with this email already exists.",
          details: { field: "email" },
        },
      }),
    );

    const error = await catchError(apiRequest("/api/v1/auth/register", { method: "POST" }));

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 409,
      code: "email_taken",
      message: "An account with this email already exists.",
      details: { field: "email" },
    });
  });

  it("parses a 401 error without redirecting", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse(401, {
        error: { code: "invalid_credentials", message: "Incorrect email or password.", details: null },
      }),
    );

    const error = await catchError(apiRequest("/api/v1/auth/login", { method: "POST" }));

    expect(error).toMatchObject({ status: 401, code: "invalid_credentials", details: null });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("uses a generic ApiError for a non-standard error body", async () => {
    fetchMock.mockResolvedValue(fakeResponse(502, "<html>Bad gateway</html>"));

    const error = await catchError(apiRequest("/api/v1/ping"));

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 502, code: "unknown_error" });
  });

  it("uses a generic ApiError for a JSON error without the error shape", async () => {
    fetchMock.mockResolvedValue(fakeResponse(500, { detail: "boom" }));

    const error = await catchError(apiRequest("/api/v1/ping"));

    expect(error).toMatchObject({ status: 500, code: "unknown_error" });
    expect((error as ApiError).message).not.toContain("boom");
  });

  it("does not report an invalid JSON success body as success", async () => {
    fetchMock.mockResolvedValue(fakeResponse(200, "not json"));

    const error = await catchError(apiRequest("/api/v1/ping"));

    expect(error).toMatchObject({ status: 200, code: "invalid_response" });
  });

  it("wraps network failures in an ApiError", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    const error = await catchError(apiRequest("/api/v1/ping"));

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 0, code: "network_error" });
  });

  it("aborts requests that exceed the timeout", async () => {
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    );

    const error = await catchError(apiRequest("/api/v1/ping", { timeoutMs: 5 }));

    expect(error).toMatchObject({ status: 0, code: "timeout" });
  });
});

describe("getErrorMessage", () => {
  it("returns the ApiError message", () => {
    expect(getErrorMessage(new ApiError(401, "invalid_credentials", "Incorrect email or password."))).toBe(
      "Incorrect email or password.",
    );
  });

  it("returns a generic message for other errors", () => {
    expect(getErrorMessage(new Error("internal detail"))).toBe(
      "Something went wrong. Please try again.",
    );
  });
});
