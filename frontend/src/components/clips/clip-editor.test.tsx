import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ClipEditor } from "@/components/clips/clip-editor";
import type { Caption } from "@/lib/api/captions";
import type { Clip } from "@/lib/api/clips";
import type { Video } from "@/lib/api/videos";

vi.mock("next/link", () => ({
  default: function MockLink({ href, children, ...rest }: { href: string; children: ReactNode }) {
    return (
      <a href={href} {...rest}>
        {children}
      </a>
    );
  },
}));

const API = "http://api.test";
const CLIP_ID = "clip-1";
const VIDEO_ID = "vid-1";
const CLIP_PATH = `/api/v1/clips/${CLIP_ID}`;
const CAPTIONS_PATH = `${CLIP_PATH}/captions`;
const fetchMock = vi.fn();

function makeClip(overrides: Partial<Clip> = {}): Clip {
  return {
    id: CLIP_ID,
    video_id: VIDEO_ID,
    start_seconds: 10,
    end_seconds: 40,
    title: "Big idea",
    reason: "Dense speech.",
    score: 0.8,
    status: "accepted",
    source: "heuristic",
    created_at: "2026-03-04T10:00:00Z",
    updated_at: "2026-03-04T10:00:00Z",
    ...overrides,
  };
}

function makeVideo(overrides: Partial<Video> = {}): Video {
  return {
    id: VIDEO_ID,
    user_id: "u1",
    original_filename: "talk.mp4",
    content_type: "video/mp4",
    size_bytes: 1024,
    status: "ready",
    error_message: null,
    duration_seconds: 120,
    width: 1920,
    height: 1080,
    fps: 30,
    created_at: "2026-03-04T10:00:00Z",
    updated_at: "2026-03-04T10:00:00Z",
    project: null,
    ...overrides,
  };
}

function makeCaption(index: number, start: number, end: number, text: string): Caption {
  return { id: `cap-${index}`, index, start_seconds: start, end_seconds: end, text };
}

const STYLE = {
  clip_id: CLIP_ID,
  preset: "classic",
  font_family: "Arial",
  font_size: 64,
  primary_color: "#FFFFFFFF",
  outline_color: "#000000FF",
  position: "bottom",
  uppercase: false,
  max_chars_per_line: 42,
  is_default: true,
  allowed_fonts: ["Arial"],
};

type FakeResponse = { ok: boolean; status: number; text: () => Promise<string> };

function jsonResponse(body: unknown, status = 200): FakeResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
  };
}

function errorResponse(status: number, code: string, message: string, details: unknown = null): FakeResponse {
  return jsonResponse({ error: { code, message, details } }, status);
}

type Call = { path: string; method: string; body: unknown };
type Route = (call: Call) => FakeResponse | Promise<FakeResponse>;

interface Backend {
  clip: Clip;
  video: Video;
  captions: Caption[];
  routes: Record<string, Route>;
}

let backend: Backend;

function calls(): Call[] {
  return (fetchMock.mock.calls as [string, RequestInit | undefined][]).map(([url, init]) => ({
    path: url.slice(API.length),
    method: init?.method ?? "GET",
    body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
  }));
}

function callsTo(method: string, path: string): Call[] {
  return calls().filter((call) => call.method === method && call.path === path);
}

function captionList(items: Caption[]) {
  return { clip_id: CLIP_ID, items, total: items.length };
}

function serve() {
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const call: Call = {
      path: url.slice(API.length),
      method: init?.method ?? "GET",
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    };
    const override = backend.routes[`${call.method} ${call.path}`];
    if (override) {
      return override(call);
    }
    switch (`${call.method} ${call.path}`) {
      case `GET ${CLIP_PATH}`:
        return jsonResponse(backend.clip);
      case `GET /api/v1/videos/${VIDEO_ID}`:
        return jsonResponse(backend.video);
      case "GET /api/v1/clips/limits":
        return jsonResponse({ min_seconds: 15, max_seconds: 90 });
      case `GET ${CLIP_PATH}/caption-style`:
        return jsonResponse(STYLE);
      case `GET ${CAPTIONS_PATH}`:
        return jsonResponse(captionList(backend.captions));
      case `PATCH ${CLIP_PATH}`: {
        const body = call.body as { start_seconds: number; end_seconds: number };
        backend.clip = makeClip({ start_seconds: body.start_seconds, end_seconds: body.end_seconds });
        return jsonResponse(backend.clip);
      }
      case `PUT ${CAPTIONS_PATH}`: {
        const body = call.body as { captions: { start_seconds: number; end_seconds: number; text: string }[] };
        backend.captions = body.captions.map((c, index) => makeCaption(index, c.start_seconds, c.end_seconds, c.text));
        return jsonResponse(captionList(backend.captions));
      }
      case `POST ${CAPTIONS_PATH}/generate`:
        backend.captions = [makeCaption(0, 0, 2.5, "Generated line")];
        return jsonResponse(captionList(backend.captions));
      default:
        return errorResponse(404, "not_found", "Not Found");
    }
  });
}

