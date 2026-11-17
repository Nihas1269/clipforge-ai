import {
  ApiError,
  GENERIC_ERROR_MESSAGE,
  apiRequest,
  getApiBaseUrl,
  isApiErrorBody,
  toApiError,
} from "@/lib/api/client";

export const VIDEOS_PATH = "/api/v1/videos";
export const UPLOAD_LIMITS_PATH = "/api/v1/videos/upload-limits";
/** The multipart field the backend reads the file from. */
export const UPLOAD_FIELD_NAME = "file";

/** The upload is aborted when no progress is reported for this long while sending. */
export const UPLOAD_STALL_TIMEOUT_MS = 60_000;
/** Once the file is sent, the backend has this long to validate, store, and respond. */
export const UPLOAD_RESPONSE_TIMEOUT_MS = 300_000;

export type VideoStatus = "uploaded" | "processing" | "ready" | "failed";

export const VIDEO_STATUSES: readonly VideoStatus[] = ["uploaded", "processing", "ready", "failed"];

/** Statuses that are still changing; the dashboard polls while a video is in one. */
export const ACTIVE_VIDEO_STATUSES: readonly VideoStatus[] = ["uploaded", "processing"];

export const DEFAULT_VIDEO_PAGE_SIZE = 20;

/** The project a video is filed under, as embedded by the backend. */
export interface VideoProjectRef {
  id: string;
  name: string;
  archived: boolean;
}

/** A video as returned by the backend (`VideoRead`). */
export interface Video {
  id: string;
  user_id: string;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  status: VideoStatus;
  error_message: string | null;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  created_at: string;
  updated_at: string;
  /** `null` while the video is in no project. */
  project?: VideoProjectRef | null;
}

/** One page of `GET /api/v1/videos`. */
export interface VideoList {
  items: Video[];
  /** All videos matching the filters, not just this page. */
  total: number;
}

export interface ListVideosParams {
  limit?: number;
  offset?: number;
  status?: VideoStatus;
  /** Only the videos in this project. */
  projectId?: string;
  /** Only videos in no project; excludes `projectId`. */
  unassigned?: boolean;
}

/** The limits `POST /api/v1/videos` enforces. */
export interface UploadLimits {
  max_upload_size_bytes: number;
  /** Lower-case, with a leading dot (".mp4"). */
  allowed_extensions: string[];
  allowed_content_types: string[];
}

export interface UploadProgress {
  /** Bytes of the request body sent so far. */
  loaded: number;
  total: number;
}

export interface UploadVideoOptions {
  onProgress?: (progress: UploadProgress) => void;
  /** Called once the whole file is sent; the backend is still validating and storing it. */
  onSent?: () => void;
  /** Aborting the signal aborts the request; the promise rejects with code `upload_cancelled`. */
  signal?: AbortSignal;
  stallTimeoutMs?: number;
  responseTimeoutMs?: number;
}

// Used when the browser reports no type for a file (common for .mkv), so the
// backend's declared content type check does not reject a valid file.
const CONTENT_TYPES_BY_EXTENSION: Record<string, string> = {
  ".mp4": "video/mp4",
  ".mov": "video/quicktime",
  ".webm": "video/webm",
  ".mkv": "video/x-matroska",
};

export function fileExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot === -1 || dot === name.length - 1 ? "" : name.slice(dot).toLowerCase();
}

function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isUploadLimits(value: unknown): value is UploadLimits {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const limits = value as Record<string, unknown>;
  return (
    typeof limits.max_upload_size_bytes === "number" &&
    Number.isFinite(limits.max_upload_size_bytes) &&
    limits.max_upload_size_bytes > 0 &&
    isStringList(limits.allowed_extensions) &&
    isStringList(limits.allowed_content_types)
  );
}

function isVideo(value: unknown): value is Video {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { id?: unknown }).id === "string" &&
    (value as { id: string }).id.length > 0
  );
}

function isVideoStatus(value: unknown): value is VideoStatus {
  return typeof value === "string" && (VIDEO_STATUSES as readonly string[]).includes(value);
}

/** The fields the dashboard relies on, so a malformed response is an error rather than a crash. */
export function isListedVideo(value: unknown): value is Video {
  if (!isVideo(value)) {
    return false;
  }
  const video = value as unknown as Record<string, unknown>;
  return (
    typeof video.original_filename === "string" &&
    typeof video.size_bytes === "number" &&
    typeof video.created_at === "string" &&
    (video.duration_seconds === null || typeof video.duration_seconds === "number") &&
    isVideoStatus(video.status)
  );
}

function isVideoList(value: unknown): value is VideoList {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const list = value as Record<string, unknown>;
  return Array.isArray(list.items) && list.items.every(isListedVideo) && typeof list.total === "number";
}

function videoPath(id: string): string {
  return `${VIDEOS_PATH}/${encodeURIComponent(id)}`;
}

