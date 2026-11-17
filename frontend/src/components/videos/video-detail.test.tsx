import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { VIDEO_POLL_INTERVAL_MS, VideoDetail } from "@/components/videos/video-detail";
import { JOB_POLL_INTERVAL_MS, MAX_JOB_POLL_FAILURES } from "@/components/videos/use-tracked-job";
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
const VIDEO_ID = "vid-1";
const fetchMock = vi.fn();

function makeVideo(overrides: Partial<Video> = {}): Video {
  return {
    id: VIDEO_ID,
    user_id: "u1",
    original_filename: "talk.mp4",
    content_type: "video/mp4",
    size_bytes: 2 * 1024 * 1024,
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

function makeTranscript(segments: unknown[]) {
  return {
    id: "tr-1",
    video_id: VIDEO_ID,
    language: "en",
    requested_language: null,
    provider: "fake",
    model: null,
    full_text: "",
    timing_granularity: "segment",
    created_at: "2026-03-04T10:00:00Z",
    segments,
  };
}

function segment(index: number, start: number | null, end: number | null, text: string) {
  return { id: `seg-${index}`, index, start_seconds: start, end_seconds: end, text, words: null };
}

function makeClip(overrides: Partial<Clip> = {}): Clip {
  return {
    id: "clip-1",
    video_id: VIDEO_ID,
    start_seconds: 10,
    end_seconds: 40,
    title: "Big idea",
    reason: "Dense speech with a question.",
    score: 0.82,
    status: "suggested",
    source: "heuristic",
    created_at: "2026-03-04T10:00:00Z",
    updated_at: "2026-03-04T10:00:00Z",
    ...overrides,
  };
}

function makeJob(status: string, errorMessage: string | null = null) {
  return {
    id: "job-1",
    job_type: "transcription",
    status,
    video_id: VIDEO_ID,
    attempts: 1,
    max_attempts: 3,
    error_message: errorMessage,
    created_at: "2026-03-04T10:00:00Z",
    started_at: null,
    finished_at: null,
  };
}

type FakeResponse = { ok: boolean; status: number; text: () => Promise<string> };

function jsonResponse(body: unknown, status = 200): FakeResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
  };
}

function errorResponse(status: number, code: string, message: string): FakeResponse {
  return jsonResponse({ error: { code, message, details: null } }, status);
}

type Call = { path: string; method: string; body: unknown };
type Route = (call: Call) => FakeResponse | Promise<FakeResponse>;

/** A small in-memory backend; `routes` (keyed "METHOD /path") override the defaults. */
interface Backend {
  video: Video;
  transcript: unknown;
  clips: Clip[];
  /** Answers for `GET /jobs/{id}` in order, repeating the last one. */
  jobs: unknown[];
  routes: Record<string, Route>;
}

let backend: Backend;
let jobReads = 0;

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

const VIDEO_PATH = `/api/v1/videos/${VIDEO_ID}`;
const JOB_PATH = "/api/v1/jobs/job-1";

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
      case `GET ${VIDEO_PATH}`:
        return jsonResponse(backend.video);
      case `GET ${VIDEO_PATH}/transcript`:
        return backend.transcript
          ? jsonResponse(backend.transcript)
          : errorResponse(404, "transcript_not_found", "This video has no transcript.");
      case `GET ${VIDEO_PATH}/clips`:
        return jsonResponse({ items: backend.clips, total: backend.clips.length });
      case `POST ${VIDEO_PATH}/transcription`:
      case `POST ${VIDEO_PATH}/clips/detect`:
        return jsonResponse({ job_id: "job-1", status: "queued" }, 202);
      case `GET ${JOB_PATH}`: {
        const answer = backend.jobs[Math.min(jobReads, backend.jobs.length - 1)];
        jobReads += 1;
        return jsonResponse(answer);
      }
      default:
        return errorResponse(404, "not_found", "Not Found");
    }
  });
}