/** Gives the rendered `<video>` a controllable fake media state. */
function mockMedia({ currentTime = 10, paused = true } = {}) {
  const element = screen.getByLabelText("Clip preview: Big idea") as HTMLVideoElement;
  const media = { readyState: 1, currentTime, paused };
  Object.defineProperty(element, "readyState", { configurable: true, get: () => media.readyState });
  Object.defineProperty(element, "duration", { configurable: true, get: () => 120 });
  Object.defineProperty(element, "paused", { configurable: true, get: () => media.paused });
  Object.defineProperty(element, "currentTime", {
    configurable: true,
    get: () => media.currentTime,
    set: (value: number) => {
      media.currentTime = value;
    },
  });
  const play = vi.fn(() => {
    media.paused = false;
    return Promise.resolve();
  });
  const pause = vi.fn(() => {
    media.paused = true;
  });
  element.play = play;
  element.pause = pause;
  return { element, media, play, pause };
}

async function renderEditor() {
  render(<ClipEditor clipId={CLIP_ID} />);
  await screen.findByRole("heading", { level: 1, name: "Edit clip: Big idea" });
}

function startInput() {
  return screen.getByLabelText("Start (seconds)") as HTMLInputElement;
}

function endInput() {
  return screen.getByLabelText("End (seconds)") as HTMLInputElement;
}

