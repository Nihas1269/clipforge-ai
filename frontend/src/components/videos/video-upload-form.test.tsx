import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { VideoUploadForm } from "@/components/videos/video-upload-form";
import { FakeXMLHttpRequest } from "@/test-utils/fake-xhr";

const mocks = vi.hoisted(() => {
  const push = vi.fn();
  const replace = vi.fn();
  return { push, replace, router: { push, replace } };
});

vi.mock("next/navigation", () => ({
  useRouter: () => mocks.router,
}));

const MIB = 1024 * 1024;
const VIDEO_ID = "0b6f4f5e-4a7d-4a50-9c2e-6f0e8a1b2c3d";
const LIMITS = {
  max_upload_size_bytes: 10 * MIB,
  allowed_extensions: [".mp4", ".mov", ".webm", ".mkv"],
  allowed_content_types: ["video/mp4", "video/quicktime", "video/webm", "video/x-matroska"],
};
const LIMITS_HINT = "MP4, MOV, WEBM or MKV files up to 10 MB.";

const fetchMock = vi.fn();

function videoFile(name = "clip.mp4", size?: number, type = "video/mp4"): File {
  const file = new File(["data"], name, { type });
  if (size !== undefined) {
    Object.defineProperty(file, "size", { value: size });
  }
  return file;
}

function dropZone() {
  return screen.getByRole("button", { name: /drag and drop a video here/i });
}

function fileInput() {
  return screen.getByLabelText("Video file");
}

function chooseFile(file: File) {
  fireEvent.change(fileInput(), { target: { files: [file] } });
}

async function renderForm(hint = LIMITS_HINT) {
  render(<VideoUploadForm />);
  // Wait for the limits from the backend to be applied.
  expect(await screen.findByText(hint)).toBeInTheDocument();
}

