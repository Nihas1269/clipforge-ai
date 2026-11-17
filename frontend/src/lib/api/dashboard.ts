import { ApiError, GENERIC_ERROR_MESSAGE, apiRequest } from "@/lib/api/client";
import { isProject, isStatusCounts, type Project, type StatusCounts } from "@/lib/api/projects";
import { isListedVideo, type Video } from "@/lib/api/videos";

export const DASHBOARD_PATH = "/api/v1/dashboard";

/** `GET /api/v1/dashboard`: the numbers and recent items above the video list. */
export interface DashboardSummary {
  videos: { total: number; status_counts: StatusCounts };
  /** Generated clips across all of the user's videos. */
  clips: number;
  projects: { active: number; archived: number };
  /** Credits are not tracked yet, so only usage is reported. */
  usage: { storage_bytes: number; video_seconds: number };
  recent_projects: Project[];
  recent_videos: Video[];
}

function isSummary(value: unknown): value is DashboardSummary {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const summary = value as Record<string, unknown>;
  const videos = summary.videos as { total?: unknown; status_counts?: unknown } | undefined;
  const projects = summary.projects as { active?: unknown; archived?: unknown } | undefined;
  const usage = summary.usage as { storage_bytes?: unknown; video_seconds?: unknown } | undefined;
  return (
    typeof videos?.total === "number" &&
    isStatusCounts(videos.status_counts) &&
    typeof summary.clips === "number" &&
    typeof projects?.active === "number" &&
    typeof projects.archived === "number" &&
    typeof usage?.storage_bytes === "number" &&
    typeof usage.video_seconds === "number" &&
    Array.isArray(summary.recent_projects) &&
    summary.recent_projects.every(isProject) &&
    Array.isArray(summary.recent_videos) &&
    summary.recent_videos.every(isListedVideo)
  );
}

export async function getDashboardSummary(): Promise<DashboardSummary> {
  const summary = await apiRequest<unknown>(DASHBOARD_PATH);
  if (!isSummary(summary)) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return summary;
}
