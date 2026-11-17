import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { POLL_INTERVAL_MS, VideoList } from "@/components/dashboard/video-list";
import type { Project } from "@/lib/api/projects";
import type { Video, VideoStatus } from "@/lib/api/videos";

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
const fetchMock = vi.fn();

let idCounter = 0;

function makeVideo(overrides: Partial<Video> = {}): Video {
  idCounter += 1;
  return {
    id: `00000000-0000-4000-8000-${String(idCounter).padStart(12, "0")}`,
    user_id: "u1",
    original_filename: `video-${idCounter}.mp4`,
    content_type: "video/mp4",
    size_bytes: 1024,
    status: "ready",
    error_message: null,
    duration_seconds: 65,
    width: 1280,
    height: 720,
    fps: 30,
    created_at: "2026-03-04T10:00:00Z",
    updated_at: "2026-03-04T10:00:00Z",
    project: null,
    ...overrides,
  };
}

function page(items: Video[], total = items.length) {
  return { items, total };
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
  };
}

type Call = { url: string; method: string; body: unknown };

function calls(): Call[] {
  return (fetchMock.mock.calls as [string, RequestInit | undefined][]).map(([url, init]) => ({
    url,
    method: init?.method ?? "GET",
    body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
  }));
}

function listCalls(): Call[] {
  return calls().filter((call) => call.method === "GET" && call.url.startsWith(`${API}/api/v1/videos?`));
}

/** Answers list requests from `pages` in order (repeating the last one). */
function serveLists(...pages: ReturnType<typeof page>[]) {
  let index = 0;
  fetchMock.mockImplementation(async () => {
    const body = pages[Math.min(index, pages.length - 1)];
    index += 1;
    return jsonResponse(body);
  });
}

function renderList(props: Partial<Parameters<typeof VideoList>[0]> = {}) {
  const onVideosChanged = vi.fn();
  const onProjectFilterChange = vi.fn();
  const view = render(
    <VideoList
      projects={[]}
      projectFilter="all"
      onProjectFilterChange={onProjectFilterChange}
      onVideosChanged={onVideosChanged}
      {...props}
    />,
  );
  return { ...view, onVideosChanged, onProjectFilterChange };
}

