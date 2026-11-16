import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { login, logout, register } from "@/lib/api/auth";

function fakeResponse(status: number, body?: unknown): Response {
  const text = body === undefined ? "" : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => text,
  } as unknown as Response;
}

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

describe("auth API", () => {
  it("register posts the credentials and returns the public user", async () => {
    const user = {
      id: "6f1c1a52-1d1f-4c3e-9f55-0d0b8f0d2a10",
      email: "alice@example.com",
      full_name: null,
      is_active: true,
      email_verified: false,
      created_at: "2026-01-01T00:00:00Z",
    };
    fetchMock.mockResolvedValue(fakeResponse(201, user));

    await expect(
      register({ email: "alice@example.com", password: "password123" }),
    ).resolves.toEqual(user);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.test/api/v1/auth/register");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body as string)).toEqual({
      email: "alice@example.com",
      password: "password123",
    });
  });

  it("login relies on the cookie and does not expose or store the token", async () => {
    fetchMock.mockResolvedValue(
      fakeResponse(200, { access_token: "header.payload.signature", token_type: "bearer" }),
    );

    await expect(
      login({ email: "alice@example.com", password: "password123" }),
    ).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.test/api/v1/auth/login");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("logout posts with credentials", async () => {
    fetchMock.mockResolvedValue(fakeResponse(204));

    await expect(logout()).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.test/api/v1/auth/logout");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
  });
});