/** Gives the rendered `<video>` a controllable fake media state. */
function mockMedia({ readyState = 1, duration = 120 } = {}) {
  const element = screen.getByLabelText("Source video: talk.mp4") as HTMLVideoElement;
  const media = { readyState, currentTime: 0 };
  Object.defineProperty(element, "readyState", { configurable: true, get: () => media.readyState });
  Object.defineProperty(element, "duration", { configurable: true, get: () => duration });
  Object.defineProperty(element, "currentTime", {
    configurable: true,
    get: () => media.currentTime,
    set: (value: number) => {
      media.currentTime = value;
    },
  });
  const play = vi.fn(() => Promise.resolve());
  const pause = vi.fn(() => {
    element.dispatchEvent(new Event("pause"));
  });
  element.play = play;
  element.pause = pause;
  return { element, media, play, pause };
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

let visibility: DocumentVisibilityState = "visible";

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", API);
  backend = { video: makeVideo(), transcript: null, clips: [], jobs: [makeJob("queued")], routes: {} };
  jobReads = 0;
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => visibility,
  });
  serve();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("VideoDetail video", () => {
  it("shows the player, metadata and status", async () => {
    render(<VideoDetail videoId={VIDEO_ID} />);

    expect(await screen.findByRole("heading", { level: 1, name: "talk.mp4" })).toBeInTheDocument();
    expect(screen.getByLabelText("Source video: talk.mp4")).toHaveAttribute("src", `${API}${VIDEO_PATH}/media`);
    expect(screen.getByText("Ready")).toHaveAttribute("data-status", "ready");
    expect(screen.getByText("2:00")).toBeInTheDocument();
    expect(screen.getByText("1920 × 1080")).toBeInTheDocument();
    expect(screen.getByText("30 fps")).toBeInTheDocument();
    expect(screen.getByText("2 MB")).toBeInTheDocument();
  });

  it("shows not found for a missing or another user's video", async () => {
    backend.routes[`GET ${VIDEO_PATH}`] = () => errorResponse(404, "not_found", "Video not found.");

    render(<VideoDetail videoId={VIDEO_ID} />);

    expect(await screen.findByRole("heading", { name: "Video not found" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /generate transcript/i })).not.toBeInTheDocument();
  });

  it("shows a processing failure message", async () => {
    backend.video = makeVideo({ status: "failed", error_message: "The video is too long." });

    render(<VideoDetail videoId={VIDEO_ID} />);

    expect(await screen.findByText("Processing failed. The video is too long.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate transcript" })).toBeDisabled();
  });

  it("re-reads a processing video until it is ready", async () => {
    vi.useFakeTimers();
    backend.video = makeVideo({ status: "processing" });
    render(<VideoDetail videoId={VIDEO_ID} />);
    await advance(0);
    expect(screen.getByText("Processing")).toBeInTheDocument();

    backend.video = makeVideo({ status: "ready" });
    await advance(VIDEO_POLL_INTERVAL_MS);
    expect(screen.getByText("Ready")).toBeInTheDocument();
    const reads = callsTo("GET", VIDEO_PATH).length;

    await advance(VIDEO_POLL_INTERVAL_MS * 3);
    expect(callsTo("GET", VIDEO_PATH)).toHaveLength(reads);
  });
});