beforeEach(() => {
  mocks.push.mockReset();
  mocks.replace.mockReset();
  FakeXMLHttpRequest.reset();
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({
    ok: true,
    status: 200,
    text: async () => JSON.stringify(LIMITS),
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("XMLHttpRequest", FakeXMLHttpRequest);
  vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "http://api.test");
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("VideoUploadForm", () => {
  it("loads the upload limits from the backend", async () => {
    await renderForm();

    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/api/v1/videos/upload-limits",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fileInput()).toHaveAttribute("accept", ".mp4,.mov,.webm,.mkv");
  });

  it("shows an error for an unsupported extension and does not upload", async () => {
    await renderForm();

    chooseFile(videoFile("notes.avi", undefined, "video/x-msvideo"));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "This file type is not supported. Use MP4, MOV, WEBM or MKV.",
    );
    expect(fileInput()).toHaveAttribute("aria-invalid", "true");
    expect(FakeXMLHttpRequest.instances).toHaveLength(0);
  });

  it("shows an error for a file over the backend limit and does not upload", async () => {
    await renderForm();

    chooseFile(videoFile("clip.mp4", 10 * MIB + 1));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "The file is too large. The maximum size is 10 MB.",
    );
    expect(FakeXMLHttpRequest.instances).toHaveLength(0);
  });

  it("falls back to NEXT_PUBLIC_MAX_UPLOAD_SIZE_MB when the limits cannot be loaded", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubEnv("NEXT_PUBLIC_MAX_UPLOAD_SIZE_MB", "1");
    await renderForm("MP4, MOV, WEBM or MKV files up to 1 MB.");
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    chooseFile(videoFile("clip.mp4", 2 * MIB));

    expect(screen.getByRole("alert")).toHaveTextContent("The maximum size is 1 MB.");
    expect(FakeXMLHttpRequest.instances).toHaveLength(0);
  });

  it("uploads a valid file to the videos endpoint in the `file` field", async () => {
    await renderForm();
    const file = videoFile("clip.mp4", 4 * MIB);

    chooseFile(file);

    const xhr = FakeXMLHttpRequest.latest();
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe("http://api.test/api/v1/videos");
    expect(xhr.withCredentials).toBe(true);
    const sent = (xhr.body as FormData).get("file") as File;
    expect(sent.name).toBe("clip.mp4");
    expect(screen.getByRole("alert")).toBeEmptyDOMElement();
    expect(screen.getByRole("status")).toHaveTextContent("Uploading clip.mp4…");
    expect(screen.getByRole("progressbar", { name: "Upload progress" })).toHaveAttribute(
      "aria-valuenow",
      "0",
    );
    expect(dropZone()).toBeDisabled();
    expect(fileInput()).toBeDisabled();
  });

  it("uploads a dropped file", async () => {
    await renderForm();

    fireEvent.drop(dropZone(), { dataTransfer: { files: [videoFile("drop.webm", 10, "video/webm")] } });

    const sent = (FakeXMLHttpRequest.latest().body as FormData).get("file") as File;
    expect(sent.name).toBe("drop.webm");
  });

  it("starts only one upload when two files arrive before a re-render", async () => {
    await renderForm();

    // Both events run inside one act, so React does not re-render between them
    // and the second handler still sees the idle phase.
    act(() => {
      chooseFile(videoFile("first.mp4"));
      fireEvent.drop(dropZone(), { dataTransfer: { files: [videoFile("second.mp4")] } });
    });

    expect(FakeXMLHttpRequest.instances).toHaveLength(1);
    const sent = (FakeXMLHttpRequest.latest().body as FormData).get("file") as File;
    expect(sent.name).toBe("first.mp4");
  });

  it("rejects dropping several files at once", async () => {
    await renderForm();

    fireEvent.drop(dropZone(), {
      dataTransfer: { files: [videoFile("a.mp4"), videoFile("b.mp4")] },
    });

    expect(screen.getByRole("alert")).toHaveTextContent("Drop one video file at a time.");
    expect(FakeXMLHttpRequest.instances).toHaveLength(0);
  });

  it("updates the progress bar as the upload progresses", async () => {
    await renderForm();
    chooseFile(videoFile());
    const xhr = FakeXMLHttpRequest.latest();

    act(() => xhr.progress(25, 100));
    const progressbar = screen.getByRole("progressbar", { name: "Upload progress" });
    expect(progressbar).toHaveAttribute("aria-valuenow", "25");
    expect(progressbar).toHaveAttribute("aria-valuetext", "25% uploaded");
    expect(progressbar).toHaveAttribute("aria-valuemin", "0");
    expect(progressbar).toHaveAttribute("aria-valuemax", "100");

    act(() => xhr.progress(999, 1000));
    expect(progressbar).toHaveAttribute("aria-valuenow", "99");

    act(() => xhr.finishSending());
    expect(progressbar).toHaveAttribute("aria-valuenow", "100");
    expect(screen.getByRole("status")).toHaveTextContent("Checking and saving clip.mp4");
    // The file has been sent, so cancelling could no longer stop it being stored.
    expect(screen.getByRole("button", { name: "Cancel upload" })).toBeDisabled();
  });

  it("aborts the request when the upload is cancelled", async () => {
    await renderForm();
    chooseFile(videoFile());
    const xhr = FakeXMLHttpRequest.latest();
    act(() => xhr.progress(10, 100));

    fireEvent.click(screen.getByRole("button", { name: "Cancel upload" }));

    expect(xhr.aborted).toBe(true);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Upload cancelled."));
    expect(screen.getByRole("alert")).toBeEmptyDOMElement();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(mocks.push).not.toHaveBeenCalled();
    expect(dropZone()).toBeEnabled();
    await waitFor(() => expect(dropZone()).toHaveFocus());
  });

  it("aborts the upload when the page is left", async () => {
    const { unmount } = render(<VideoUploadForm />);
    await screen.findByText(LIMITS_HINT);
    chooseFile(videoFile());
    const xhr = FakeXMLHttpRequest.latest();

    unmount();

    expect(xhr.aborted).toBe(true);
  });

  it("navigates to the video detail page on success", async () => {
    await renderForm();
    chooseFile(videoFile());
    const xhr = FakeXMLHttpRequest.latest();

    act(() => {
      xhr.finishSending();
      xhr.respond(201, { id: VIDEO_ID, original_filename: "clip.mp4", status: "uploaded" });
    });

    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith(`/videos/${VIDEO_ID}`));
    expect(mocks.push).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("status")).toHaveTextContent("Upload complete.");
    // No second upload can start while the page changes.
    expect(fileInput()).toBeDisabled();
  });

  it("shows the backend error and allows another attempt", async () => {
    await renderForm();
    chooseFile(videoFile());
    const xhr = FakeXMLHttpRequest.latest();

    act(() =>
      xhr.respond(415, {
        error: {
          code: "file_type_mismatch",
          message: "The file contents do not match a supported video format for its extension.",
          details: null,
        },
      }),
    );

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "The file contents do not match a supported video format for its extension.",
      ),
    );
    expect(screen.getByRole("alert")).toHaveAttribute("aria-live", "assertive");
    expect(mocks.push).not.toHaveBeenCalled();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(fileInput()).toBeEnabled();
    await waitFor(() => expect(dropZone()).toHaveFocus());
  });

  it("shows a network error", async () => {
    await renderForm();
    chooseFile(videoFile());

    act(() => FakeXMLHttpRequest.latest().failNetwork());

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Could not reach the server."),
    );
  });

  it("lets keyboard users open the file picker from the labelled drop zone", async () => {
    await renderForm();
    const clickSpy = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => {});
    const zone = dropZone();

    // A native <button>: focusable and activated with Enter or Space.
    expect(zone.tagName).toBe("BUTTON");
    expect(zone).toHaveAttribute("type", "button");
    zone.focus();
    expect(zone).toHaveFocus();
    expect(zone).toHaveAccessibleDescription(LIMITS_HINT);

    fireEvent.click(zone);

    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(clickSpy.mock.contexts[0]).toBe(fileInput());
  });

  it("keeps the labelled file input in the tab order", async () => {
    await renderForm();
    const input = fileInput();

    expect(input).toHaveAttribute("type", "file");
    expect(input).not.toHaveAttribute("tabindex");
    input.focus();
    expect(input).toHaveFocus();
    expect(input).toHaveAccessibleDescription(/MP4, MOV, WEBM or MKV files up to 10 MB\./);
  });
});
