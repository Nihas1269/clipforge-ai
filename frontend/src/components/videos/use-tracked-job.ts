"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { usePolling } from "@/components/videos/use-polling";
import { ApiError, getErrorMessage } from "@/lib/api/client";
import { getJob, isActiveJobStatus, type JobStatus, type QueuedJob } from "@/lib/api/jobs";

/** How often a queued or running job is checked. */
export const JOB_POLL_INTERVAL_MS = 2_000;

/** Polling gives up after this many failed status checks in a row. */
export const MAX_JOB_POLL_FAILURES = 3;

export interface TrackedJob {
  id: string;
  /** `"unknown"` once the job's status could no longer be read. */
  status: JobStatus | "unknown";
  /** The job's own error, or why its status could not be read. */
  errorMessage: string | null;
}

export interface UseTrackedJobResult {
  job: TrackedJob | null;
  /** True while the job is queued or running. */
  active: boolean;
  /** Starts following the job returned by an endpoint that queued it. */
  track: (queued: QueuedJob) => void;
}

function isActive(job: TrackedJob | null): boolean {
  return job !== null && job.status !== "unknown" && isActiveJobStatus(job.status);
}

/**
 * Follows one job with `GET /jobs/{id}` until it reaches a terminal status
 * (`succeeded`, `failed`, `cancelled`), then calls `onFinished` once.
 * Polling uses `usePolling`: it pauses while the page is hidden and stops on
 * unmount. A missing job (`404`), a lost session, or repeated failures stop
 * polling with status `"unknown"`; that is never reported as success.
 */
export function useTrackedJob(onFinished: (job: TrackedJob) => void): UseTrackedJobResult {
  const [job, setJob] = useState<TrackedJob | null>(null);
  const failures = useRef(0);
  const finishedRef = useRef(onFinished);
  const mounted = useRef(false);

  useEffect(() => {
    finishedRef.current = onFinished;
  }, [onFinished]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const active = isActive(job);
  const jobId = job?.id ?? null;

  const poll = useCallback(async () => {
    if (!jobId) {
      return;
    }
    let next: TrackedJob;
    try {
      const latest = await getJob(jobId);
      failures.current = 0;
      next = { id: jobId, status: latest.status, errorMessage: latest.error_message };
    } catch (error) {
      failures.current += 1;
      const fatal = error instanceof ApiError && [401, 403, 404].includes(error.status);
      if (!fatal && failures.current < MAX_JOB_POLL_FAILURES) {
        return;
      }
      next = {
        id: jobId,
        status: "unknown",
        errorMessage: `Could not check the progress. ${getErrorMessage(error)}`,
      };
    }
    if (!mounted.current) {
      return;
    }
    setJob((current) => (current?.id === jobId ? next : current));
    if (!isActive(next)) {
      finishedRef.current(next);
    }
  }, [jobId]);

  usePolling(active, poll, JOB_POLL_INTERVAL_MS);

  const track = useCallback((queued: QueuedJob) => {
    failures.current = 0;
    const next: TrackedJob = { id: queued.job_id, status: queued.status, errorMessage: null };
    setJob(next);
    if (!isActive(next)) {
      finishedRef.current(next);
    }
  }, []);

  return { job, active, track };
}
