import { ApiError, GENERIC_ERROR_MESSAGE, apiRequest, getApiBaseUrl } from "@/lib/api/client";
import { toQueuedJob, type QueuedJob } from "@/lib/api/jobs";
import { VIDEOS_PATH } from "@/lib/api/videos";

export type SubtitleFormat = "srt" | "vtt";

/** One transcript segment. Times are seconds of source-video time. */
export interface TranscriptSegment {
  id: string;
  index: number;
  /** `null` when the response had no usable time for the segment. */
  start_seconds: number | null;
  end_seconds: number | null;
  text: string;
}

/** A video's transcript (`TranscriptRead`), with its segments in order. */
export interface Transcript {
  id: string;
  video_id: string;
  language: string | null;
  requested_language: string | null;
  provider: string;
  model: string | null;
  full_text: string;
  timing_granularity: "word" | "segment";
  created_at: string;
  segments: TranscriptSegment[];
}

function videoPath(videoId: string): string {
  return `${VIDEOS_PATH}/${encodeURIComponent(videoId)}`;
}

function finiteTime(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function toSegment(value: unknown, position: number): TranscriptSegment | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const segment = value as Record<string, unknown>;
  if (typeof segment.text !== "string") {
    return null;
  }
  const index = typeof segment.index === "number" ? segment.index : position;
  return {
    id: typeof segment.id === "string" && segment.id ? segment.id : `segment-${index}`,
    index,
    start_seconds: finiteTime(segment.start_seconds),
    end_seconds: finiteTime(segment.end_seconds),
    text: segment.text,
  };
}

/** Validates the response; segments with bad times are kept without them, not dropped. */
function toTranscript(value: unknown): Transcript {
  if (typeof value !== "object" || value === null) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  const transcript = value as Record<string, unknown>;
  if (typeof transcript.id !== "string" || !Array.isArray(transcript.segments)) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  const segments: TranscriptSegment[] = [];
  for (const [position, item] of transcript.segments.entries()) {
    const segment = toSegment(item, position);
    if (!segment) {
      throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
    }
    segments.push(segment);
  }
  const text = (key: string) => (typeof transcript[key] === "string" ? (transcript[key] as string) : null);
  return {
    id: transcript.id,
    video_id: text("video_id") ?? "",
    language: text("language"),
    requested_language: text("requested_language"),
    provider: text("provider") ?? "",
    model: text("model"),
    full_text: text("full_text") ?? "",
    timing_granularity: transcript.timing_granularity === "word" ? "word" : "segment",
    created_at: text("created_at") ?? "",
    segments,
  };
}

/** The video's transcript, or `null` when it has none yet (`404 transcript_not_found`). */
export async function getTranscript(videoId: string): Promise<Transcript | null> {
  try {
    return toTranscript(await apiRequest<unknown>(`${videoPath(videoId)}/transcript`));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404 && error.code === "transcript_not_found") {
      return null;
    }
    throw error;
  }
}

/**
 * Queues transcription of the video. `language` is an ISO 639 code such as
 * `en` or `hi-IN`; omit it (or pass `""`) for automatic detection.
 */
export async function startTranscription(videoId: string, language?: string): Promise<QueuedJob> {
  const response = await apiRequest<unknown>(`${videoPath(videoId)}/transcription`, {
    method: "POST",
    body: language ? { language } : {},
  });
  return toQueuedJob(response);
}

/** The download URL of the transcript as subtitles; the session cookie authorizes it. */
export function subtitleUrl(videoId: string, format: SubtitleFormat): string {
  return `${getApiBaseUrl()}${videoPath(videoId)}/transcript.${format}`;
}
