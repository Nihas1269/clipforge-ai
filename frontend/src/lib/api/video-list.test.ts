import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";
import { getDashboardSummary } from "@/lib/api/dashboard";
import { createProject, listProjects, updateProject } from "@/lib/api/projects";
import { deleteVideo, listVideos, moveVideoToProject } from "@/lib/api/videos";

const API = "http://api.test";
const fetchMock = vi.fn();

const VIDEO = {
  id: "v1",
  user_id: "u1",
  original_filename: "talk.mp4",
  content_type: "video/mp4",
  size_bytes: 10,
  status: "processing",
  error_message: null,
  duration_seconds: null,
  width: null,
  height: null,
  fps: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  project: null,
};

const PROJECT = {
  id: "p1",
  name: "Podcast",
  archived: false,
  archived_at: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  video_count: 1,
  status_counts: { uploaded: 0, processing: 1, ready: 0, failed: 0 },
};

function respond(body: unknown, status = 200) {
  fetchMock.mockResolvedValueOnce({
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
  });
}

function lastCall(): { url: string; init: RequestInit } {
  const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit];
  return { url, init };
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

describe("listVideos", () => {
  it("requests the first page by default with the session cookie", async () => {
    respond({ items: [VIDEO], total: 1 });

    const list = await listVideos();

    expect(list).toEqual({ items: [VIDEO], total: 1 });
    expect(lastCall().url).toBe(`${API}/api/v1/videos?limit=20&offset=0`);
    expect(lastCall().init).toMatchObject({ method: "GET", credentials: "include" });
  });

  it("sends pagination, status and project filters", async () => {
    respond({ items: [], total: 0 });
    await listVideos({ limit: 5, offset: 10, status: "failed", projectId: "p/1" });
    expect(lastCall().url).toBe(`${API}/api/v1/videos?limit=5&offset=10&status=failed&project_id=p%2F1`);

    respond({ items: [], total: 0 });
    await listVideos({ unassigned: true });
    expect(lastCall().url).toBe(`${API}/api/v1/videos?limit=20&offset=0&unassigned=true`);
  });

  it.each([
    [{ items: "nope", total: 1 }],
    [{ items: [{ ...VIDEO, status: "archived" }], total: 1 }],
    [{ items: [{ ...VIDEO, size_bytes: "10" }], total: 1 }],
    [{ items: [VIDEO] }],
  ])("rejects a malformed response %#", async (body) => {
    respond(body);

    await expect(listVideos()).rejects.toMatchObject({ code: "invalid_response" });
  });
});

describe("deleteVideo", () => {
  it("sends DELETE for the encoded id and resolves on 204", async () => {
    respond(undefined, 204);

    await expect(deleteVideo("a/b")).resolves.toBeUndefined();

    expect(lastCall().url).toBe(`${API}/api/v1/videos/a%2Fb`);
    expect(lastCall().init.method).toBe("DELETE");
  });

  it("rejects with the backend error when deletion fails", async () => {
    respond({ error: { code: "delete_failed", message: "The video could not be deleted.", details: null } }, 500);

    const error = await deleteVideo("v1").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 500, code: "delete_failed" });
  });
});

describe("moveVideoToProject", () => {
  it("sends the project id, or null to unassign", async () => {
    respond({ ...VIDEO, project: { id: "p1", name: "Podcast", archived: false } });
    const moved = await moveVideoToProject("v1", "p1");
    expect(moved.project?.name).toBe("Podcast");
    expect(lastCall().init).toMatchObject({ method: "PATCH", body: JSON.stringify({ project_id: "p1" }) });

    respond(VIDEO);
    await moveVideoToProject("v1", null);
    expect(lastCall().init.body).toBe(JSON.stringify({ project_id: null }));
  });
});

describe("projects", () => {
  it("lists active projects by default and archived ones on request", async () => {
    respond({ items: [PROJECT], total: 1 });
    expect(await listProjects()).toEqual({ items: [PROJECT], total: 1 });
    expect(lastCall().url).toBe(`${API}/api/v1/projects?limit=10&offset=0`);

    respond({ items: [], total: 0 });
    await listProjects({ archived: true, limit: 50, offset: 50 });
    expect(lastCall().url).toBe(`${API}/api/v1/projects?limit=50&offset=50&archived=true`);
  });

  it("creates, renames and archives projects", async () => {
    respond(PROJECT, 201);
    await createProject("Podcast");
    expect(lastCall().init).toMatchObject({ method: "POST", body: JSON.stringify({ name: "Podcast" }) });

    respond({ ...PROJECT, name: "Shows" });
    await updateProject("p1", { name: "Shows" });
    expect(lastCall().url).toBe(`${API}/api/v1/projects/p1`);
    expect(lastCall().init).toMatchObject({ method: "PATCH", body: JSON.stringify({ name: "Shows" }) });

    respond({ ...PROJECT, archived: true });
    await updateProject("p1", { archived: true });
    expect(lastCall().init.body).toBe(JSON.stringify({ archived: true }));
  });

  it("rejects a malformed project", async () => {
    respond({ id: "p1", name: "Podcast" }, 201);

    await expect(createProject("Podcast")).rejects.toMatchObject({ code: "invalid_response" });
  });
});

describe("getDashboardSummary", () => {
  it("returns a valid summary and rejects a malformed one", async () => {
    const summary = {
      videos: { total: 1, status_counts: PROJECT.status_counts },
      clips: 2,
      projects: { active: 1, archived: 0 },
      usage: { storage_bytes: 10, video_seconds: 0 },
      recent_projects: [PROJECT],
      recent_videos: [VIDEO],
    };
    respond(summary);
    expect(await getDashboardSummary()).toEqual(summary);
    expect(lastCall().url).toBe(`${API}/api/v1/dashboard`);

    respond({ ...summary, clips: "2" });
    await expect(getDashboardSummary()).rejects.toMatchObject({ code: "invalid_response" });

    respond({ ...summary, videos: { total: 1, status_counts: { ...PROJECT.status_counts, failed: "1" } } });
    await expect(getDashboardSummary()).rejects.toMatchObject({ code: "invalid_response" });

    respond({ ...summary, recent_videos: [{ ...VIDEO, status: "archived" }] });
    await expect(getDashboardSummary()).rejects.toMatchObject({ code: "invalid_response" });
  });
});
