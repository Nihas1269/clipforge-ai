import { ApiError, GENERIC_ERROR_MESSAGE, apiRequest } from "@/lib/api/client";
import type { VideoStatus } from "@/lib/api/videos";

export const PROJECTS_PATH = "/api/v1/projects";
/** Matches the backend's `PROJECT_NAME_MAX_LENGTH`. */
export const PROJECT_NAME_MAX_LENGTH = 100;
export const DEFAULT_PROJECT_PAGE_SIZE = 10;

export type StatusCounts = Record<VideoStatus, number>;

/** A project as returned by the backend (`ProjectRead`). */
export interface Project {
  id: string;
  name: string;
  archived: boolean;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
  video_count: number;
  status_counts: StatusCounts;
}

export interface ProjectList {
  items: Project[];
  total: number;
}

export interface ListProjectsParams {
  limit?: number;
  offset?: number;
  archived?: boolean;
}

export interface ProjectChanges {
  name?: string;
  archived?: boolean;
}

export function isStatusCounts(value: unknown): value is StatusCounts {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const counts = value as Record<string, unknown>;
  return (
    typeof counts.uploaded === "number" &&
    typeof counts.processing === "number" &&
    typeof counts.ready === "number" &&
    typeof counts.failed === "number"
  );
}

export function isProject(value: unknown): value is Project {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const project = value as Record<string, unknown>;
  return (
    typeof project.id === "string" &&
    project.id.length > 0 &&
    typeof project.name === "string" &&
    typeof project.archived === "boolean" &&
    typeof project.video_count === "number" &&
    isStatusCounts(project.status_counts)
  );
}

function projectPath(id: string): string {
  return `${PROJECTS_PATH}/${encodeURIComponent(id)}`;
}

function checkedProject(value: unknown): Project {
  if (!isProject(value)) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return value;
}

/** One page of the user's active (or archived) projects, most recently updated first. */
export async function listProjects(params: ListProjectsParams = {}): Promise<ProjectList> {
  const query = new URLSearchParams();
  query.set("limit", String(params.limit ?? DEFAULT_PROJECT_PAGE_SIZE));
  query.set("offset", String(params.offset ?? 0));
  if (params.archived) {
    query.set("archived", "true");
  }
  const list = await apiRequest<unknown>(`${PROJECTS_PATH}?${query.toString()}`);
  const items = (list as { items?: unknown } | null)?.items;
  const total = (list as { total?: unknown } | null)?.total;
  if (!Array.isArray(items) || !items.every(isProject) || typeof total !== "number") {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return { items, total };
}

export async function createProject(name: string): Promise<Project> {
  return checkedProject(await apiRequest<unknown>(PROJECTS_PATH, { method: "POST", body: { name } }));
}

/** Renames the project and/or archives (`archived: true`) or restores it. */
export async function updateProject(id: string, changes: ProjectChanges): Promise<Project> {
  return checkedProject(await apiRequest<unknown>(projectPath(id), { method: "PATCH", body: changes }));
}
