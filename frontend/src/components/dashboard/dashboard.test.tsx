import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Dashboard } from "@/components/dashboard/dashboard";

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

const COUNTS = { uploaded: 0, processing: 1, ready: 2, failed: 1 };

function project(id: string, name: string) {
  return {
    id,
    name,
    archived: false,
    archived_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    video_count: 2,
    status_counts: { uploaded: 0, processing: 1, ready: 1, failed: 0 },
  };
}

const VIDEO = {
  id: "v1",
  user_id: "u1",
  original_filename: "keynote.mp4",
  content_type: "video/mp4",
  size_bytes: 2048,
  status: "ready",
  error_message: null,
  duration_seconds: 90,
  width: 1280,
  height: 720,
  fps: 30,
  created_at: "2026-02-01T00:00:00Z",
  updated_at: "2026-02-01T00:00:00Z",
  project: { id: "p1", name: "Podcast", archived: false },
};

interface ServerState {
  projects: ReturnType<typeof project>[];
  summaryFails: boolean;
}

let state: ServerState;

function json(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  };
}

function summary() {
  return {
    videos: { total: 4, status_counts: COUNTS },
    clips: 7,
    projects: { active: state.projects.length, archived: 0 },
    usage: { storage_bytes: 3 * 1024 * 1024, video_seconds: 7500 },
    recent_projects: state.projects.slice(0, 5),
    recent_videos: [VIDEO],
  };
}

function route(url: string, init?: RequestInit) {
  const path = url.replace(API, "");
  const method = init?.method ?? "GET";
  if (path === "/api/v1/dashboard") {
    return state.summaryFails
      ? json({ error: { code: "internal_error", message: "Internal Server Error", details: null } }, 500)
      : json(summary());
  }
  if (path.startsWith("/api/v1/projects?")) {
    return json({ items: state.projects, total: state.projects.length });
  }
  if (path === "/api/v1/projects" && method === "POST") {
    const { name } = JSON.parse(String(init?.body)) as { name: string };
    const created = project(`p${state.projects.length + 1}`, name);
    state.projects = [created, ...state.projects];
    return json(created, 201);
  }
  const projectMatch = path.match(/^\/api\/v1\/projects\/([^/?]+)$/);
  if (projectMatch && method === "PATCH") {
    const changes = JSON.parse(String(init?.body)) as { name?: string; archived?: boolean };
    const target = state.projects.find((item) => item.id === projectMatch[1]);
    if (!target) {
      return json({ error: { code: "not_found", message: "Project not found.", details: null } }, 404);
    }
    const updated = { ...target, ...changes };
    state.projects = changes.archived
      ? state.projects.filter((item) => item.id !== target.id)
      : state.projects.map((item) => (item.id === target.id ? updated : item));
    return json(updated);
  }
  if (path.startsWith("/api/v1/videos?")) {
    return json({ items: [VIDEO], total: 1 });
  }
  throw new Error(`Unexpected request ${method} ${path}`);
}

function requests(method: string, prefix: string) {
  return (fetchMock.mock.calls as [string, RequestInit][]).filter(
    ([url, init]) => (init?.method ?? "GET") === method && url.startsWith(`${API}${prefix}`),
  );
}

