import { ApiError, GENERIC_ERROR_MESSAGE, apiRequest } from "@/lib/api/client";
import { toQueuedJob, type QueuedJob } from "@/lib/api/jobs";
import { VIDEOS_PATH } from "@/lib/api/videos";

export const CLIPS_PATH = "/api/v1/clips";

export type ClipStatus = "suggested" | "accepted" | "rejected";

export type ClipSource = "heuristic" | "llm" | "manual";

const CLIP_STATUSES: readonly ClipStatus[] = ["suggested", "accepted", "rejected"];

/** A clip of a video (`ClipRead`). Times are seconds of source-video time. */
export interface Clip {
  id: string;
  video_id: string;
  start_seconds: number;
  end_seconds: number;
  title: string;
  reason: string;
  /** Between 0 and 1. */
  score: number;
  status: ClipStatus;
  source: ClipSource;
  created_at: string;
  updated_at: string;
}

export interface ClipList {
  items: Clip[];
  total: number;
}

export interface CreateClipInput {
  start_seconds: number;
  end_seconds: number;
  title?: string;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function isClip(value: unknown): value is Clip {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const clip = value as Record<string, unknown>;
  return (
    typeof clip.id === "string" &&
    clip.id.length > 0 &&
    isFiniteNumber(clip.start_seconds) &&
    isFiniteNumber(clip.end_seconds) &&
    typeof clip.title === "string" &&
    typeof clip.reason === "string" &&
    isFiniteNumber(clip.score) &&
    typeof clip.status === "string" &&
    (CLIP_STATUSES as readonly string[]).includes(clip.status)
  );
}

function toClip(value: unknown): Clip {
  if (!isClip(value)) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return value;
}

function videoClipsPath(videoId: string): string {
  return `${VIDEOS_PATH}/${encodeURIComponent(videoId)}/clips`;
}

function clipPath(clipId: string): string {
  return `${CLIPS_PATH}/${encodeURIComponent(clipId)}`;
}

/** The video's clips, best score first. */
export async function listClips(videoId: string): Promise<ClipList> {
  const list = await apiRequest<unknown>(videoClipsPath(videoId));
  if (
    typeof list !== "object" ||
    list === null ||
    !Array.isArray((list as { items?: unknown }).items) ||
    !(list as { items: unknown[] }).items.every(isClip) ||
    typeof (list as { total?: unknown }).total !== "number"
  ) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return list as ClipList;
}

/** Queues clip detection; the video must have a transcript. */
export async function detectClips(videoId: string): Promise<QueuedJob> {
  return toQueuedJob(await apiRequest<unknown>(`${videoClipsPath(videoId)}/detect`, { method: "POST" }));
}

/** Creates an accepted manual clip. The backend checks the range and length again. */
export async function createClip(videoId: string, input: CreateClipInput): Promise<Clip> {
  const body: CreateClipInput = { start_seconds: input.start_seconds, end_seconds: input.end_seconds };
  if (input.title) {
    body.title = input.title;
  }
  return toClip(await apiRequest<unknown>(videoClipsPath(videoId), { method: "POST", body }));
}

/** Accepts or rejects a clip (`PATCH /clips/{id}` with `{status}`). */
export async function updateClipStatus(clipId: string, status: Exclude<ClipStatus, "suggested">): Promise<Clip> {
  return toClip(await apiRequest<unknown>(clipPath(clipId), { method: "PATCH", body: { status } }));
}