describe("VideoDetail transcript", () => {
  it("renders the transcript and seeks the player when a segment is clicked", async () => {
    backend.transcript = makeTranscript([
      segment(0, 0, 5, "Hello world"),
      segment(1, 65.5, 70, "Second part"),
      segment(2, null, null, "No timing here"),
    ]);
    render(<VideoDetail videoId={VIDEO_ID} />);
    const list = await screen.findByRole("list", { name: "Transcript segments" });
    const { media } = mockMedia();

    expect(within(list).getByRole("button", { name: /0:00\s*Hello world/ })).toBeInTheDocument();
    fireEvent.click(within(list).getByRole("button", { name: /1:05\s*Second part/ }));
    expect(media.currentTime).toBe(65.5);

    // A segment without a timestamp is shown but cannot seek.
    expect(within(list).getByText("No timing here")).toBeInTheDocument();
    expect(within(list).queryByRole("button", { name: /No timing here/ })).not.toBeInTheDocument();
  });

  it("waits for the media metadata before seeking", async () => {
    backend.transcript = makeTranscript([segment(0, 30, 35, "Later")]);
    render(<VideoDetail videoId={VIDEO_ID} />);
    await screen.findByRole("button", { name: /Later/ });
    const { element, media } = mockMedia({ readyState: 0 });

    fireEvent.click(screen.getByRole("button", { name: /Later/ }));
    expect(media.currentTime).toBe(0);

    media.readyState = 1;
    element.dispatchEvent(new Event("loadedmetadata"));
    expect(media.currentTime).toBe(30);

    // The listener is gone: later metadata events do not seek again.
    media.currentTime = 50;
    element.dispatchEvent(new Event("loadedmetadata"));
    expect(media.currentTime).toBe(50);
  });

  it("shows an empty transcript", async () => {
    backend.transcript = makeTranscript([]);

    render(<VideoDetail videoId={VIDEO_ID} />);

    expect(await screen.findByText(/the transcript is empty/i)).toBeInTheDocument();
  });

  it("offers SRT and VTT downloads once a transcript exists", async () => {
    backend.transcript = makeTranscript([segment(0, 0, 5, "Hello")]);

    render(<VideoDetail videoId={VIDEO_ID} />);

    expect(await screen.findByRole("link", { name: "Download SRT" })).toHaveAttribute(
      "href",
      `${API}${VIDEO_PATH}/transcript.srt`,
    );
    expect(screen.getByRole("link", { name: "Download SRT" })).toHaveAttribute("download");
    expect(screen.getByRole("link", { name: "Download VTT" })).toHaveAttribute(
      "href",
      `${API}${VIDEO_PATH}/transcript.vtt`,
    );
  });

  it("has no download links without a transcript", async () => {
    render(<VideoDetail videoId={VIDEO_ID} />);

    expect(await screen.findByText("This video has no transcript yet.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Download SRT" })).not.toBeInTheDocument();
  });

  it("refreshes the transcript on request", async () => {
    backend.transcript = makeTranscript([segment(0, 0, 5, "Old text")]);
    render(<VideoDetail videoId={VIDEO_ID} />);
    await screen.findByText("Old text");

    backend.transcript = makeTranscript([segment(0, 0, 5, "New text")]);
    fireEvent.click(screen.getByRole("button", { name: "Refresh transcript" }));

    expect(await screen.findByText("New text")).toBeInTheDocument();
    expect(screen.queryByText("Old text")).not.toBeInTheDocument();
  });

  it("starts a transcription job and polls it until it completes", async () => {
    vi.useFakeTimers();
    backend.jobs = [makeJob("running"), makeJob("running"), makeJob("succeeded")];
    render(<VideoDetail videoId={VIDEO_ID} />);
    await advance(0);

    fireEvent.change(screen.getByLabelText("Language"), { target: { value: "hi" } });
    fireEvent.click(screen.getByRole("button", { name: "Generate transcript" }));
    await advance(0);

    expect(callsTo("POST", `${VIDEO_PATH}/transcription`)).toEqual([
      { path: `${VIDEO_PATH}/transcription`, method: "POST", body: { language: "hi" } },
    ]);
    expect(screen.getByText("Transcription is queued…")).toBeInTheDocument();
    // Pending: a second job cannot be started.
    expect(screen.getByRole("button", { name: "Transcribing…" })).toBeDisabled();

    await advance(JOB_POLL_INTERVAL_MS);
    expect(callsTo("GET", JOB_PATH)).toHaveLength(1);
    expect(screen.getByText("Transcribing the video…")).toBeInTheDocument();

    await advance(JOB_POLL_INTERVAL_MS);
    expect(callsTo("GET", JOB_PATH)).toHaveLength(2);

    backend.transcript = makeTranscript([segment(0, 0, 5, "Fresh words")]);
    await advance(JOB_POLL_INTERVAL_MS);
    expect(callsTo("GET", JOB_PATH)).toHaveLength(3);
    expect(screen.getByText("The transcript is ready.")).toBeInTheDocument();
    expect(screen.getByText("Fresh words")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regenerate transcript" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Find clips" })).toBeEnabled();

    // Polling stops at the terminal status.
    await advance(JOB_POLL_INTERVAL_MS * 5);
    expect(callsTo("GET", JOB_PATH)).toHaveLength(3);
  });

  it("sends no language for automatic detection", async () => {
    render(<VideoDetail videoId={VIDEO_ID} />);
    fireEvent.click(await screen.findByRole("button", { name: "Generate transcript" }));

    await waitFor(() => expect(callsTo("POST", `${VIDEO_PATH}/transcription`)).toHaveLength(1));
    expect(callsTo("POST", `${VIDEO_PATH}/transcription`)[0].body).toEqual({});
  });

  it("shows the job's error message when transcription fails", async () => {
    vi.useFakeTimers();
    backend.jobs = [makeJob("failed", "The transcription service failed. Try again later.")];
    render(<VideoDetail videoId={VIDEO_ID} />);
    await advance(0);

    fireEvent.click(screen.getByRole("button", { name: "Generate transcript" }));
    await advance(0);
    await advance(JOB_POLL_INTERVAL_MS);

    expect(
      screen.getByText("Transcription failed. The transcription service failed. Try again later."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Generate transcript" })).toBeEnabled();
    expect(screen.queryByText("The transcript is ready.")).not.toBeInTheDocument();

    await advance(JOB_POLL_INTERVAL_MS * 3);
    expect(callsTo("GET", JOB_PATH)).toHaveLength(1);
  });

  it("shows why transcription could not be started", async () => {
    backend.routes[`POST ${VIDEO_PATH}/transcription`] = () =>
      errorResponse(409, "transcription_in_progress", "This video is already being transcribed.");
    render(<VideoDetail videoId={VIDEO_ID} />);

    fireEvent.click(await screen.findByRole("button", { name: "Generate transcript" }));

    expect(
      await screen.findByText("Could not start transcription. This video is already being transcribed."),
    ).toBeInTheDocument();
  });

  it("stops polling after repeated status failures without reporting success", async () => {
    vi.useFakeTimers();
    backend.routes[`GET ${JOB_PATH}`] = () => Promise.reject(new TypeError("Failed to fetch"));
    render(<VideoDetail videoId={VIDEO_ID} />);
    await advance(0);

    fireEvent.click(screen.getByRole("button", { name: "Generate transcript" }));
    await advance(0);
    for (let attempt = 0; attempt < MAX_JOB_POLL_FAILURES; attempt += 1) {
      await advance(JOB_POLL_INTERVAL_MS);
    }

    expect(screen.getByText(/could not check the progress/i)).toBeInTheDocument();
    expect(screen.queryByText("The transcript is ready.")).not.toBeInTheDocument();
    await advance(JOB_POLL_INTERVAL_MS * 5);
    expect(callsTo("GET", JOB_PATH)).toHaveLength(MAX_JOB_POLL_FAILURES);
  });

  it("pauses job polling while the page is hidden", async () => {
    vi.useFakeTimers();
    backend.jobs = [makeJob("running")];
    render(<VideoDetail videoId={VIDEO_ID} />);
    await advance(0);
    fireEvent.click(screen.getByRole("button", { name: "Generate transcript" }));
    await advance(0);

    visibility = "hidden";
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await advance(JOB_POLL_INTERVAL_MS * 3);
    expect(callsTo("GET", JOB_PATH)).toHaveLength(0);

    visibility = "visible";
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await advance(0);
    expect(callsTo("GET", JOB_PATH)).toHaveLength(1);
  });

  it("stops job polling when the page is left", async () => {
    vi.useFakeTimers();
    backend.jobs = [makeJob("running")];
    const { unmount } = render(<VideoDetail videoId={VIDEO_ID} />);
    await advance(0);
    fireEvent.click(screen.getByRole("button", { name: "Generate transcript" }));
    await advance(0);
    await advance(JOB_POLL_INTERVAL_MS);
    expect(callsTo("GET", JOB_PATH)).toHaveLength(1);

    unmount();
    await advance(JOB_POLL_INTERVAL_MS * 5);
    expect(callsTo("GET", JOB_PATH)).toHaveLength(1);
  });
});