function rowFor(filename: string): HTMLElement {
  const row = screen.getByRole("link", { name: filename }).closest("li");
  if (!row) {
    throw new Error(`No row for ${filename}`);
  }
  return row;
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
  visibility = "visible";
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => visibility,
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("VideoList rendering", () => {
  it("lists videos with details, links and text status badges", async () => {
    const statuses: VideoStatus[] = ["uploaded", "processing", "ready", "failed"];
    const videos = statuses.map((status) =>
      makeVideo({
        status,
        original_filename: `${status}.mp4`,
        error_message: status === "failed" ? "The video is too long." : null,
        duration_seconds: status === "ready" ? 3723 : null,
        size_bytes: 1.5 * 1024 * 1024,
      }),
    );
    serveLists(page(videos));

    renderList();

    expect(await screen.findByRole("link", { name: "ready.mp4" })).toBeInTheDocument();
    for (const [index, status] of statuses.entries()) {
      const row = rowFor(`${status}.mp4`);
      const label = { uploaded: "Uploaded", processing: "Processing", ready: "Ready", failed: "Failed" }[status];
      // The badge is a text label (with a screen-reader prefix), not color alone.
      expect(within(row).getByText(label)).toHaveAttribute("data-status", status);
      expect(within(row).getByText("Status:")).toHaveClass("sr-only");
      expect(within(row).getByRole("link", { name: `${status}.mp4` })).toHaveAttribute(
        "href",
        `/videos/${videos[index].id}`,
      );
      expect(within(row).getByText("1.5 MB")).toBeInTheDocument();
      expect(row.querySelector("time")).toHaveAttribute("datetime", "2026-03-04T10:00:00Z");
    }
    expect(within(rowFor("ready.mp4")).getByText("1:02:03")).toBeInTheDocument();
    expect(within(rowFor("processing.mp4")).getByText("Unknown")).toBeInTheDocument();
    expect(within(rowFor("failed.mp4")).getByText("The video is too long.")).toBeInTheDocument();
    expect(listCalls()[0].url).toBe(`${API}/api/v1/videos?limit=20&offset=0`);
  });

  it("shows an empty state that links to the upload page", async () => {
    serveLists(page([]));

    renderList();

    expect(await screen.findByText("You have no videos yet.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Upload your first video" })).toHaveAttribute("href", "/videos/new");
  });

  it("shows an error with a retry that loads the list", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fetchMock.mockResolvedValue(jsonResponse(page([makeVideo({ original_filename: "talk.mp4" })])));

    renderList();

    expect(await screen.findByText(/could not load your videos/i)).toHaveTextContent(/could not reach the server/i);

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("link", { name: "talk.mp4" })).toBeInTheDocument();
    expect(screen.queryByText(/could not load your videos/i)).not.toBeInTheDocument();
    expect(listCalls()).toHaveLength(2);
  });

  it("treats a malformed response as an error", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [{ id: "x" }], total: 1 }));

    renderList();

    expect(await screen.findByText(/could not load your videos/i)).toBeInTheDocument();
  });

  it("sends the status and project filters and resets to the first page", async () => {
    const videos = [makeVideo(), makeVideo()];
    serveLists(page(videos, 5));
    const view = renderList({ pageSize: 2 });
    await screen.findByRole("link", { name: videos[0].original_filename });

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(listCalls().at(-1)?.url).toContain("offset=2"));
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "failed" } });
    await waitFor(() => expect(listCalls().at(-1)?.url).toBe(`${API}/api/v1/videos?limit=2&offset=0&status=failed`));

    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "unassigned" } });
    expect(view.onProjectFilterChange).toHaveBeenCalledWith("unassigned");
    view.rerender(
      <VideoList projects={[]} projectFilter="unassigned" onProjectFilterChange={vi.fn()} pageSize={2} />,
    );
    await waitFor(() =>
      expect(listCalls().at(-1)?.url).toBe(`${API}/api/v1/videos?limit=2&offset=0&status=failed&unassigned=true`),
    );
  });
});

