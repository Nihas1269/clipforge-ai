import { afterEach, describe, expect, it, vi } from "vitest";

import {
  BYTES_PER_MB,
  DEFAULT_MAX_UPLOAD_SIZE_MB,
  formatExtensions,
  formatMegabytes,
  getFallbackMaxUploadSizeMb,
  getFallbackUploadRules,
  rulesFromLimits,
  validateVideoFile,
} from "@/lib/validation/video";

function fileOfSize(name: string, size: number): File {
  const file = new File(["x"], name);
  Object.defineProperty(file, "size", { value: size });
  return file;
}

const rules = { maxBytes: 10 * BYTES_PER_MB, extensions: [".mp4", ".mov", ".webm", ".mkv"] };

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("validateVideoFile", () => {
  it.each(["clip.mp4", "clip.MOV", "clip.webm", "clip.mkv"])("accepts %s", (name) => {
    expect(validateVideoFile(fileOfSize(name, 1), rules)).toBeUndefined();
  });

  it.each(["clip.avi", "clip.mp4.exe", "clip", "clip."])("rejects %s", (name) => {
    expect(validateVideoFile(fileOfSize(name, 1), rules)).toBe(
      "This file type is not supported. Use MP4, MOV, WEBM or MKV.",
    );
  });

  it("rejects an empty file", () => {
    expect(validateVideoFile(fileOfSize("clip.mp4", 0), rules)).toBe("The file is empty.");
  });

  it("accepts a file at the limit and rejects one byte more", () => {
    expect(validateVideoFile(fileOfSize("clip.mp4", rules.maxBytes), rules)).toBeUndefined();
    expect(validateVideoFile(fileOfSize("clip.mp4", rules.maxBytes + 1), rules)).toBe(
      "The file is too large. The maximum size is 10 MB.",
    );
  });
});

describe("fallback rules", () => {
  it("reads NEXT_PUBLIC_MAX_UPLOAD_SIZE_MB", () => {
    vi.stubEnv("NEXT_PUBLIC_MAX_UPLOAD_SIZE_MB", "25");

    expect(getFallbackMaxUploadSizeMb()).toBe(25);
    expect(getFallbackUploadRules().maxBytes).toBe(25 * BYTES_PER_MB);
  });

  it.each(["", "0", "-5", "1.5", "abc", "99999999999999"])(
    "uses the default for %j",
    (value) => {
      vi.stubEnv("NEXT_PUBLIC_MAX_UPLOAD_SIZE_MB", value);

      expect(getFallbackMaxUploadSizeMb()).toBe(DEFAULT_MAX_UPLOAD_SIZE_MB);
    },
  );
});

describe("rulesFromLimits", () => {
  it("uses the backend limits", () => {
    expect(
      rulesFromLimits({
        max_upload_size_bytes: 2048,
        allowed_extensions: [".MP4", ".webm"],
        allowed_content_types: ["video/mp4"],
      }),
    ).toEqual({ maxBytes: 2048, extensions: [".mp4", ".webm"] });
  });
});

describe("formatting", () => {
  it("formats sizes and extension lists", () => {
    expect(formatMegabytes(500 * BYTES_PER_MB)).toBe("500 MB");
    expect(formatMegabytes(1.5 * BYTES_PER_MB)).toBe("1.5 MB");
    expect(formatExtensions([".mp4"])).toBe("MP4");
    expect(formatExtensions([".mp4", ".mov"])).toBe("MP4 or MOV");
  });
});
