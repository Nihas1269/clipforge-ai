"use client";

import Link from "next/link";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/button";
import { ConfirmDialog } from "@/components/dashboard/confirm-dialog";
import { STATUS_LABELS, StatusBadge } from "@/components/dashboard/status-badge";
import { getErrorMessage } from "@/lib/api/client";
import type { Project } from "@/lib/api/projects";
import {
  ACTIVE_VIDEO_STATUSES,
  DEFAULT_VIDEO_PAGE_SIZE,
  VIDEO_STATUSES,
  deleteVideo,
  listVideos,
  moveVideoToProject,
  type ListVideosParams,
  type Video,
  type VideoList as VideoPage,
  type VideoStatus,
} from "@/lib/api/videos";
import { formatBytes, formatDate, formatDuration } from "@/lib/format";

/** How often the list is refreshed while a listed video is still being processed. */
export const POLL_INTERVAL_MS = 4_000;

/** `"all"`, `"unassigned"`, or a project id. */
export type ProjectFilter = string;

export interface VideoListProps {
  /** The user's active projects, for the project filter and "move to project". */
  projects: readonly Project[];
  projectFilter: ProjectFilter;
  onProjectFilterChange: (filter: ProjectFilter) => void;
  /** Called after a video was deleted, moved, or changed status. */
  onVideosChanged?: () => void;
  /** Changing it reloads the current page, e.g. after a project was renamed. */
  refreshRequest?: number;
  pageSize?: number;
}

type Outcome = { key: string; list: VideoPage } | { key: string; error: string };

function isActive(video: Video): boolean {
  return ACTIVE_VIDEO_STATUSES.includes(video.status);
}

function statusSignature(list: VideoPage): string {
  return list.items.map((video) => `${video.id}:${video.status}`).join(",");
}

export function videoDetailPath(id: string): string {
  return `/videos/${encodeURIComponent(id)}`;
}

/**
 * The user's videos, a page at a time, with status and project filters.
 *
 * While any listed video is `uploaded` or `processing`, the current page (with
 * the same filters) is refreshed every `POLL_INTERVAL_MS`; polling pauses
 * while the page is hidden, never overlaps a request still in flight, and
 * stops once every listed video is `ready` or `failed` and on unmount.
 */
