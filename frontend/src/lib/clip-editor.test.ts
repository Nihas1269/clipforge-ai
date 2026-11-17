import { describe, expect, it } from "vitest";

import {
  MAX_CAPTIONS,
  boundariesChanged,
  captionErrorIndex,
  captionsAt,
  cleanCaptionLines,
  draftsSignature,
  nextCaptionTimes,
  nudgeTime,
  renderedCaptionLines,
  toCentiseconds,
  toClipTime,
  validateBoundaries,
  validateCaptionDrafts,
  wrapCaptionText,
  type CaptionDraft,
} from "@/lib/clip-editor";

const LIMITS = { min_seconds: 15, max_seconds: 90 };

function caption(start: number, end: number, text: string) {
  return { start_seconds: start, end_seconds: end, text };
}

function draft(start: string, end: string, text: string, key = `${start}-${end}`): CaptionDraft {
  return { key, start, end, text };
}

describe("captionsAt (overlay timing, as in the ASS output)", () => {
  const captions = [caption(0, 1.5, "first"), caption(1.5, 3, "second"), caption(4, 5, "third")];

  it("shows the caption whose range contains the clip time", () => {
    expect(captionsAt(captions, 0).map((c) => c.text)).toEqual(["first"]);
    expect(captionsAt(captions, 1.49).map((c) => c.text)).toEqual(["first"]);
    expect(captionsAt(captions, 2).map((c) => c.text)).toEqual(["second"]);
    expect(captionsAt(captions, 4.999).map((c) => c.text)).toEqual(["third"]);
  });

  it("treats the end as exclusive, so touching captions never show together", () => {
    expect(captionsAt(captions, 1.5).map((c) => c.text)).toEqual(["second"]);
    expect(captionsAt(captions, 3)).toEqual([]);
    expect(captionsAt(captions, 5)).toEqual([]);
  });

  it("shows nothing in gaps, before the clip and for invalid times", () => {
    expect(captionsAt(captions, 3.5)).toEqual([]);
    expect(captionsAt(captions, -0.1)).toEqual([]);
    expect(captionsAt(captions, Number.NaN)).toEqual([]);
  });

  it("rounds times to centiseconds like the renderer", () => {
    // 1.004 rounds to 1.00 and 2.006 to 2.01.
    const rounded = [caption(1.004, 2.006, "rounded")];
    expect(captionsAt(rounded, 1.0)).toHaveLength(1);
    expect(captionsAt(rounded, 2.008)).toHaveLength(1);
    expect(captionsAt(rounded, 2.01)).toHaveLength(0);
    expect(toCentiseconds(2.006)).toBe(201);
    expect(toCentiseconds(1.004)).toBe(100);
    expect(toCentiseconds(-1)).toBe(0);
  });

  it("gives a caption shorter than a centisecond 1 cs, as the renderer does", () => {
    const tiny = [caption(1, 1.001, "tiny")];
    expect(captionsAt(tiny, 1.005)).toHaveLength(1);
    expect(captionsAt(tiny, 1.01)).toHaveLength(0);
  });

  it("skips captions without text", () => {
    expect(captionsAt([caption(0, 1, "  \n ")], 0.5)).toEqual([]);
  });

  it("uses clip time: source time minus the clip start", () => {
    expect(toClipTime(12.5, 10)).toBe(2.5);
    expect(captionsAt(captions, toClipTime(12, 10)).map((c) => c.text)).toEqual(["second"]);
  });
});

describe("caption text", () => {
  it("cleans lines like the backend", () => {
    expect(cleanCaptionLines("  hello   world \r\n\n second\tline ")).toEqual(["hello world", "second line"]);
    // Control and format characters become spaces, as in `clean_caption_lines`.
    expect(cleanCaptionLines("one\u0007two​three")).toEqual(["one two three"]);
  });

  it("wraps at word boundaries and breaks long words", () => {
    expect(wrapCaptionText("the quick brown fox jumps", 10)).toEqual(["the quick", "brown fox", "jumps"]);
    expect(wrapCaptionText("abcdefghijkl", 5)).toEqual(["abcde", "fghij", "kl"]);
    expect(wrapCaptionText("first\nsecond", 42)).toEqual(["first", "second"]);
  });

  it("upper-cases when the style asks for it", () => {
    expect(renderedCaptionLines("Hello there", 42, true)).toEqual(["HELLO THERE"]);
  });
});

