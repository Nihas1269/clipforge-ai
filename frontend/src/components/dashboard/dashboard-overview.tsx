import Link from "next/link";
import { useId, type ReactNode } from "react";

import { Button } from "@/components/button";
import { NEW_PROJECT_INPUT_ID } from "@/components/dashboard/project-panel";
import { StatusBadge } from "@/components/dashboard/status-badge";
import { videoDetailPath } from "@/components/dashboard/video-list";
import type { DashboardSummary } from "@/lib/api/dashboard";
import { formatBytes, formatDate, formatTotalDuration } from "@/lib/format";

const linkClass =
  "text-sm font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

export interface DashboardOverviewProps {
  summary: DashboardSummary | null;
  error: string | null;
  onRetry: () => void;
  onSelectProject: (projectId: string) => void;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface p-4">
      <dt className="text-sm text-fg-muted">{label}</dt>
      <dd className="mt-1 text-2xl font-semibold text-fg">{value}</dd>
    </div>
  );
}

/** Quick-start actions, totals, usage, and the most recent projects and videos. */
export function DashboardOverview({ summary, error, onRetry, onSelectProject }: DashboardOverviewProps) {
  const headingId = useId();
  const recentProjectsId = useId();
  const recentVideosId = useId();

  let content: ReactNode;
  if (summary) {
    const counts = summary.videos.status_counts;
    content = (
      <>
        <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="Videos" value={String(summary.videos.total)} />
          <Stat label="Processing" value={String(counts.uploaded + counts.processing)} />
          <Stat label="Ready" value={String(counts.ready)} />
          <Stat label="Failed" value={String(counts.failed)} />
          <Stat label="Clips generated" value={String(summary.clips)} />
          <Stat label="Active projects" value={String(summary.projects.active)} />
          <Stat label="Storage used" value={formatBytes(summary.usage.storage_bytes)} />
          <Stat label="Video uploaded" value={formatTotalDuration(summary.usage.video_seconds)} />
        </dl>
        <p className="text-sm text-fg-muted">Credits: not tracked yet. Usage above covers all your videos.</p>

        <div className="grid gap-6 md:grid-cols-2">
          <section aria-labelledby={recentProjectsId} className="flex flex-col gap-2">
            <h3 id={recentProjectsId} className="text-base font-semibold text-fg">
              Recent projects
            </h3>
            {summary.recent_projects.length === 0 ? (
              <p className="text-sm text-fg-muted">No projects yet.</p>
            ) : (
              <ul className="flex flex-col gap-1">
                {summary.recent_projects.map((project) => (
                  <li key={project.id} className="flex items-baseline justify-between gap-2 text-sm">
                    <button
                      type="button"
                      onClick={() => onSelectProject(project.id)}
                      className="truncate text-left text-fg underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-accent"
                    >
                      {project.name}
                    </button>
                    <span className="shrink-0 text-fg-muted">
                      {project.video_count} {project.video_count === 1 ? "video" : "videos"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section aria-labelledby={recentVideosId} className="flex flex-col gap-2">
            <h3 id={recentVideosId} className="text-base font-semibold text-fg">
              Recent videos
            </h3>
            {summary.recent_videos.length === 0 ? (
              <p className="text-sm text-fg-muted">No videos yet.</p>
            ) : (
              <ul className="flex flex-col gap-1">
                {summary.recent_videos.map((video) => (
                  <li key={video.id} className="flex items-center justify-between gap-2 text-sm">
                    <Link href={videoDetailPath(video.id)} className="truncate text-fg underline-offset-4 hover:underline">
                      {video.original_filename}
                    </Link>
                    <span className="flex shrink-0 items-center gap-2">
                      <time dateTime={video.created_at} className="text-fg-muted">
                        {formatDate(video.created_at)}
                      </time>
                      <StatusBadge status={video.status} />
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </>
    );
  } else if (error) {
    content = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-sm text-fg">
          Could not load your summary. {error}
        </p>
        <Button variant="secondary" onClick={onRetry}>
          Try again
        </Button>
      </div>
    );
  } else {
    content = (
      <p role="status" className="text-sm text-fg-muted">
        Loading your summary…
      </p>
    );
  }

  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h2 id={headingId} className="text-xl font-semibold text-fg">
          Overview
        </h2>
        <nav aria-label="Quick start" className="flex flex-wrap gap-4">
          <Link href="/videos/new" className={linkClass}>
            Upload a video
          </Link>
          <a
            href={`#${NEW_PROJECT_INPUT_ID}`}
            className={linkClass}
            onClick={(event) => {
              // A fragment link scrolls but does not reliably move focus.
              const input = document.getElementById(NEW_PROJECT_INPUT_ID);
              if (input) {
                event.preventDefault();
                input.focus();
              }
            }}
          >
            Create a project
          </a>
        </nav>
      </div>
      {content}
    </section>
  );
}
