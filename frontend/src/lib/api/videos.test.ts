import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";
import {
  UPLOAD_RESPONSE_TIMEOUT_MS,
  UPLOAD_STALL_TIMEOUT_MS,
  fileExtension,
  getUploadLimits,
  uploadVideo,
} from "@/lib/api/videos";
import { FakeXMLHttpRequest } from "@/test-utils/fake-xhr";

const video = {
  id: "0b6f4f5e-4a7d-4a50-9c2e-6f0e8a1b2c3d",
  user_id: "6f1c1a52-1d1f-4c3e-9f55-0d0b8f0d2a10",
  original_filename: "clip.mp4",
  content_type: "video/mp4",
  size_bytes: 4,
  status: "uploaded",
  error_message: null,
  duration_seconds: null,
  width: null,
  height: null,
  fps: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

async function catchError(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError);
    return error as ApiError;
  }
  throw new Error("Expected the promise to reject.");
}

function mp4(name = "clip.mp4", type = "video/mp4"): File {
  return new File(["data"], name, { type });
}

beforeEach(() => {
  FakeXMLHttpRequest.reset();
  vi.stubGlobal("XMLHttpRequest", FakeXMLHttpRequest);
  vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://api.test");
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("uploadVideo", () => {
  it("posts the file as the `file` field with credentials and resolves with the video", async () => {
    const file = mp4();
    const promise = uploadVideo(file);
    const xhr = FakeXMLHttpRequest.latest();

    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("http://api.test/api/v1/videos");
    expect(xhr.withCredentials).toBe(true);
    expect(xhr.requestHeaders.Accept).toBe("application/json");
    expect(xhr.body).toBeInstanceOf(FormData);
    const sent = (xhr.body as FormData).get("file");
    expect(sent).toBeInstanceOf(File);
    expect((sent as File).name).toBe("clip.mp4");
    expect((sent as File).type).toBe("video/mp4");
    expect([...(xhr.body as FormData).keys()]).toEqual(["file"]);

    xhr.respond(201, video);

    await expect(promise).resolves.toEqual(video);
  });

  it("declares the content type from the extension when the browser reports none", async () => {
    const promise = uploadVideo(mp4("movie.MKV", ""));
    const xhr = FakeXMLHttpRequest.latest();

    const sent = (xhr.body as FormData).get("file") as File;
    expect(sent.name).toBe("movie.MKV");
    expect(sent.type).toBe("video/x-matroska");

    xhr.respond(201, video);
    await promise;
  });

  it("reports upload progress and when the file has been sent", async () => {
    const onProgress = vi.fn();
    const onSent = vi.fn();
    const promise = uploadVideo(mp4(), { onProgress, onSent });
    const xhr = FakeXMLHttpRequest.latest();

    xhr.progress(1, 4);
    xhr.progress(2, 0, false);
    xhr.finishSending();

    expect(onProgress).toHaveBeenNthCalledWith(1, { loaded: 1, total: 4 });
    // Without a computable length, the file size is the total.
    expect(onProgress).toHaveBeenNthCalledWith(2, { loaded: 2, total: 4 });
    expect(onSent).toHaveBeenCalledTimes(1);

    xhr.respond(201, video);
    await promise;
  });

  it("aborts the request when the signal is aborted", async () => {
    const controller = new AbortController();
    const promise = uploadVideo(mp4(), { signal: controller.signal });
    const xhr = FakeXMLHttpRequest.latest();

    controller.abort();

    expect(xhr.aborted).toBe(true);
    const error = await catchError(promise);
    expect(error.code).toBe("upload_cancelled");
    expect(error.status).toBe(0);
  });

  it("does not send anything when the signal is already aborted", async () => {
    const controller = new AbortController();
    controller.abort();

    const error = await catchError(uploadVideo(mp4(), { signal: controller.signal }));

    expect(error.code).toBe("upload_cancelled");
    expect(FakeXMLHttpRequest.instances).toHaveLength(0);
  });

  it("rejects with the backend error", async () => {
    const promise = uploadVideo(mp4());

    FakeXMLHttpRequest.latest().respond(415, {
      error: {
        code: "file_type_mismatch",
        message: "The file contents do not match a supported video format for its extension.",
        details: null,
      },
    });

    const error = await catchError(promise);
    expect(error.status).toBe(415);
    expect(error.code).toBe("file_type_mismatch");
    expect(error.message).toBe(
      "The file contents do not match a supported video format for its extension.",
    );
  });

  it("explains a 413 without the backend error shape", async () => {
    const promise = uploadVideo(mp4());

    FakeXMLHttpRequest.latest().respond(413, "<html>Request Entity Too Large</html>");

    const error = await catchError(promise);
    expect(error.code).toBe("file_too_large");
    expect(error.message).toBe("The file is too large to upload.");
  });

  it("never reports a malformed success response as success", async () => {
    const promise = uploadVideo(mp4());

    FakeXMLHttpRequest.latest().respond(201, "not json");

    const error = await catchError(promise);
    expect(error.code).toBe("invalid_response");
  });

  it("rejects with a network error", async () => {
    const promise = uploadVideo(mp4());

    FakeXMLHttpRequest.latest().failNetwork();

    const error = await catchError(promise);
    expect(error.code).toBe("network_error");
    expect(error.status).toBe(0);
  });

  it("aborts a stalled upload with a timeout error", async () => {
    vi.useFakeTimers();
    const promise = uploadVideo(mp4());
    const xhr = FakeXMLHttpRequest.latest();

    vi.advanceTimersByTime(UPLOAD_STALL_TIMEOUT_MS - 1);
    xhr.progress(1, 4);
    vi.advanceTimersByTime(UPLOAD_STALL_TIMEOUT_MS - 1);
    expect(xhr.aborted).toBe(false);
    vi.advanceTimersByTime(1);

    expect(xhr.aborted).toBe(true);
    const error = await catchError(promise);
    expect(error.code).toBe("timeout");
  });

  it("gives the backend longer to respond once the file is sent", async () => {
    vi.useFakeTimers();
    const promise = uploadVideo(mp4());
    const xhr = FakeXMLHttpRequest.latest();

    xhr.finishSending();
    vi.advanceTimersByTime(UPLOAD_RESPONSE_TIMEOUT_MS - 1);
    expect(xhr.aborted).toBe(false);
    vi.advanceTimersByTime(1);

    const error = await catchError(promise);
    expect(error.code).toBe("timeout");
  });
});

describe("getUploadLimits", () => {
  it("returns the backend limits", async () => {
    const limits = {
      max_upload_size_bytes: 1024,
      allowed_extensions: [".mp4"],
      allowed_content_types: ["video/mp4"],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify(limits),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getUploadLimits()).resolves.toEqual(limits);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/api/v1/videos/upload-limits",
      expect.objectContaining({ method: "GET", credentials: "include" }),
    );
  });

  it("rejects a malformed response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        text: async () => JSON.stringify({ max_upload_size_bytes: "big" }),
      }),
    );

    const error = await catchError(getUploadLimits());
    expect(error.code).toBe("invalid_response");
  });
});

describe("fileExtension", () => {
  it.each([
    ["clip.mp4", ".mp4"],
    ["CLIP.MOV", ".mov"],
    ["archive.tar.webm", ".webm"],
    ["noext", ""],
    ["trailing.", ""],
  ])("%s -> %s", (name, ext) => {
    expect(fileExtension(name)).toBe(ext);
  });
});