function saveButton() {
  return screen.getByRole("button", { name: "Save changes" });
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", API);
  backend = {
    clip: makeClip(),
    video: makeVideo(),
    captions: [makeCaption(0, 0, 2, "Hello there"), makeCaption(1, 2, 4.5, "Second line")],
    routes: {},
  };
  serve();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("ClipEditor loading", () => {
  it("shows the clip range, captions and a saved state", async () => {
    await renderEditor();

    expect(startInput().value).toBe("10");
    expect(endInput().value).toBe("40");
    expect(screen.getByLabelText("Caption 1 text")).toHaveValue("Hello there");
    expect(screen.getByLabelText("Caption 2 end (seconds)")).toHaveValue("4.5");
    expect(screen.getByTestId("save-state")).toHaveTextContent("All changes saved");
    expect(saveButton()).toBeDisabled();
    expect(screen.getByLabelText("Clip preview: Big idea")).toHaveAttribute("src", `${API}/api/v1/videos/${VIDEO_ID}/media`);
  });

  it("shows not found for a missing or another user's clip", async () => {
    backend.routes[`GET ${CLIP_PATH}`] = () => errorResponse(404, "not_found", "Clip not found.");

    render(<ClipEditor clipId={CLIP_ID} />);

    expect(await screen.findByRole("heading", { name: "Clip not found" })).toBeInTheDocument();
  });

  it("labels every control", async () => {
    await renderEditor();

    for (const button of screen.getAllByRole("button")) {
      expect(button).toHaveAccessibleName();
    }
    for (const field of screen.getAllByRole("textbox")) {
      expect(field).toHaveAccessibleName();
    }
    expect(screen.getByRole("checkbox", { name: "Loop playback" })).toBeInTheDocument();
  });
});

describe("ClipEditor boundaries", () => {
  it("nudges the boundaries by 0.5 s and 1 s", async () => {
    await renderEditor();

    fireEvent.click(screen.getByRole("button", { name: "Move the start 0.5 seconds later" }));
    fireEvent.click(screen.getByRole("button", { name: "Move the start 1 second later" }));
    expect(startInput().value).toBe("11.5");

    fireEvent.click(screen.getByRole("button", { name: "Move the end 1 second earlier" }));
    fireEvent.click(screen.getByRole("button", { name: "Move the end 0.5 seconds earlier" }));
    expect(endInput().value).toBe("38.5");

    expect(screen.getByTestId("save-state")).toHaveTextContent("Unsaved changes");
    expect(saveButton()).toBeEnabled();
  });

  it("keeps nudges within the video", async () => {
    backend.clip = makeClip({ start_seconds: 0.3, end_seconds: 119.8 });
    await renderEditor();

    fireEvent.click(screen.getByRole("button", { name: "Move the start 0.5 seconds earlier" }));
    fireEvent.click(screen.getByRole("button", { name: "Move the end 1 second later" }));

    expect(startInput().value).toBe("0");
    expect(endInput().value).toBe("120");
  });

  it("blocks saving an invalid range", async () => {
    await renderEditor();

    // 10-20 s is shorter than the 15 s minimum.
    fireEvent.change(endInput(), { target: { value: "20" } });
    expect(screen.getByText(/A clip must be between 15 and 90 seconds long/)).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
    fireEvent.click(saveButton());

    // An end before the start.
    fireEvent.change(endInput(), { target: { value: "5" } });
    expect(screen.getByText("The end must be after the start.")).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();

    // Past the end of the video.
    fireEvent.change(endInput(), { target: { value: "125" } });
    expect(screen.getByText(/The end must be within the video/)).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();

    expect(callsTo("PATCH", CLIP_PATH)).toHaveLength(0);
  });

  it("sets a boundary to the player's current time", async () => {
    await renderEditor();
    const { media } = mockMedia();

    media.currentTime = 33.25;
    fireEvent.click(screen.getByRole("button", { name: "Set end to current time" }));
    expect(endInput().value).toBe("33.25");

    media.currentTime = 12.1234;
    fireEvent.click(screen.getByRole("button", { name: "Set start to current time" }));
    expect(startInput().value).toBe("12.123");
  });

  it("saves the range with PATCH", async () => {
    await renderEditor();

    fireEvent.click(screen.getByRole("button", { name: "Move the start 0.5 seconds later" }));
    fireEvent.change(endInput(), { target: { value: "45" } });
    fireEvent.click(saveButton());

    await waitFor(() => expect(screen.getByTestId("save-state")).toHaveTextContent("All changes saved"));
    expect(callsTo("PATCH", CLIP_PATH)).toEqual([
      { path: CLIP_PATH, method: "PATCH", body: { start_seconds: 10.5, end_seconds: 45 } },
    ]);
    // Unchanged captions are not sent.
    expect(callsTo("PUT", CAPTIONS_PATH)).toHaveLength(0);
    expect(screen.getByText("Saved the boundaries.")).toBeInTheDocument();
  });

  it("shows a backend error for the range and keeps the edit", async () => {
    backend.routes[`PATCH ${CLIP_PATH}`] = () =>
      errorResponse(409, "video_not_ready", "The video must be ready before its clips can be changed.");
    await renderEditor();

    fireEvent.change(startInput(), { target: { value: "12" } });
    fireEvent.click(saveButton());

    expect(
      await screen.findByText(
        "Could not save the boundaries. The video must be ready before its clips can be changed.",
      ),
    ).toBeInTheDocument();
    expect(startInput().value).toBe("12");
    expect(screen.getByTestId("save-state")).toHaveTextContent("Unsaved changes");
  });
});

describe("ClipEditor captions", () => {
  it("sends edited captions with PUT", async () => {
    await renderEditor();

    fireEvent.change(screen.getByLabelText("Caption 1 text"), { target: { value: "Hello  everyone" } });
    fireEvent.change(screen.getByLabelText("Caption 2 start (seconds)"), { target: { value: "2.25" } });
    fireEvent.click(saveButton());

    await waitFor(() => expect(screen.getByTestId("save-state")).toHaveTextContent("All changes saved"));
    expect(callsTo("PUT", CAPTIONS_PATH).map((call) => call.body)).toEqual([
      {
        captions: [
          { start_seconds: 0, end_seconds: 2, text: "Hello everyone" },
          { start_seconds: 2.25, end_seconds: 4.5, text: "Second line" },
        ],
      },
    ]);
    expect(callsTo("PATCH", CLIP_PATH)).toHaveLength(0);
  });

  it("adds and removes captions", async () => {
    await renderEditor();

    fireEvent.click(screen.getByRole("button", { name: "Add caption" }));
    expect(screen.getByLabelText("Caption 3 start (seconds)")).toHaveValue("4.5");
    expect(screen.getByLabelText("Caption 3 end (seconds)")).toHaveValue("6.5");
    // A new caption needs text before it can be saved.
    expect(screen.getByText("Enter the caption text.")).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Caption 3 text"), { target: { value: "Third line" } });
    fireEvent.click(screen.getByRole("button", { name: "Remove caption 1" }));
    expect(screen.queryByDisplayValue("Hello there")).not.toBeInTheDocument();
    fireEvent.click(saveButton());

    await waitFor(() => expect(callsTo("PUT", CAPTIONS_PATH)).toHaveLength(1));
    expect(callsTo("PUT", CAPTIONS_PATH)[0].body).toEqual({
      captions: [
        { start_seconds: 2, end_seconds: 4.5, text: "Second line" },
        { start_seconds: 4.5, end_seconds: 6.5, text: "Third line" },
      ],
    });
  });

  it("blocks saving overlapping captions", async () => {
    await renderEditor();

    fireEvent.change(screen.getByLabelText("Caption 2 start (seconds)"), { target: { value: "1.5" } });

    expect(screen.getByText(/Captions must not overlap/)).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
  });

  it("shows backend validation errors for a caption", async () => {
    backend.routes[`PUT ${CAPTIONS_PATH}`] = () =>
      errorResponse(422, "caption_line_too_long", "Caption lines must be at most 42 characters.", {
        index: 1,
        max_chars_per_line: 42,
      });
    await renderEditor();

    fireEvent.change(screen.getByLabelText("Caption 2 text"), { target: { value: "Changed" } });
    fireEvent.click(saveButton());

    expect(
      await screen.findByText("Could not save the captions. Caption 2: Caption lines must be at most 42 characters."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Caption 2 text")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Caption 2 text")).toHaveValue("Changed");
    expect(screen.getByTestId("save-state")).toHaveTextContent("Unsaved changes");
  });

  it("generates captions when there are none", async () => {
    backend.captions = [];
    await renderEditor();
    expect(screen.getByText("This clip has no captions yet.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Generate captions" }));

    expect(await screen.findByDisplayValue("Generated line")).toBeInTheDocument();
    expect(callsTo("POST", `${CAPTIONS_PATH}/generate`)).toHaveLength(1);
    expect(screen.getByTestId("save-state")).toHaveTextContent("All changes saved");
  });

  it("shows a generation error", async () => {
    backend.captions = [];
    backend.routes[`POST ${CAPTIONS_PATH}/generate`] = () =>
      errorResponse(409, "transcript_required", "The video has no transcript. Transcribe it first.");
    await renderEditor();

    fireEvent.click(screen.getByRole("button", { name: "Generate captions" }));

    expect(
      await screen.findByText("Could not generate captions. The video has no transcript. Transcribe it first."),
    ).toBeInTheDocument();
  });

  it("saves the range before the captions, validating captions against the new length", async () => {
    await renderEditor();

    fireEvent.change(endInput(), { target: { value: "30" } });
    fireEvent.change(screen.getByLabelText("Caption 1 text"), { target: { value: "Hi" } });
    fireEvent.click(saveButton());

    await waitFor(() => expect(callsTo("PUT", CAPTIONS_PATH)).toHaveLength(1));
    const writes = calls().filter((call) => call.method !== "GET");
    expect(writes.map((call) => call.method)).toEqual(["PATCH", "PUT"]);
  });

  it("blocks saving a range that cuts off unedited captions", async () => {
    backend.captions = [makeCaption(0, 0, 2, "Hello there"), makeCaption(1, 18, 25, "Late line")];
    await renderEditor();

    // 10-30 s is 20 s long, so the saved caption at 18-25 s would end after the clip.
    fireEvent.change(endInput(), { target: { value: "30" } });

    expect(screen.getByText(/Captions must lie within the clip/)).toBeInTheDocument();
    expect(screen.getByText(/The new range must still fit every caption/)).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
    fireEvent.click(saveButton());
    expect(callsTo("PATCH", CLIP_PATH)).toHaveLength(0);

    // Once the captions fit, the range and the captions are saved together.
    fireEvent.click(screen.getByRole("button", { name: "Remove caption 2" }));
    expect(saveButton()).toBeEnabled();
    fireEvent.click(saveButton());

    await waitFor(() => expect(screen.getByTestId("save-state")).toHaveTextContent("All changes saved"));
    expect(callsTo("PATCH", CLIP_PATH).map((call) => call.body)).toEqual([{ start_seconds: 10, end_seconds: 30 }]);
    expect(callsTo("PUT", CAPTIONS_PATH).map((call) => call.body)).toEqual([
      { captions: [{ start_seconds: 0, end_seconds: 2, text: "Hello there" }] },
    ]);
  });
});

