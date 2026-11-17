import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";
import { getJob } from "@/lib/api/jobs";
import { getTranscript, startTranscription, subtitleUrl } from "@/lib/api/transcripts";

const API = "http://api.test";
const fetchMock = vi.fn();

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
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

describe("getTranscript", () => {
  it("returns the transcript with its segments", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        id: "t1",
        video_id: "v1",
        language: "en",
        requested_language: null,
        provider: "fake",
        model: null,
        full_text: "Hi",
        timing_granularity: "word",
        created_at: "2026-01-01T00:00:00Z",
        segments: [{ id: "s1", index: 0, start_seconds: 1.5, end_seconds: 3, text: "Hi", words: [] }],
      }),
    );

    const transcript = await getTranscript("v 1");

    expect(fetchMock.mock.calls[0][0]).toBe(`${API}/api/v1/videos/v%201/transcript`);
    expect(transcript?.segments).toEqual([{ id: "s1", index: 0, start_seconds: 1.5, end_seconds: 3, text: "Hi" }]);
    expect(transcript?.timing_granularity).toBe("word");
  });

  it("keeps segments with missing timestamps without their times", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ id: "t1", segments: [{ index: 0, start_seconds: null, end_seconds: "x", text: "Hi" }] }),
    );

    const transcript = await getTranscript("v1");

    expect(transcript?.segments[0]).toMatchObject({ start_seconds: null, end_seconds: null, text: "Hi" });
  });

  it("returns null when the video has no transcript", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ error: { code: "transcript_not_found", message: "No transcript.", details: null } }, 404),
    );

    await expect(getTranscript("v1")).resolves.toBeNull();
  });

  it("rethrows other errors, including a missing video", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ error: { code: "not_found", message: "Not found.", details: null } }, 404));

    await expect(getTranscript("v1")).rejects.toMatchObject({ status: 404, code: "not_found" });
  });

  it("rejects a malformed transcript", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: "t1", segments: [{ index: 0 }] }));

    await expect(getTranscript("v1")).rejects.toBeInstanceOf(ApiError);
  });
});

describe("startTranscription", () => {
  it("posts the language and returns the queued job", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ job_id: "j1", status: "queued" }, 202));

    await expect(startTranscription("v1", "te")).resolves.toEqual({ job_id: "j1", status: "queued" });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${API}/api/v1/videos/v1/transcription`);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ language: "te" });
  });

  it("rejects a response without a job id", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ status: "queued" }, 202));

    await expect(startTranscription("v1")).rejects.toMatchObject({ code: "invalid_response" });
  });
});

describe("subtitleUrl", () => {
  it("points at the SRT and VTT downloads", () => {
    expect(subtitleUrl("v1", "srt")).toBe(`${API}/api/v1/videos/v1/transcript.srt`);
    expect(subtitleUrl("v1", "vtt")).toBe(`${API}/api/v1/videos/v1/transcript.vtt`);
  });
});

describe("getJob", () => {
  it("returns the job status and error", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ id: "j1", job_type: "transcription", status: "failed", error_message: "Boom." }),
    );

    await expect(getJob("j1")).resolves.toMatchObject({ id: "j1", status: "failed", error_message: "Boom." });
    expect(fetchMock.mock.calls[0][0]).toBe(`${API}/api/v1/jobs/j1`);
  });

  it("rejects an unknown status", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: "j1", status: "done" }));

    await expect(getJob("j1")).rejects.toMatchObject({ code: "invalid_response" });
  });
});
