import { describe, expect, it } from "vitest";

import { formatSeconds, formatTimestamp, parseTimestamp } from "@/lib/timestamps";

describe("formatTimestamp", () => {
  it("formats minutes and hours", () => {
    expect(formatTimestamp(0)).toBe("0:00");
    expect(formatTimestamp(65.9)).toBe("1:05");
    expect(formatTimestamp(3723)).toBe("1:02:03");
  });

  it("marks missing or invalid times", () => {
    expect(formatTimestamp(null)).toBe("--:--");
    expect(formatTimestamp(Number.NaN)).toBe("--:--");
    expect(formatTimestamp(-1)).toBe("--:--");
  });
});

describe("formatSeconds", () => {
  it("rounds to one decimal", () => {
    expect(formatSeconds(30)).toBe("30 s");
    expect(formatSeconds(12.345)).toBe("12.3 s");
  });
});

describe("parseTimestamp", () => {
  it("accepts seconds, m:ss and h:mm:ss", () => {
    expect(parseTimestamp("90")).toBe(90);
    expect(parseTimestamp(" 90.5 ")).toBe(90.5);
    expect(parseTimestamp("1:30")).toBe(90);
    expect(parseTimestamp("1:30.25")).toBe(90.25);
    expect(parseTimestamp("1:02:03")).toBe(3723);
    expect(parseTimestamp("0")).toBe(0);
  });

  it("rejects blank and malformed input", () => {
    for (const input of ["", "  ", "abc", "-5", "1:60", "1:75:00", "1::2", "1:2:3:4", "1e3", "1.5:00", ":30"]) {
      expect(parseTimestamp(input)).toBeNull();
    }
  });
});
