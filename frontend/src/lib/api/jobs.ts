import { ApiError, GENERIC_ERROR_MESSAGE, apiRequest } from "@/lib/api/client";

export const JOBS_PATH = "/api/v1/jobs";

export type JobType = "metadata" | "transcription" | "clip_detection" | "render";

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export const JOB_STATUSES: readonly JobStatus[] = ["queued", "running", "succeeded", "failed", "cancelled"];

/** Statuses that are still changing; a job is polled only while it is in one. */
export const ACTIVE_JOB_STATUSES: readonly JobStatus[] = ["queued", "running"];

/** A processing job as returned to its owner (`JobRead`). */
export interface Job {
  id: string;
  job_type: JobType;
  status: JobStatus;
  video_id: string | null;
  attempts: number;
  max_attempts: number;
  /** Already sanitized by the backend; set when the job failed. */
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

/** The `202` body of the endpoints that queue a job. */
export interface QueuedJob {
  job_id: string;
  status: JobStatus;
}

export function isJobStatus(value: unknown): value is JobStatus {
  return typeof value === "string" && (JOB_STATUSES as readonly string[]).includes(value);
}

export function isActiveJobStatus(status: JobStatus): boolean {
  return ACTIVE_JOB_STATUSES.includes(status);
}

function isJob(value: unknown): value is Job {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const job = value as Record<string, unknown>;
  return (
    typeof job.id === "string" &&
    job.id.length > 0 &&
    isJobStatus(job.status) &&
    (job.error_message === null || job.error_message === undefined || typeof job.error_message === "string")
  );
}

/** Checks the `202 {job_id, status}` body returned when a job is queued. */
export function toQueuedJob(value: unknown): QueuedJob {
  if (
    typeof value !== "object" ||
    value === null ||
    typeof (value as { job_id?: unknown }).job_id !== "string" ||
    !(value as { job_id: string }).job_id ||
    !isJobStatus((value as { status?: unknown }).status)
  ) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return value as QueuedJob;
}

/** The current state of one of the user's jobs. */
export async function getJob(id: string): Promise<Job> {
  const job = await apiRequest<unknown>(`${JOBS_PATH}/${encodeURIComponent(id)}`);
  if (!isJob(job)) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return { ...job, error_message: job.error_message ?? null };
}