/** One page of the current user's videos, newest first. */
export async function listVideos(params: ListVideosParams = {}): Promise<VideoList> {
  const query = new URLSearchParams();
  query.set("limit", String(params.limit ?? DEFAULT_VIDEO_PAGE_SIZE));
  query.set("offset", String(params.offset ?? 0));
  if (params.status) {
    query.set("status", params.status);
  }
  if (params.projectId) {
    query.set("project_id", params.projectId);
  } else if (params.unassigned) {
    query.set("unassigned", "true");
  }
  const list = await apiRequest<unknown>(`${VIDEOS_PATH}?${query.toString()}`);
  if (!isVideoList(list)) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return list;
}

/** One of the current user's videos; another user's video is `404` like a missing one. */
export async function getVideo(id: string): Promise<Video> {
  const video = await apiRequest<unknown>(videoPath(id));
  if (!isListedVideo(video)) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return video;
}

/** The URL the video element streams the source file from (`GET /videos/{id}/media`). */
export function videoMediaUrl(id: string): string {
  return `${getApiBaseUrl()}${videoPath(id)}/media`;
}

/** Deletes the video and its files. Resolves only once the backend confirms (`204`). */
export async function deleteVideo(id: string): Promise<void> {
  await apiRequest<unknown>(videoPath(id), { method: "DELETE" });
}

/** Moves the video into a project, or makes it unassigned with `null`. */
export async function moveVideoToProject(id: string, projectId: string | null): Promise<Video> {
  const video = await apiRequest<unknown>(videoPath(id), {
    method: "PATCH",
    body: { project_id: projectId },
  });
  if (!isListedVideo(video)) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return video;
}

/** The backend's upload limits, so the browser can check a file before sending it. */
export async function getUploadLimits(): Promise<UploadLimits> {
  const limits = await apiRequest<unknown>(UPLOAD_LIMITS_PATH);
  if (!isUploadLimits(limits)) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return limits;
}

function buildForm(file: File): FormData {
  const form = new FormData();
  const fallbackType = CONTENT_TYPES_BY_EXTENSION[fileExtension(file.name)];
  const part = !file.type && fallbackType ? new File([file], file.name, { type: fallbackType }) : file;
  form.append(UPLOAD_FIELD_NAME, part, file.name);
  return form;
}

function parseJson(text: string): unknown {
  if (!text) {
    return undefined;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

function responseError(status: number, payload: unknown): ApiError {
  if (status === 413 && !isApiErrorBody(payload)) {
    // For example a reverse proxy rejecting the body before it reaches the API.
    return new ApiError(413, "file_too_large", "The file is too large to upload.");
  }
  return toApiError(status, payload);
}

/**
 * Uploads `file` as `multipart/form-data` (field `file`) to `POST /api/v1/videos`
 * with the session cookie. Uses `XMLHttpRequest` because `fetch` reports no
 * upload progress. Resolves with the created video; rejects with an `ApiError`
 * for backend errors, network failures (`network_error`), stalls (`timeout`),
 * and cancellation (`upload_cancelled`). Aborting closes the connection, and
 * the backend discards a partially received upload without storing it.
 */
export function uploadVideo(file: File, options: UploadVideoOptions = {}): Promise<Video> {
  const {
    onProgress,
    onSent,
    signal,
    stallTimeoutMs = UPLOAD_STALL_TIMEOUT_MS,
    responseTimeoutMs = UPLOAD_RESPONSE_TIMEOUT_MS,
  } = options;

  return new Promise<Video>((resolve, reject) => {
    const cancelled = () => new ApiError(0, "upload_cancelled", "The upload was cancelled.");

    if (signal?.aborted) {
      reject(cancelled());
      return;
    }

    const xhr = new XMLHttpRequest();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let timedOut = false;
    let settled = false;

    const abort = () => xhr.abort();

    function startTimer(ms: number) {
      clearTimeout(timer);
      timer = setTimeout(() => {
        timedOut = true;
        xhr.abort();
      }, ms);
    }

    function settle(outcome: () => void) {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      outcome();
    }

    xhr.open("POST", `${getApiBaseUrl()}${VIDEOS_PATH}`);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Accept", "application/json");

    xhr.upload.onprogress = (event) => {
      startTimer(stallTimeoutMs);
      onProgress?.({
        loaded: event.loaded,
        total: event.lengthComputable ? event.total : file.size,
      });
    };
    xhr.upload.onload = () => {
      startTimer(responseTimeoutMs);
      onSent?.();
    };

    xhr.onload = () =>
      settle(() => {
        const payload = parseJson(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) {
          if (isVideo(payload)) {
            resolve(payload);
          } else {
            reject(new ApiError(xhr.status, "invalid_response", GENERIC_ERROR_MESSAGE));
          }
          return;
        }
        reject(responseError(xhr.status, payload));
      });
    xhr.onerror = () =>
      settle(() =>
        reject(
          new ApiError(
            0,
            "network_error",
            "Could not reach the server. Check your connection and try again.",
          ),
        ),
      );
    xhr.onabort = () =>
      settle(() =>
        reject(
          timedOut
            ? new ApiError(0, "timeout", "The upload stopped responding. Please try again.")
            : cancelled(),
        ),
      );

    signal?.addEventListener("abort", abort);
    startTimer(stallTimeoutMs);
    xhr.send(buildForm(file));
  });
}
