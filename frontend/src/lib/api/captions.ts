import { ApiError, GENERIC_ERROR_MESSAGE, apiRequest } from "@/lib/api/client";
import { CLIPS_PATH } from "@/lib/api/clips";

export type CaptionPosition = "bottom" | "middle" | "top";

/** A clip caption (`CaptionRead`). Times are seconds **from the clip start**; lines are split by `\n`. */
export interface Caption {
  id: string;
  index: number;
  start_seconds: number;
  end_seconds: number;
  text: string;
}

export interface CaptionList {
  clip_id: string;
  items: Caption[];
  total: number;
}

/** One caption of `PUT /clips/{id}/captions` (seconds from the clip start). */
export interface CaptionInput {
  start_seconds: number;
  end_seconds: number;
  text: string;
}

/** A clip's caption style (`CaptionStyleRead`); `is_default` until one is saved. */
export interface CaptionStyle {
  preset: string;
  font_family: string;
  /** For a 1080-pixel shorter side; scaled to the output. */
  font_size: number;
  /** `#RRGGBBAA` (CSS alpha, `FF` opaque). */
  primary_color: string;
  outline_color: string;
  position: CaptionPosition;
  uppercase: boolean;
  max_chars_per_line: number;
  is_default: boolean;
}

const POSITIONS: readonly CaptionPosition[] = ["bottom", "middle", "top"];

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isCaption(value: unknown): value is Caption {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const caption = value as Record<string, unknown>;
  return (
    typeof caption.id === "string" &&
    isFiniteNumber(caption.index) &&
    isFiniteNumber(caption.start_seconds) &&
    isFiniteNumber(caption.end_seconds) &&
    typeof caption.text === "string"
  );
}

function toCaptionList(value: unknown): CaptionList {
  if (
    typeof value !== "object" ||
    value === null ||
    !Array.isArray((value as { items?: unknown }).items) ||
    !(value as { items: unknown[] }).items.every(isCaption)
  ) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  const list = value as { clip_id?: unknown; items: Caption[] };
  return {
    clip_id: typeof list.clip_id === "string" ? list.clip_id : "",
    items: list.items,
    total: list.items.length,
  };
}

function isCaptionStyle(value: unknown): value is CaptionStyle {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const style = value as Record<string, unknown>;
  return (
    typeof style.font_family === "string" &&
    isFiniteNumber(style.font_size) &&
    typeof style.primary_color === "string" &&
    typeof style.outline_color === "string" &&
    typeof style.position === "string" &&
    (POSITIONS as readonly string[]).includes(style.position) &&
    typeof style.uppercase === "boolean" &&
    isFiniteNumber(style.max_chars_per_line)
  );
}

function captionsPath(clipId: string): string {
  return `${CLIPS_PATH}/${encodeURIComponent(clipId)}/captions`;
}

/** The clip's captions in time order. */
export async function listCaptions(clipId: string): Promise<CaptionList> {
  return toCaptionList(await apiRequest<unknown>(captionsPath(clipId)));
}

/** Rebuilds the captions from the transcript. Replaces **every** caption, including edits. */
export async function generateCaptions(clipId: string): Promise<CaptionList> {
  return toCaptionList(await apiRequest<unknown>(`${captionsPath(clipId)}/generate`, { method: "POST" }));
}

/** Replaces the clip's captions. The backend checks times, order, overlap and line length (`422`). */
export async function replaceCaptions(clipId: string, captions: CaptionInput[]): Promise<CaptionList> {
  const body = {
    captions: captions.map(({ start_seconds, end_seconds, text }) => ({ start_seconds, end_seconds, text })),
  };
  return toCaptionList(await apiRequest<unknown>(captionsPath(clipId), { method: "PUT", body }));
}

/** The stored caption style, or the default one. */
export async function getCaptionStyle(clipId: string): Promise<CaptionStyle> {
  const style = await apiRequest<unknown>(`${CLIPS_PATH}/${encodeURIComponent(clipId)}/caption-style`);
  if (!isCaptionStyle(style)) {
    throw new ApiError(200, "invalid_response", GENERIC_ERROR_MESSAGE);
  }
  return style;
}