describe("VideoList polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("polls only while a video is processing and stops once all are done", async () => {
    const processing = makeVideo({ status: "processing", original_filename: "talk.mp4" });
    const ready = { ...processing, status: "ready" as const, duration_seconds: 42 };
    serveLists(page([processing]), page([processing]), page([ready]));
    const { onVideosChanged } = renderList();

    await advance(0);
    expect(listCalls()).toHaveLength(1);
    expect(within(rowFor("talk.mp4")).getByText("Processing")).toBeInTheDocument();

    await advance(POLL_INTERVAL_MS);
    expect(listCalls()).toHaveLength(2);

    await advance(POLL_INTERVAL_MS);
    expect(listCalls()).toHaveLength(3);
    expect(within(rowFor("talk.mp4")).getByText("Ready")).toBeInTheDocument();
    expect(onVideosChanged).toHaveBeenCalledTimes(1);

    await advance(POLL_INTERVAL_MS * 5);
    expect(listCalls()).toHaveLength(3);
  });

  it("does not poll when no listed video is processing", async () => {
    serveLists(page([makeVideo({ status: "ready" }), makeVideo({ status: "failed" })]));
    renderList();

    await advance(0);
    await advance(POLL_INTERVAL_MS * 5);

    expect(listCalls()).toHaveLength(1);
  });

  it("keeps the current page and filters when it polls", async () => {
    const videos = [makeVideo({ status: "processing" }), makeVideo({ status: "uploaded" })];
    serveLists(page(videos, 5));
    renderList({ pageSize: 2, projectFilter: "p1", projects: [] });
    await advance(0);

    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "processing" } });
    await advance(0);
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    await advance(0);
    const before = listCalls().length;
    expect(screen.getByText("Page 2 of 3")).toBeInTheDocument();

    await advance(POLL_INTERVAL_MS);

    expect(listCalls()).toHaveLength(before + 1);
    expect(listCalls().at(-1)?.url).toBe(`${API}/api/v1/videos?limit=2&offset=2&status=processing&project_id=p1`);
    expect(screen.getByText("Page 2 of 3")).toBeInTheDocument();
    expect(screen.getByLabelText("Status")).toHaveValue("processing");
  });

  it("pauses while the page is hidden and resumes when it is shown", async () => {
    serveLists(page([makeVideo({ status: "processing" })]));
    renderList();
    await advance(0);

    visibility = "hidden";
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await advance(POLL_INTERVAL_MS * 3);
    expect(listCalls()).toHaveLength(1);

    visibility = "visible";
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await advance(0);
    expect(listCalls()).toHaveLength(2);

    await advance(POLL_INTERVAL_MS);
    expect(listCalls()).toHaveLength(3);
  });

  it("never sends a poll while a request is still in flight", async () => {
    const processing = page([makeVideo({ status: "processing" })]);
    const pending: { release?: () => void } = {};
    let served = 0;
    fetchMock.mockImplementation(() => {
      served += 1;
      if (served === 2) {
        // The first poll hangs until released.
        return new Promise((resolve) => {
          pending.release = () => resolve(jsonResponse(processing));
        });
      }
      return Promise.resolve(jsonResponse(processing));
    });
    renderList();
    await advance(0);

    await advance(POLL_INTERVAL_MS * 3);
    expect(listCalls()).toHaveLength(2);

    await act(async () => {
      pending.release?.();
    });
    await advance(0);
    await advance(POLL_INTERVAL_MS);
    expect(listCalls()).toHaveLength(3);
  });

  it("stops polling when unmounted", async () => {
    serveLists(page([makeVideo({ status: "processing" })]));
    const { unmount } = renderList();
    await advance(0);

    unmount();
    await advance(POLL_INTERVAL_MS * 3);

    expect(listCalls()).toHaveLength(1);
  });
});

