import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";
import { changePassword, getCurrentUser, updateProfile } from "@/lib/api/users";

function fakeResponse(status: number, body?: unknown): Response {
  const text = body === undefined ? "" : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => text,
  } as unknown as Response;
}

const user = {
  id: "6f1c1a52-1d1f-4c3e-9f55-0d0b8f0d2a10",
  email: "alice@example.com",
  full_name: "Alice",
  is_active: true,
  email_verified: false,
  created_at: "2026-01-01T00:00:00Z",
};

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://api.test");
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("users API", () => {
  it("getCurrentUser fetches /users/me with credentials", async () => {
    fetchMock.mockResolvedValue(fakeResponse(200, user));

    await expect(getCurrentUser()).resolves.toEqual(user);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.test/api/v1/users/me");
    expect(init.method).toBe("GET");
    expect(init.credentials).toBe("include");
  });

  it("getCurrentUser throws a 401 ApiError without a session", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse(401, {
        error: { code: "not_authenticated", message: "Not authenticated", details: null },
      }),
    );

    const error = await getCurrentUser().catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
  });

  it("updateProfile patches only full_name", async () => {
    fetchMock.mockResolvedValue(fakeResponse(200, { ...user, full_name: "Alice B" }));

    await expect(updateProfile({ full_name: "Alice B" })).resolves.toMatchObject({
      full_name: "Alice B",
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.test/api/v1/users/me");
    expect(init.method).toBe("PATCH");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body as string)).toEqual({ full_name: "Alice B" });
  });

  it("changePassword posts both passwords and does not store the new token", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse(200, { access_token: "header.payload.signature", token_type: "bearer" }),
    );

    await expect(
      changePassword({ current_password: "old-password", new_password: "new-password" }),
    ).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.test/api/v1/users/me/password");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body as string)).toEqual({
      current_password: "old-password",
      new_password: "new-password",
    });
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });
});