export function VideoList({
  projects,
  projectFilter,
  onProjectFilterChange,
  onVideosChanged,
  refreshRequest = 0,
  pageSize = DEFAULT_VIDEO_PAGE_SIZE,
}: VideoListProps) {
  const headingId = useId();
  const statusFilterId = useId();
  const projectFilterId = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);

  const [statusFilter, setStatusFilter] = useState<VideoStatus | "">("");
  // The offset belongs to the filters it was chosen with; other filters start on page one.
  const [page, setPage] = useState({ filter: `${projectFilter}|`, offset: 0 });
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [current, setCurrent] = useState<{ key: string; list: VideoPage } | null>(null);
  const [reloadCount, setReloadCount] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Video | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [movingId, setMovingId] = useState<string | null>(null);
  const [focusHeading, setFocusHeading] = useState(0);

  const requestSeq = useRef(0);
  const inFlight = useRef(false);
  const lastSignature = useRef<string | null>(null);
  const changedRef = useRef(onVideosChanged);

  useEffect(() => {
    changedRef.current = onVideosChanged;
  }, [onVideosChanged]);

  const filterKey = `${projectFilter}|${statusFilter}`;
  const offset = page.filter === filterKey ? page.offset : 0;
  const queryKey = `${filterKey}|${offset}|${pageSize}`;

  const params = useMemo<ListVideosParams>(
    () => ({
      limit: pageSize,
      offset,
      status: statusFilter || undefined,
      projectId:
        projectFilter !== "all" && projectFilter !== "unassigned" ? projectFilter : undefined,
      unassigned: projectFilter === "unassigned",
    }),
    [offset, pageSize, projectFilter, statusFilter],
  );

  /** Fetches the current page; a response for an outdated query is dropped. */
  const fetchPage = useCallback(async (): Promise<Outcome | null> => {
    requestSeq.current += 1;
    const seq = requestSeq.current;
    inFlight.current = true;
    try {
      const list = await listVideos(params);
      return seq === requestSeq.current ? { key: queryKey, list } : null;
    } catch (caught) {
      return seq === requestSeq.current ? { key: queryKey, error: getErrorMessage(caught) } : null;
    } finally {
      if (seq === requestSeq.current) {
        inFlight.current = false;
      }
    }
  }, [params, queryKey]);

  const apply = useCallback((result: Outcome | null) => {
    if (!result) {
      return;
    }
    setOutcome(result);
    if ("list" in result) {
      setCurrent(result);
      const signature = statusSignature(result.list);
      if (lastSignature.current !== null && lastSignature.current !== signature) {
        changedRef.current?.();
      }
      lastSignature.current = signature;
    }
  }, []);

  const refresh = useCallback(() => fetchPage().then(apply), [apply, fetchPage]);

  useEffect(() => {
    void fetchPage().then(apply);
  }, [apply, fetchPage, reloadCount, refreshRequest]);

  const loaded = current !== null && current.key === queryKey ? current.list : null;
  const failure = outcome !== null && outcome.key === queryKey && "error" in outcome ? outcome.error : null;
  const hasActive = loaded?.items.some(isActive) ?? false;

  useEffect(() => {
    if (!hasActive) {
      return;
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;

    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
    }

    async function tick() {
      if (stopped || document.visibilityState === "hidden") {
        // Resumed by the visibilitychange handler.
        return;
      }
      if (!inFlight.current) {
        await refresh();
      }
      if (!stopped) {
        schedule();
      }
    }

    function handleVisibilityChange() {
      if (document.visibilityState === "hidden") {
        clearTimeout(timer);
      } else {
        void tick();
      }
    }

    schedule();
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [hasActive, refresh]);

  useEffect(() => {
    if (focusHeading > 0) {
      headingRef.current?.focus();
    }
  }, [focusHeading]);

  function goToOffset(next: number) {
    setPage({ filter: filterKey, offset: Math.max(0, next) });
  }

  function retry() {
    setOutcome(null);
    setReloadCount((count) => count + 1);
  }

  async function confirmDelete() {
    const video = pendingDelete;
    if (!video || deleting) {
      return;
    }
    setDeleting(true);
    setActionError(null);
    setNotice(null);
    try {
      await deleteVideo(video.id);
      // Removed only once the backend confirmed the deletion.
      setCurrent((previous) =>
        previous
          ? {
              key: previous.key,
              list: {
                items: previous.list.items.filter((item) => item.id !== video.id),
                total: Math.max(0, previous.list.total - 1),
              },
            }
          : previous,
      );
      setNotice(`Deleted ${video.original_filename}.`);
      setFocusHeading((count) => count + 1);
      // Reported here; the refresh below must not report it again.
      lastSignature.current = null;
      changedRef.current?.();
      if (loaded && loaded.items.length === 1 && offset > 0) {
        goToOffset(offset - pageSize);
        return;
      }
    } catch (caught) {
      setActionError(`Could not delete ${video.original_filename}. ${getErrorMessage(caught)}`);
    } finally {
      setDeleting(false);
      setPendingDelete(null);
    }
    // Either way, show what the backend now has.
    void refresh();
  }

  async function moveVideo(video: Video, projectId: string) {
    setMovingId(video.id);
    setActionError(null);
    setNotice(null);
    try {
      const updated = await moveVideoToProject(video.id, projectId || null);
      setCurrent((previous) =>
        previous
          ? {
              key: previous.key,
              list: {
                ...previous.list,
                items: previous.list.items.map((item) => (item.id === updated.id ? updated : item)),
              },
            }
          : previous,
      );
      setNotice(
        updated.project
          ? `Moved ${video.original_filename} to ${updated.project.name}.`
          : `${video.original_filename} is now unassigned.`,
      );
      lastSignature.current = null;
      changedRef.current?.();
    } catch (caught) {
      setActionError(`Could not move ${video.original_filename}. ${getErrorMessage(caught)}`);
    } finally {
      setMovingId(null);
    }
    void refresh();
  }

  const filtered = statusFilter !== "" || projectFilter !== "all";
  const projectIds = new Set(projects.map((project) => project.id));

  let body: ReactNode;
  if (loaded) {
    body =
      loaded.items.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border px-6 py-10 text-center">
          <p className="text-base font-medium text-fg">
            {filtered ? "No videos match these filters." : "You have no videos yet."}
          </p>
          <p className="mt-3">
            <Link
              href="/videos/new"
              className="text-sm font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            >
              {filtered ? "Upload a video" : "Upload your first video"}
            </Link>
          </p>
        </div>
      ) : (
        <ul className="flex flex-col gap-3">
          {loaded.items.map((video) => {
            const selectId = `${projectFilterId}-${video.id}`;
            const projectValue = video.project?.id ?? "";
            return (
              <li
                key={video.id}
                className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-4 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="flex min-w-0 flex-col gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Link
                      href={videoDetailPath(video.id)}
                      className="truncate text-base font-medium text-fg underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                    >
                      {video.original_filename}
                    </Link>
                    <StatusBadge status={video.status} />
                  </div>
                  <dl className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-fg-muted">
                    <div className="flex gap-1">
                      <dt>Upload date</dt>
                      <dd>
                        <time dateTime={video.created_at}>{formatDate(video.created_at)}</time>
                      </dd>
                    </div>
                    <div className="flex gap-1">
                      <dt>Duration</dt>
                      <dd>{formatDuration(video.duration_seconds)}</dd>
                    </div>
                    <div className="flex gap-1">
                      <dt>Size</dt>
                      <dd>{formatBytes(video.size_bytes)}</dd>
                    </div>
                  </dl>
                  {video.status === "failed" && video.error_message ? (
                    <p className="text-sm text-fg">{video.error_message}</p>
                  ) : null}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <label htmlFor={selectId} className="sr-only">
                    Project for {video.original_filename}
                  </label>
                  <select
                    id={selectId}
                    value={projectValue}
                    disabled={movingId === video.id}
                    onChange={(event) => void moveVideo(video, event.target.value)}
                    className="rounded-md border border-border bg-surface-raised px-2 py-2 text-sm text-fg focus-visible:outline-2 focus-visible:outline-accent"
                  >
                    <option value="">Unassigned</option>
                    {projects.map((project) => (
                      <option key={project.id} value={project.id}>
                        {project.name}
                      </option>
                    ))}
                    {video.project && !projectIds.has(video.project.id) ? (
                      <option value={video.project.id} disabled>
                        {video.project.archived ? `${video.project.name} (archived)` : video.project.name}
                      </option>
                    ) : null}
                  </select>
                  <Button
                    variant="secondary"
                    onClick={() => setPendingDelete(video)}
                    aria-label={`Delete ${video.original_filename}`}
                  >
                    Delete
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      );
  } else if (failure) {
    body = (
      <div className="flex flex-col items-start gap-3 rounded-lg border border-danger px-4 py-4">
        <p role="alert" className="text-sm text-fg">
          Could not load your videos. {failure}
        </p>
        <Button variant="secondary" onClick={retry}>
          Try again
        </Button>
      </div>
    );
  } else {
    body = (
      <p role="status" className="text-sm text-fg-muted">
        Loading videos…
      </p>
    );
  }

  const total = loaded?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const pageNumber = Math.floor(offset / pageSize) + 1;

  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <h2 id={headingId} ref={headingRef} tabIndex={-1} className="text-xl font-semibold text-fg">
          Your videos
        </h2>
        <div className="flex flex-wrap gap-3">
          <div className="flex flex-col gap-1">
            <label htmlFor={statusFilterId} className="text-xs font-medium text-fg-muted">
              Status
            </label>
            <select
              id={statusFilterId}
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value as VideoStatus | "")}
              className="rounded-md border border-border bg-surface-raised px-2 py-2 text-sm text-fg focus-visible:outline-2 focus-visible:outline-accent"
            >
              <option value="">All statuses</option>
              {VIDEO_STATUSES.map((status) => (
                <option key={status} value={status}>
                  {STATUS_LABELS[status]}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor={projectFilterId} className="text-xs font-medium text-fg-muted">
              Project
            </label>
            <select
              id={projectFilterId}
              value={projectFilter}
              onChange={(event) => onProjectFilterChange(event.target.value)}
              className="rounded-md border border-border bg-surface-raised px-2 py-2 text-sm text-fg focus-visible:outline-2 focus-visible:outline-accent"
            >
              <option value="all">All projects</option>
              <option value="unassigned">Unassigned</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
              {projectFilter !== "all" && projectFilter !== "unassigned" && !projectIds.has(projectFilter) ? (
                <option value={projectFilter}>Selected project</option>
              ) : null}
            </select>
          </div>
        </div>
      </div>

      <div role="alert">
        {actionError ? (
          <p className="rounded-md border border-danger px-3 py-2 text-sm text-fg">{actionError}</p>
        ) : null}
      </div>
      <div role="status">{notice ? <p className="text-sm text-fg-muted">{notice}</p> : null}</div>
      {loaded && failure ? (
        <p className="text-sm text-fg-muted">Could not refresh the list. Showing the last loaded videos.</p>
      ) : null}

      {body}

      {loaded && total > pageSize ? (
        <nav aria-label="Video pages" className="flex items-center justify-between gap-4">
          <Button variant="secondary" onClick={() => goToOffset(offset - pageSize)} disabled={offset === 0}>
            Previous page
          </Button>
          <p className="text-sm text-fg-muted">
            Page {pageNumber} of {pageCount}
          </p>
          <Button
            variant="secondary"
            onClick={() => goToOffset(offset + pageSize)}
            disabled={offset + pageSize >= total}
          >
            Next page
          </Button>
        </nav>
      ) : null}

      {pendingDelete ? (
        <ConfirmDialog
          title={`Delete ${pendingDelete.original_filename}?`}
          confirmLabel="Delete video"
          busyLabel="Deleting…"
          busy={deleting}
          onConfirm={() => void confirmDelete()}
          onCancel={() => setPendingDelete(null)}
        >
          This permanently deletes the video, its transcript and its clips. This cannot be undone.
        </ConfirmDialog>
      ) : null}
    </section>
  );
}