describe("VideoDetail clips", () => {
  it("disables Find clips without a transcript", async () => {
    render(<VideoDetail videoId={VIDEO_ID} />);

    expect(await screen.findByText("This video has no transcript yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find clips" })).toBeDisabled();
    expect(screen.getByText("Generate a transcript first to find clips.")).toBeInTheDocument();
  });

  it("lists suggestions with time range, duration, title, reason, score and status", async () => {
    backend.transcript = makeTranscript([segment(0, 0, 5, "Hello")]);
    backend.clips = [makeClip()];

    render(<VideoDetail videoId={VIDEO_ID} />);

    const item = await screen.findByTestId("clip-clip-1");
    expect(within(item).getByRole("heading", { name: "Big idea" })).toBeInTheDocument();
    expect(within(item).getByText("0:10 – 0:40")).toBeInTheDocument();
    expect(within(item).getByText("30 s")).toBeInTheDocument();
    expect(within(item).getByText("82%")).toBeInTheDocument();
    expect(within(item).getByText("Dense speech with a question.")).toBeInTheDocument();
    expect(within(item).getByText("Suggested")).toHaveAttribute("data-status", "suggested");
    expect(screen.getByRole("button", { name: "Find clips" })).toBeEnabled();
  });

  it("finds clips with a detection job and shows the new suggestions", async () => {
    vi.useFakeTimers();
    backend.transcript = makeTranscript([segment(0, 0, 5, "Hello")]);
    backend.jobs = [{ ...makeJob("succeeded"), job_type: "clip_detection" }];
    render(<VideoDetail videoId={VIDEO_ID} />);
    await advance(0);

    fireEvent.click(screen.getByRole("button", { name: "Find clips" }));
    await advance(0);
    expect(callsTo("POST", `${VIDEO_PATH}/clips/detect`)).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Finding clips…" })).toBeDisabled();

    backend.clips = [makeClip({ title: "Detected moment" })];
    await advance(JOB_POLL_INTERVAL_MS);

    expect(screen.getByText("Clip suggestions are ready.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Detected moment" })).toBeInTheDocument();
  });

  it("shows the error when clip detection fails", async () => {
    vi.useFakeTimers();
    backend.transcript = makeTranscript([segment(0, 0, 5, "Hello")]);
    backend.jobs = [{ ...makeJob("failed", "Clip detection returned an invalid result."), job_type: "clip_detection" }];
    render(<VideoDetail videoId={VIDEO_ID} />);
    await advance(0);

    fireEvent.click(screen.getByRole("button", { name: "Find clips" }));
    await advance(0);
    await advance(JOB_POLL_INTERVAL_MS);

    expect(screen.getByText("Clip detection failed. Clip detection returned an invalid result.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Find clips" })).toBeEnabled();
  });

  it("accepts and rejects clips through PATCH and updates the list", async () => {
    backend.clips = [makeClip()];
    backend.routes["PATCH /api/v1/clips/clip-1"] = (call) =>
      jsonResponse(makeClip({ status: (call.body as { status: Clip["status"] }).status }));
    render(<VideoDetail videoId={VIDEO_ID} />);
    const item = await screen.findByTestId("clip-clip-1");

    fireEvent.click(within(item).getByRole("button", { name: "Accept Big idea" }));

    await waitFor(() => expect(within(item).getByText("Accepted")).toHaveAttribute("data-status", "accepted"));
    expect(within(item).getByRole("button", { name: "Accept Big idea" })).toBeDisabled();
    expect(screen.getByText("Accepted Big idea.")).toBeInTheDocument();

    fireEvent.click(within(item).getByRole("button", { name: "Reject Big idea" }));

    await waitFor(() => expect(within(item).getByText("Rejected")).toHaveAttribute("data-status", "rejected"));
    expect(within(item).getByRole("button", { name: "Reject Big idea" })).toBeDisabled();
    expect(within(item).getByRole("button", { name: "Accept Big idea" })).toBeEnabled();
    expect(callsTo("PATCH", "/api/v1/clips/clip-1").map((call) => call.body)).toEqual([
      { status: "accepted" },
      { status: "rejected" },
    ]);
  });

  it("disables the clip's actions while a change is pending", async () => {
    backend.clips = [makeClip()];
    const pending: { release?: () => void } = {};
    backend.routes["PATCH /api/v1/clips/clip-1"] = () =>
      new Promise<FakeResponse>((resolve) => {
        pending.release = () => resolve(jsonResponse(makeClip({ status: "accepted" })));
      });
    render(<VideoDetail videoId={VIDEO_ID} />);
    const item = await screen.findByTestId("clip-clip-1");

    fireEvent.click(within(item).getByRole("button", { name: "Accept Big idea" }));

    await waitFor(() => expect(within(item).getByRole("button", { name: "Reject Big idea" })).toBeDisabled());
    fireEvent.click(within(item).getByRole("button", { name: "Accept Big idea" }));
    expect(callsTo("PATCH", "/api/v1/clips/clip-1")).toHaveLength(1);

    await act(async () => {
      pending.release?.();
    });
    await waitFor(() => expect(within(item).getByRole("button", { name: "Reject Big idea" })).toBeEnabled());
  });

  it("keeps the clip unchanged and shows the error when a change is refused", async () => {
    backend.clips = [makeClip({ status: "accepted" })];
    backend.routes["PATCH /api/v1/clips/clip-1"] = () =>
      errorResponse(409, "video_not_ready", "The video must be ready before its clips can be changed.");
    render(<VideoDetail videoId={VIDEO_ID} />);
    const item = await screen.findByTestId("clip-clip-1");

    fireEvent.click(within(item).getByRole("button", { name: "Reject Big idea" }));

    expect(
      await screen.findByText("Could not update Big idea. The video must be ready before its clips can be changed."),
    ).toBeInTheDocument();
    expect(within(item).getByText("Accepted")).toBeInTheDocument();
  });

  it("previews a clip by seeking and playing the source until its end", async () => {
    backend.clips = [makeClip()];
    render(<VideoDetail videoId={VIDEO_ID} />);
    const item = await screen.findByTestId("clip-clip-1");
    const { element, media, play, pause } = mockMedia({ readyState: 0 });

    fireEvent.click(within(item).getByRole("button", { name: "Preview Big idea" }));
    // Nothing happens until the metadata is loaded.
    expect(play).not.toHaveBeenCalled();

    media.readyState = 1;
    element.dispatchEvent(new Event("loadedmetadata"));
    expect(media.currentTime).toBe(10);
    expect(play).toHaveBeenCalledTimes(1);

    media.currentTime = 25;
    element.dispatchEvent(new Event("timeupdate"));
    expect(pause).not.toHaveBeenCalled();

    media.currentTime = 40.1;
    element.dispatchEvent(new Event("timeupdate"));
    expect(pause).toHaveBeenCalledTimes(1);

    // The preview's listeners are removed once it ends.
    element.dispatchEvent(new Event("timeupdate"));
    expect(pause).toHaveBeenCalledTimes(1);
  });

  it("ends a preview when another one starts", async () => {
    backend.clips = [makeClip(), makeClip({ id: "clip-2", title: "Second", start_seconds: 50, end_seconds: 80 })];
    render(<VideoDetail videoId={VIDEO_ID} />);
    await screen.findByTestId("clip-clip-2");
    const { element, media, pause } = mockMedia();

    fireEvent.click(screen.getByRole("button", { name: "Preview Big idea" }));
    fireEvent.click(screen.getByRole("button", { name: "Preview Second" }));
    expect(media.currentTime).toBe(50);

    // Past the first clip's end, but within the second one: playback continues.
    media.currentTime = 60;
    element.dispatchEvent(new Event("timeupdate"));
    expect(pause).not.toHaveBeenCalled();
  });
});