describe("boundaries", () => {
  it("nudges within the video and rounds to milliseconds", () => {
    expect(nudgeTime(10, 0.5, 120)).toBe(10.5);
    expect(nudgeTime(0.3, -0.5, 120)).toBe(0);
    expect(nudgeTime(119.8, 1, 120)).toBe(120);
    expect(nudgeTime(0.1, 0.2, null)).toBe(0.3);
  });

  it("accepts a valid range", () => {
    expect(validateBoundaries("10", "40", 120, LIMITS)).toEqual({ bounds: { start: 10, end: 40 }, errors: {} });
    expect(validateBoundaries("1:00", "1:30.5", 120, LIMITS).bounds).toEqual({ start: 60, end: 90.5 });
  });

  it("rejects invalid input, order and positions outside the video", () => {
    expect(validateBoundaries("abc", "40", 120, LIMITS).errors.start).toBeDefined();
    expect(validateBoundaries("40", "40", 120, LIMITS).errors.end).toBe("The end must be after the start.");
    expect(validateBoundaries("100", "121", 120, LIMITS).errors.end).toMatch(/within the video/);
    expect(validateBoundaries("120", "125", 120, LIMITS).errors.start).toMatch(/before the end of the video/);
  });

  it("rejects lengths outside the limits and accepts the limits themselves", () => {
    expect(validateBoundaries("10", "20", 120, LIMITS).errors.length).toMatch(/between 15 and 90 seconds/);
    expect(validateBoundaries("10", "101", 120, LIMITS).bounds).toBeNull();
    expect(validateBoundaries("10", "25", 120, LIMITS).bounds).toEqual({ start: 10, end: 25 });
    expect(validateBoundaries("10", "100", 120, LIMITS).bounds).toEqual({ start: 10, end: 100 });
    // Without limits only the backend checks the length.
    expect(validateBoundaries("10", "20", 120, null).bounds).toEqual({ start: 10, end: 20 });
  });

  it("detects real changes only", () => {
    const saved = { start: 10, end: 40 };
    expect(boundariesChanged("10.0", "40", saved)).toBe(false);
    expect(boundariesChanged("10.5", "40", saved)).toBe(true);
    expect(boundariesChanged("", "40", saved)).toBe(true);
  });
});

describe("validateCaptionDrafts", () => {
  it("returns the payload for valid captions, cleaned and rounded", () => {
    const result = validateCaptionDrafts(
      [draft("0", "1.2346", "  Hello  "), draft("1.2346", "3", "second\nline")],
      30,
      42,
    );
    expect(result.captions).toEqual([
      { start_seconds: 0, end_seconds: 1.235, text: "Hello" },
      { start_seconds: 1.235, end_seconds: 3, text: "second\nline" },
    ]);
    expect(result.errors).toEqual([{}, {}]);
  });

  it("rejects blank text, bad times and inverted ranges", () => {
    const result = validateCaptionDrafts(
      [draft("0", "1", "   "), draft("x", "2", "a"), draft("3", "2.5", "b")],
      30,
      42,
    );
    expect(result.captions).toBeNull();
    expect(result.errors[0].text).toBe("Enter the caption text.");
    expect(result.errors[1].start).toBeDefined();
    expect(result.errors[2].end).toBe("The caption must end after it starts.");
  });

  it("rejects captions outside the clip and accepts one ending at the clip end", () => {
    expect(validateCaptionDrafts([draft("29", "31", "late")], 30, 42).errors[0].end).toMatch(/within the clip/);
    expect(validateCaptionDrafts([draft("30", "31", "after")], 30, 42).captions).toBeNull();
    expect(validateCaptionDrafts([draft("29", "30", "edge")], 30, 42).captions).toEqual([
      { start_seconds: 29, end_seconds: 30, text: "edge" },
    ]);
  });

  it("rejects overlapping or out-of-order captions but allows touching ones", () => {
    const overlapping = validateCaptionDrafts([draft("0", "2", "a"), draft("1.5", "3", "b")], 30, 42);
    expect(overlapping.errors[1].start).toMatch(/must not overlap/);
    const touching = validateCaptionDrafts([draft("0", "2", "a"), draft("2", "3", "b")], 30, 42);
    expect(touching.captions).toHaveLength(2);
  });

  it("rejects lines longer than the style allows", () => {
    const result = validateCaptionDrafts([draft("0", "2", "this line is too long")], 30, 10);
    expect(result.errors[0].text).toMatch(/at most 10 characters/);
  });

  it("accepts an empty list (removes every caption)", () => {
    expect(validateCaptionDrafts([], 30, 42)).toEqual({ captions: [], errors: [] });
  });

  it("explains a list with more captions than the backend accepts", () => {
    const drafts = (count: number) =>
      Array.from({ length: count }, (_, index) =>
        draft(String((index * 100) / 1000), String((index * 100 + 50) / 1000), "a", `k${index}`),
      );
    const atLimit = validateCaptionDrafts(drafts(MAX_CAPTIONS), 120, 42);
    expect(atLimit.captions).toHaveLength(MAX_CAPTIONS);
    expect(atLimit.listError).toBeUndefined();

    const tooMany = validateCaptionDrafts(drafts(MAX_CAPTIONS + 2), 120, 42);
    expect(tooMany.captions).toBeNull();
    expect(tooMany.listError).toBe(`A clip can have at most ${MAX_CAPTIONS} captions; remove 2.`);
  });
});

describe("caption list helpers", () => {
  it("places a new caption after the last one, inside the clip", () => {
    expect(nextCaptionTimes([], 30)).toEqual({ start: 0, end: 2 });
    expect(nextCaptionTimes([draft("0", "29", "a")], 30)).toEqual({ start: 29, end: 30 });
    expect(nextCaptionTimes([draft("0", "30", "a")], 30)).toBeNull();
  });

  it("compares drafts by what they would save", () => {
    expect(draftsSignature([draft("1.50", "2", "a", "k1")])).toBe(draftsSignature([draft("1.5", "2.0", "a", "k2")]));
    expect(draftsSignature([draft("1.5", "2", "a")])).not.toBe(draftsSignature([draft("1.5", "2", "b")]));
  });

  it("finds the caption a backend error refers to", () => {
    expect(captionErrorIndex({ index: 2, clip_duration_seconds: 30 })).toBe(2);
    expect(captionErrorIndex([{ loc: ["body", "captions", 1, "text"], msg: "Invalid value.", type: "value_error" }])).toBe(1);
    expect(captionErrorIndex(null)).toBeNull();
    expect(captionErrorIndex({ other: true })).toBeNull();
  });
});
