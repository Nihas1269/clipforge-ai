import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createClip, detectClips, listClips, updateClipStatus, type Clip } from "@/lib/api/clips";

const API = "http://api.test";
const fetchMock = vi.fn();

const clip: Clip = {
  id: "c1",
  video_id: "v1",
  start_seconds: 5,
  end_seconds: 35,
  title: "Hook",
  reason: "Questions.",
  score: 0.5,
  status: "suggested",
  source: "heuristic",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
  };
}

function lastCall() {
  const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit | undefined];
  return {
    url,
    method: init?.method ?? "GET",
    body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
  };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", API);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("clips API", () => {
  it("lists a video's clips", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [clip], total: 1 }));

    await expect(listClips("v1")).resolves.toEqual({ items: [clip], total: 1 });
    expect(lastCall()).toEqual({ url: `${API}/api/v1/videos/v1/clips`, method: "GET", body: undefined });
  });

  it("rejects a malformed clip list", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [{ ...clip, start_seconds: "5" }], total: 1 }));

    await expect(listClips("v1")).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("queues clip detection", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ job_id: "j1", status: "queued" }, 202));

    await expect(detectClips("v1")).resolves.toEqual({ job_id: "j1", status: "queued" });
    expect(lastCall()).toEqual({ url: `${API}/api/v1/videos/v1/clips/detect`, method: "POST", body: undefined });
  });

  it("creates a manual clip, leaving out an empty title", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ...clip, status: "accepted", source: "manual" }, 201));

    await createClip("v1", { start_seconds: 1, end_seconds: 20, title: "" });

    expect(lastCall()).toEqual({
      url: `${API}/api/v1/videos/v1/clips`,
      method: "POST",
      body: { start_seconds: 1, end_seconds: 20 },
    });
  });

  it("changes a clip's status with PATCH", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ...clip, status: "rejected" }));

    await expect(updateClipStatus("c1", "rejected")).resolves.toMatchObject({ status: "rejected" });
    expect(lastCall()).toEqual({ url: `${API}/api/v1/clips/c1`, method: "PATCH", body: { status: "rejected" } });
  });
});