describe("VideoDetail manual clips", () => {
  async function renderReady() {
    render(<VideoDetail videoId={VIDEO_ID} />);
    await screen.findByText("No clips yet.");
  }

  function fillForm(start: string, end: string, title = "") {
    fireEvent.change(screen.getByLabelText("Start time"), { target: { value: start } });
    fireEvent.change(screen.getByLabelText("End time"), { target: { value: end } });
    fireEvent.change(screen.getByLabelText("Title (optional)"), { target: { value: title } });
    fireEvent.click(screen.getByRole("button", { name: "Add clip" }));
  }

  it("rejects an end that is not after the start without a request", async () => {
    await renderReady();

    fillForm("1:30", "1:00");

    expect(await screen.findByText("The end must be after the start.")).toBeInTheDocument();
    expect(screen.getByLabelText("End time")).toHaveAttribute("aria-invalid", "true");
    fillForm("30", "30");
    expect(await screen.findByText("The end must be after the start.")).toBeInTheDocument();
    expect(callsTo("POST", `${VIDEO_PATH}/clips`)).toHaveLength(0);
  });

  it("rejects missing, malformed and out-of-range times", async () => {
    await renderReady();

    fillForm("", "abc");
    expect(await screen.findByText("Enter a start time.")).toBeInTheDocument();
    expect(screen.getByText("Enter a time such as 1:35 or 95.")).toBeInTheDocument();

    fillForm("1:00", "2:30");
    expect(await screen.findByText("The end must be within the video (2:00).")).toBeInTheDocument();
    expect(callsTo("POST", `${VIDEO_PATH}/clips`)).toHaveLength(0);
  });

  it("creates a manual clip and shows it", async () => {
    backend.routes[`POST ${VIDEO_PATH}/clips`] = (call) => {
      const body = call.body as { start_seconds: number; end_seconds: number; title?: string };
      return jsonResponse(
        makeClip({
          id: "clip-manual",
          start_seconds: body.start_seconds,
          end_seconds: body.end_seconds,
          title: body.title ?? "Clip at 1:05",
          reason: "Created manually.",
          score: 1,
          status: "accepted",
          source: "manual",
        }),
        201,
      );
    };
    await renderReady();

    fillForm("1:05", "95.5", "  My moment ");

    const item = await screen.findByTestId("clip-clip-manual");
    expect(callsTo("POST", `${VIDEO_PATH}/clips`)[0].body).toEqual({
      start_seconds: 65,
      end_seconds: 95.5,
      title: "My moment",
    });
    expect(within(item).getByRole("heading", { name: "My moment" })).toBeInTheDocument();
    expect(within(item).getByText("Accepted")).toBeInTheDocument();
    expect(screen.getByText("Added My moment.")).toBeInTheDocument();
    expect(screen.getByLabelText("Start time")).toHaveValue("");
  });

  it("shows the server's validation error", async () => {
    backend.routes[`POST ${VIDEO_PATH}/clips`] = () =>
      errorResponse(422, "invalid_clip_length", "A clip must be between 15 and 90 seconds long.");
    await renderReady();

    fillForm("0", "5");

    expect(
      await screen.findByText("Could not add the clip. A clip must be between 15 and 90 seconds long."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Start time")).toHaveValue("0");
  });
});
