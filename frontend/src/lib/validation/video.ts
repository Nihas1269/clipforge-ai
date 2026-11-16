/**
 * Client-side checks for video uploads. They only spare the user a doomed
 * upload: the backend validates every upload again (type, signature, size)
 * and stays authoritative. The rules come from
 * `GET /api/v1/videos/upload-limits`; the defaults below are used only until
 * (or if) that request fails.
 */

import { fileExtension, type UploadLimits } from "@/lib/api/videos";

export const BYTES_PER_MB = 1024 * 1024;
/** Matches the backend's default `MAX_UPLOAD_SIZE_MB`. */
export const DEFAULT_MAX_UPLOAD_SIZE_MB = 500;
/** Matches the backend's default `ALLOWED_VIDEO_EXTENSIONS`. */
export const DEFAULT_ALLOWED_VIDEO_EXTENSIONS: readonly string[] = [".mp4", ".mov", ".webm", ".mkv"];

export interface VideoUploadRules {
  maxBytes: number;
  /** Lower-case, with a leading dot. */
  extensions: readonly string[];
}

/**
 * The fallback size limit: `NEXT_PUBLIC_MAX_UPLOAD_SIZE_MB` (a positive whole
 * number of MiB, set to the backend's `MAX_UPLOAD_SIZE_MB`), else 500.
 */
export function getFallbackMaxUploadSizeMb(): number {
  // Referenced literally so Next.js can inline the value into the bundle.
  const configured = process.env.NEXT_PUBLIC_MAX_UPLOAD_SIZE_MB?.trim() ?? "";
  if (/^\d+$/.test(configured)) {
    const value = Number(configured);
    if (value > 0 && Number.isSafeInteger(value * BYTES_PER_MB)) {
      return value;
    }
  }
  return DEFAULT_MAX_UPLOAD_SIZE_MB;
}

export function getFallbackUploadRules(): VideoUploadRules {
  return {
    maxBytes: getFallbackMaxUploadSizeMb() * BYTES_PER_MB,
    extensions: DEFAULT_ALLOWED_VIDEO_EXTENSIONS,
  };
}

export function rulesFromLimits(limits: UploadLimits): VideoUploadRules {
  return {
    maxBytes: limits.max_upload_size_bytes,
    extensions: limits.allowed_extensions.map((ext) => ext.toLowerCase()),
  };
}

/** "500 MB", "1.5 MB" (MiB, like the backend's messages). */
export function formatMegabytes(bytes: number): string {
  const megabytes = Math.round((bytes / BYTES_PER_MB) * 10) / 10;
  return `${megabytes} MB`;
}

/** "MP4, MOV, WEBM or MKV". */
export function formatExtensions(extensions: readonly string[]): string {
  const names = extensions.map((ext) => ext.replace(/^\./, "").toUpperCase());
  if (names.length <= 1) {
    return names.join("");
  }
  return `${names.slice(0, -1).join(", ")} or ${names[names.length - 1]}`;
}

/** Returns an error message, or `undefined` when the file may be uploaded. */
export function validateVideoFile(file: File, rules: VideoUploadRules): string | undefined {
  if (!rules.extensions.includes(fileExtension(file.name))) {
    return rules.extensions.length
      ? `This file type is not supported. Use ${formatExtensions(rules.extensions)}.`
      : "This file type is not supported.";
  }
  if (file.size === 0) {
    return "The file is empty.";
  }
  if (file.size > rules.maxBytes) {
    return `The file is too large. The maximum size is ${formatMegabytes(rules.maxBytes)}.`;
  }
  return undefined;
}