describe("ClipEditor preview", () => {
  it("shows the caption for the playhead in clip time", async () => {
    await renderEditor();
    const { element, media } = mockMedia();
    const overlay = screen.getByTestId("caption-overlay");

    // Source 11 s is clip time 1 s (the clip starts at 10 s).
    media.currentTime = 11;
    act(() => {
      element.dispatchEvent(new Event("timeupdate"));
    });
    expect(overlay).toHaveTextContent("Hello there");

    media.currentTime = 12;
    act(() => {
      element.dispatchEvent(new Event("timeupdate"));
    });
    expect(overlay).toHaveTextContent("Second line");
    expect(overlay).not.toHaveTextContent("Hello there");

    // A gap between captions, and a time before the clip.
    media.currentTime = 20;
    act(() => {
      element.dispatchEvent(new Event("timeupdate"));
    });
    expect(overlay.textContent).toBe("");
    media.currentTime = 5;
    act(() => {
      element.dispatchEvent(new Event("timeupdate"));
    });
    expect(overlay.textContent).toBe("");
    expect(screen.getByTestId("playhead")).toHaveTextContent("Playhead outside the clip");
  });

  it("plays from the clip start and stops at the clip end", async () => {
    await renderEditor();
    const { element, media, play, pause } = mockMedia({ currentTime: 0 });

    fireEvent.click(screen.getByRole("button", { name: "Play clip" }));
    expect(media.currentTime).toBe(10);
    expect(play).toHaveBeenCalledTimes(1);

    media.currentTime = 40.2;
    act(() => {
      element.dispatchEvent(new Event("timeupdate"));
    });
    expect(pause).toHaveBeenCalled();
    expect(media.currentTime).toBe(40);
  });

  it("restarts the clip when the native controls play from the clip end", async () => {
    await renderEditor();
    const { element, media, pause } = mockMedia({ currentTime: 40 });

    // The native play button starts playback without going through "Play clip".
    media.paused = false;
    act(() => {
      element.dispatchEvent(new Event("play"));
    });

    expect(media.currentTime).toBe(10);
    expect(pause).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
  });

  it("loops at the clip end when looping is on", async () => {
    await renderEditor();
    const { element, media } = mockMedia({ paused: false });

    fireEvent.click(screen.getByRole("checkbox", { name: "Loop playback" }));
    media.currentTime = 40.1;
    act(() => {
      element.dispatchEvent(new Event("timeupdate"));
    });

    expect(media.currentTime).toBe(10);
    expect(media.paused).toBe(false);
  });
});