describe("VideoList deletion", () => {
  function serveWithDelete(deleteResponse: ReturnType<typeof jsonResponse>, ...pages: ReturnType<typeof page>[]) {
    let index = 0;
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return deleteResponse;
      }
      const body = pages[Math.min(index, pages.length - 1)];
      index += 1;
      return jsonResponse(body);
    });
  }

  it("asks for confirmation, deletes through the API and removes the video", async () => {
    const first = makeVideo({ original_filename: "first.mp4" });
    const second = makeVideo({ original_filename: "second.mp4" });
    serveWithDelete(jsonResponse(undefined, 204), page([first, second]), page([second]));
    const { onVideosChanged } = renderList();
    await screen.findByRole("link", { name: "first.mp4" });

    fireEvent.click(screen.getByRole("button", { name: "Delete first.mp4" }));

    const dialog = screen.getByRole("alertdialog", { name: "Delete first.mp4?" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleDescription(/cannot be undone/i);
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete video" }));

    await waitFor(() => expect(screen.queryByRole("link", { name: "first.mp4" })).not.toBeInTheDocument());
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "second.mp4" })).toBeInTheDocument();
    expect(screen.getByText("Deleted first.mp4.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Your videos" })).toHaveFocus();
    expect(calls().filter((call) => call.method === "DELETE")).toEqual([
      { url: `${API}/api/v1/videos/${first.id}`, method: "DELETE", body: undefined },
    ]);
    expect(onVideosChanged).toHaveBeenCalled();
    // The list is refreshed from the backend afterwards.
    await waitFor(() => expect(listCalls()).toHaveLength(2));
  });

  it("keeps the video and shows a recoverable error when deletion fails", async () => {
    const video = makeVideo({ original_filename: "keep.mp4" });
    serveWithDelete(
      jsonResponse(
        { error: { code: "delete_failed", message: "The video could not be deleted.", details: null } },
        500,
      ),
      page([video]),
    );
    renderList();
    await screen.findByRole("link", { name: "keep.mp4" });

    fireEvent.click(screen.getByRole("button", { name: "Delete keep.mp4" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete video" }));

    expect(await screen.findByText("Could not delete keep.mp4. The video could not be deleted.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "keep.mp4" })).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() => expect(listCalls()).toHaveLength(2));
    expect(screen.getByRole("link", { name: "keep.mp4" })).toBeInTheDocument();
  });

  it("cancels with Escape without deleting", async () => {
    serveLists(page([makeVideo({ original_filename: "stay.mp4" })]));
    renderList();
    await screen.findByRole("link", { name: "stay.mp4" });

    fireEvent.click(screen.getByRole("button", { name: "Delete stay.mp4" }));
    fireEvent.keyDown(screen.getByRole("alertdialog"), { key: "Escape" });

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(calls().some((call) => call.method === "DELETE")).toBe(false);
    expect(screen.getByRole("link", { name: "stay.mp4" })).toBeInTheDocument();
  });

  it("keeps keyboard focus inside the dialog", async () => {
    serveLists(page([makeVideo({ original_filename: "trap.mp4" })]));
    renderList();
    await screen.findByRole("link", { name: "trap.mp4" });
    fireEvent.click(screen.getByRole("button", { name: "Delete trap.mp4" }));
    const dialog = screen.getByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Delete video" });
    const cancel = within(dialog).getByRole("button", { name: "Cancel" });

    confirm.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(cancel).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(confirm).toHaveFocus();
  });
});

describe("VideoList projects", () => {
  const podcast: Project = {
    id: "p1",
    name: "Podcast",
    archived: false,
    archived_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    video_count: 0,
    status_counts: { uploaded: 0, processing: 0, ready: 0, failed: 0 },
  };

  it("moves a video to a project through the API", async () => {
    const video = makeVideo({ original_filename: "talk.mp4" });
    const moved = { ...video, project: { id: "p1", name: "Podcast", archived: false } };
    let stored: Video = video;
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        stored = moved;
        return jsonResponse(moved);
      }
      return jsonResponse(page([stored]));
    });
    const { onVideosChanged } = renderList({ projects: [podcast] });
    await screen.findByRole("link", { name: "talk.mp4" });

    fireEvent.change(screen.getByLabelText("Project for talk.mp4"), { target: { value: "p1" } });

    expect(await screen.findByText("Moved talk.mp4 to Podcast.")).toBeInTheDocument();
    expect(calls().find((call) => call.method === "PATCH")).toEqual({
      url: `${API}/api/v1/videos/${video.id}`,
      method: "PATCH",
      body: { project_id: "p1" },
    });
    expect(screen.getByLabelText("Project for talk.mp4")).toHaveValue("p1");
    expect(onVideosChanged).toHaveBeenCalled();
  });

  it("shows an error when a move is rejected", async () => {
    const video = makeVideo({ original_filename: "talk.mp4" });
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) =>
      init?.method === "PATCH"
        ? jsonResponse(
            {
              error: {
                code: "project_archived",
                message: "Videos cannot be moved into an archived project.",
                details: null,
              },
            },
            409,
          )
        : jsonResponse(page([video])),
    );
    renderList({ projects: [podcast] });
    await screen.findByRole("link", { name: "talk.mp4" });

    fireEvent.change(screen.getByLabelText("Project for talk.mp4"), { target: { value: "p1" } });

    expect(
      await screen.findByText("Could not move talk.mp4. Videos cannot be moved into an archived project."),
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Project for talk.mp4")).toHaveValue(""));
  });
});