beforeEach(() => {
  state = { projects: [project("p1", "Podcast")], summaryFails: false };
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (url: string, init?: RequestInit) => route(url, init));
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", API);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("Dashboard", () => {
  it("shows the overview, quick-start actions, projects and videos", async () => {
    render(<Dashboard />);

    expect(await screen.findByText("Clips generated")).toBeInTheDocument();
    const overview = screen.getByRole("region", { name: "Overview" });
    expect(within(overview).getByText("Videos").nextSibling).toHaveTextContent("4");
    expect(within(overview).getByText("Processing").nextSibling).toHaveTextContent("1");
    expect(within(overview).getByText("Clips generated").nextSibling).toHaveTextContent("7");
    expect(within(overview).getByText("Storage used").nextSibling).toHaveTextContent("3 MB");
    expect(within(overview).getByText("Video uploaded").nextSibling).toHaveTextContent("2 h 5 min");
    expect(within(overview).getByText(/credits: not tracked yet/i)).toBeInTheDocument();
    const quickStart = within(overview).getByRole("navigation", { name: "Quick start" });
    expect(within(quickStart).getByRole("link", { name: "Upload a video" })).toHaveAttribute("href", "/videos/new");
    expect(within(quickStart).getByRole("link", { name: "Create a project" })).toHaveAttribute(
      "href",
      "#new-project-name",
    );
    const recentVideos = within(overview).getByRole("region", { name: "Recent videos" });
    expect(within(recentVideos).getByRole("link", { name: "keynote.mp4" })).toHaveAttribute("href", "/videos/v1");

    const projects = screen.getByRole("region", { name: "Projects" });
    expect(await within(projects).findByText("Podcast")).toBeInTheDocument();
    expect(within(projects).getByText("2 videos · 1 processing")).toBeInTheDocument();

    const videos = screen.getByRole("region", { name: "Your videos" });
    expect(await within(videos).findByRole("link", { name: "keynote.mp4" })).toBeInTheDocument();
    expect(within(videos).getByLabelText("Project for keynote.mp4")).toHaveValue("p1");
  });

  it("creates a project and refreshes the projects and summary", async () => {
    render(<Dashboard />);
    const projects = await screen.findByRole("region", { name: "Projects" });
    await within(projects).findByText("Podcast");
    const summaryRequests = requests("GET", "/api/v1/dashboard").length;

    fireEvent.click(within(projects).getByRole("button", { name: "Create project" }));
    expect(await within(projects).findByText("Enter a project name.")).toBeInTheDocument();
    expect(requests("POST", "/api/v1/projects")).toHaveLength(0);

    fireEvent.change(within(projects).getByLabelText("New project name"), { target: { value: "  Interviews " } });
    fireEvent.click(within(projects).getByRole("button", { name: "Create project" }));

    expect(await within(projects).findByText("Interviews")).toBeInTheDocument();
    expect(JSON.parse(String(requests("POST", "/api/v1/projects")[0][1].body))).toEqual({ name: "Interviews" });
    expect(within(projects).getByLabelText("New project name")).toHaveValue("");
    await waitFor(() => expect(requests("GET", "/api/v1/dashboard").length).toBeGreaterThan(summaryRequests));
  });

  it("moves focus to the new project name from the quick-start link", async () => {
    render(<Dashboard />);
    const overview = screen.getByRole("region", { name: "Overview" });
    const projects = screen.getByRole("region", { name: "Projects" });

    fireEvent.click(within(overview).getByRole("link", { name: "Create a project" }));

    expect(within(projects).getByLabelText("New project name")).toHaveFocus();
    await within(projects).findByText("Podcast");
  });

  it("renames a project", async () => {
    render(<Dashboard />);
    const projects = await screen.findByRole("region", { name: "Projects" });
    await within(projects).findByText("Podcast");

    fireEvent.click(within(projects).getByRole("button", { name: "Rename Podcast" }));
    fireEvent.change(within(projects).getByLabelText("New name for Podcast"), { target: { value: "Shows" } });
    fireEvent.click(within(projects).getByRole("button", { name: "Save name" }));

    expect(await within(projects).findByText("Shows")).toBeInTheDocument();
    const [, init] = requests("PATCH", "/api/v1/projects/p1")[0];
    expect(JSON.parse(String(init.body))).toEqual({ name: "Shows" });
  });

  it("archives a project after confirmation", async () => {
    render(<Dashboard />);
    const projects = await screen.findByRole("region", { name: "Projects" });
    await within(projects).findByText("Podcast");

    fireEvent.click(within(projects).getByRole("button", { name: "Archive Podcast" }));
    const dialog = screen.getByRole("alertdialog", { name: "Archive Podcast?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Archive project" }));

    await waitFor(() => expect(within(projects).queryByText("Podcast")).not.toBeInTheDocument());
    const [, init] = requests("PATCH", "/api/v1/projects/p1")[0];
    expect(JSON.parse(String(init.body))).toEqual({ archived: true });
  });

  it("filters the video list by a selected project", async () => {
    render(<Dashboard />);
    const projects = await screen.findByRole("region", { name: "Projects" });
    await within(projects).findByText("Podcast");

    fireEvent.click(within(projects).getByRole("button", { name: "Show videos in Podcast" }));

    await waitFor(() =>
      expect(requests("GET", "/api/v1/videos?").at(-1)?.[0]).toBe(
        `${API}/api/v1/videos?limit=20&offset=0&project_id=p1`,
      ),
    );
    expect(within(projects).getByRole("button", { name: "Show videos in Podcast" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows a summary error with a retry", async () => {
    state.summaryFails = true;
    render(<Dashboard />);

    expect(await screen.findByText(/could not load your summary/i)).toBeInTheDocument();
    state.summaryFails = false;
    const overview = screen.getByRole("region", { name: "Overview" });
    fireEvent.click(within(overview).getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Clips generated")).toBeInTheDocument();
    expect(screen.queryByText(/could not load your summary/i)).not.toBeInTheDocument();
  });
});