describe("ClipEditor unsaved changes", () => {
  function clickBackLink() {
    let reached = false;
    const after = () => {
      reached = true;
    };
    // Stands in for the navigation itself, which jsdom does not implement.
    const block = (event: Event) => event.preventDefault();
    window.addEventListener("click", after);
    window.addEventListener("click", block);
    fireEvent.click(screen.getByRole("link", { name: "Back to the video" }));
    window.removeEventListener("click", after);
    window.removeEventListener("click", block);
    return reached;
  }

  it("does not warn without changes", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await renderEditor();

    expect(clickBackLink()).toBe(true);
    expect(confirm).not.toHaveBeenCalled();
    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(false);
  });

  it("asks before leaving with unsaved changes and stays when declined", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await renderEditor();
    fireEvent.change(startInput(), { target: { value: "12" } });

    expect(clickBackLink()).toBe(false);
    expect(confirm).toHaveBeenCalledTimes(1);

    const unload = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);

    confirm.mockReturnValue(true);
    expect(clickBackLink()).toBe(true);
  });

  it("stops warning once the changes are saved", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await renderEditor();
    fireEvent.change(startInput(), { target: { value: "12" } });
    fireEvent.click(saveButton());
    await waitFor(() => expect(screen.getByTestId("save-state")).toHaveTextContent("All changes saved"));

    expect(clickBackLink()).toBe(true);
    expect(confirm).not.toHaveBeenCalled();
  });

  it("discards changes back to the last saved version", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await renderEditor();
    fireEvent.change(startInput(), { target: { value: "12" } });
    fireEvent.change(screen.getByLabelText("Caption 1 text"), { target: { value: "Changed" } });

    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));

    expect(startInput().value).toBe("10");
    expect(screen.getByLabelText("Caption 1 text")).toHaveValue("Hello there");
    expect(screen.getByTestId("save-state")).toHaveTextContent("All changes saved");
    expect(within(screen.getByRole("list", { name: "Caption lines" })).getAllByRole("listitem")).toHaveLength(2);
  });
});
