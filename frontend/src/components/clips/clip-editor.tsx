"use client";

import Link from "next/link";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type CSSProperties } from "react";

import { Button } from "@/components/button";
import { TextField } from "@/components/text-field";
import { useUnsavedChangesWarning } from "@/components/clips/use-unsaved-changes-warning";
import { seekTo } from "@/components/videos/player";
import { ApiError, getErrorMessage } from "@/lib/api/client";
import { getClip, getClipLimits, updateClipRange, type Clip, type ClipLimits } from "@/lib/api/clips";
import {
  generateCaptions,
  getCaptionStyle,
  listCaptions,
  replaceCaptions,
  type Caption,
  type CaptionStyle,
} from "@/lib/api/captions";
import { getVideo, videoMediaUrl, type Video } from "@/lib/api/videos";
import {
  MAX_CAPTIONS,
  NUDGE_STEPS,
  boundariesChanged,
  captionErrorIndex,
  captionsAt,
  draftsFromCaptions,
  draftsSignature,
  formatInputSeconds,
  formatPreciseSeconds,
  nextCaptionTimes,
  nudgeTime,
  renderedCaptionLines,
  toClipTime,
  validateBoundaries,
  validateCaptionDrafts,
  type CaptionDraft,
} from "@/lib/clip-editor";
import { formatSeconds, parseTimestamp } from "@/lib/timestamps";

/** Used for the overlay while the clip's caption style cannot be loaded (the backend's classic look). */
export const FALLBACK_CAPTION_STYLE: CaptionStyle = {
  preset: "classic",
  font_family: "Arial",
  font_size: 64,
  primary_color: "#FFFFFFFF",
  outline_color: "#000000FF",
  position: "bottom",
  uppercase: false,
  max_chars_per_line: 42,
  is_default: true,
};

/** A boundary at or past the clip end restarts playback from the clip start. */
const END_SLACK_SECONDS = 0.01;

const linkClasses =
  "text-sm font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

type EditorLoad =
  | { state: "loading" }
  | { state: "not_found" }
  | { state: "error"; message: string }
  | {
      state: "loaded";
      clip: Clip;
      video: Video;
      captions: Caption[];
      limits: ClipLimits | null;
      style: CaptionStyle | null;
    };

async function loadEditor(clipId: string): Promise<EditorLoad> {
  try {
    const clip = await getClip(clipId);
    const [video, captions, limits, style] = await Promise.all([
      getVideo(clip.video_id),
      listCaptions(clip.id),
      // Optional: without them only the backend checks the length / the overlay uses the default look.
      getClipLimits().catch(() => null),
      getCaptionStyle(clip.id).catch(() => null),
    ]);
    return { state: "loaded", clip, video, captions: captions.items, limits, style };
  } catch (caught) {
    if (caught instanceof ApiError && (caught.status === 404 || caught.status === 422)) {
      return { state: "not_found" };
    }
    return { state: "error", message: getErrorMessage(caught) };
  }
}

/**
 * The clip editor page: loads the clip, its video, captions, caption style
 * and the clip length limits, then shows `ClipEditorForm`.
 */
export function ClipEditor({ clipId }: { clipId: string }) {
  const [load, setLoad] = useState<EditorLoad>({ state: "loading" });
  const [reloadCount, setReloadCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    void loadEditor(clipId).then((result) => {
      if (!cancelled) {
        setLoad(result);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [clipId, reloadCount]);

  if (load.state === "loading") {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading the clip…
      </p>
    );
  }
  if (load.state === "not_found") {
    return (
      <div className="flex flex-col items-start gap-3">
        <h1 className="text-3xl font-semibold tracking-tight text-fg">Clip not found</h1>
        <p className="text-base text-fg-muted">This clip does not exist or you do not have access to it.</p>
        <Link href="/dashboard" className={linkClasses}>
          Back to the dashboard
        </Link>
      </div>
    );
  }
  if (load.state === "error") {
    return (
      <div className="flex flex-col items-start gap-3 rounded-lg border border-danger px-4 py-4">
        <p role="alert" className="text-sm text-fg">
          Could not load the clip. {load.message}
        </p>
        <Button
          variant="secondary"
          onClick={() => {
            setLoad({ state: "loading" });
            setReloadCount((count) => count + 1);
          }}
        >
          Try again
        </Button>
      </div>
    );
  }
  return (
    <ClipEditorForm
      clip={load.clip}
      video={load.video}
      captions={load.captions}
      limits={load.limits}
      captionStyle={load.style}
    />
  );
}

export interface ClipEditorFormProps {
  clip: Clip;
  video: Video;
  captions: Caption[];
  limits: ClipLimits | null;
  /** `null` when it could not be loaded; the overlay then uses FALLBACK_CAPTION_STYLE. */
  captionStyle: CaptionStyle | null;
}

type Boundary = "start" | "end";

function stepLabel(step: number): string {
  return step === 1 ? "1 second" : `${step} seconds`;
}

function overlayTextStyle(style: CaptionStyle): CSSProperties {
  const outline = style.outline_color;
  return {
    fontFamily: `"${style.font_family}", sans-serif`,
    // Font sizes are given for a 1080-pixel shorter side, like the ASS output.
    fontSize: `${(style.font_size / 1080) * 100}cqmin`,
    fontWeight: style.preset === "bold" || style.preset === "karaoke" ? 700 : 400,
    color: style.primary_color,
    textShadow: `-0.05em -0.05em 0 ${outline}, 0.05em -0.05em 0 ${outline}, -0.05em 0.05em 0 ${outline}, 0.05em 0.05em 0 ${outline}`,
    lineHeight: 1.2,
  };
}

const OVERLAY_ALIGNMENT: Record<CaptionStyle["position"], string> = {
  top: "justify-start",
  middle: "justify-center",
  bottom: "justify-end",
};

/**
 * Edits a loaded clip: the source-video range (inputs, ±0.5 s / ±1 s nudges,
 * "set to current time"), the captions (generate, edit text and times, add,
 * remove) and a preview player limited to the range with an HTML caption
 * overlay timed like the rendered ASS captions.
 *
 * Boundaries are source-video seconds; caption times are seconds from the
 * clip start (as rendered), so moving the start shifts the captions with it.
 * One "Save changes" sends `PATCH /clips/{id}` for a changed range, then
 * `PUT /clips/{id}/captions` for changed captions. "Discard changes" returns
 * to the last saved edit.
 */
export function ClipEditorForm({ clip, video, captions, limits, captionStyle }: ClipEditorFormProps) {
  const formId = useId();
  const style = captionStyle ?? FALLBACK_CAPTION_STYLE;
  const duration = video.duration_seconds;

  const [savedClip, setSavedClip] = useState(clip);
  const [savedCaptions, setSavedCaptions] = useState(captions);
  const [startInput, setStartInput] = useState(() => formatInputSeconds(clip.start_seconds));
  const [endInput, setEndInput] = useState(() => formatInputSeconds(clip.end_seconds));
  const [drafts, setDrafts] = useState<CaptionDraft[]>(() => draftsFromCaptions(captions));
  const [initialStart] = useState(clip.start_seconds);

  const [playhead, setPlayhead] = useState(clip.start_seconds);
  const [playing, setPlaying] = useState(false);
  const [loop, setLoop] = useState(false);
  const [playerError, setPlayerError] = useState<string | null>(null);

  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [boundaryError, setBoundaryError] = useState<string | null>(null);
  const [captionError, setCaptionError] = useState<{ message: string; index: number | null } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const videoRef = useRef<HTMLVideoElement>(null);
  const newCaptionCount = useRef(0);

  const savedBounds = useMemo(
    () => ({ start: savedClip.start_seconds, end: savedClip.end_seconds }),
    [savedClip],
  );
  const boundaryCheck = validateBoundaries(startInput, endInput, duration, limits);
  // The preview follows any ordered range inside the video, even one whose length cannot be saved.
  const orderedBounds = validateBoundaries(startInput, endInput, duration, null).bounds;
  const previewBounds = orderedBounds ?? savedBounds;
  const previewStart = previewBounds.start;
  const previewEnd = previewBounds.end;
  const clipLength = previewEnd - previewStart;

  const boundariesDirty = boundariesChanged(startInput, endInput, savedBounds);
  const savedSignature = useMemo(() => draftsSignature(draftsFromCaptions(savedCaptions)), [savedCaptions]);
  const captionsDirty = draftsSignature(drafts) !== savedSignature;
  const dirty = boundariesDirty || captionsDirty;
  const captionCheck = validateCaptionDrafts(drafts, clipLength, style.max_chars_per_line);

  const boundariesBlocked = boundariesDirty && boundaryCheck.bounds === null;
  // Unedited captions are checked against an edited range too: the backend
  // saves a range without looking at the captions, so a range that cuts them
  // off would leave stored captions outside the clip.
  const captionsBlocked = (captionsDirty || boundariesDirty) && captionCheck.captions === null;
  const rangeCutsCaptions = boundariesDirty && !boundariesBlocked && captionCheck.captions === null;
  const busy = saving || generating;

  useUnsavedChangesWarning(dirty);

  // Starts the preview at the clip start once the metadata is loaded.
  useEffect(() => {
    const element = videoRef.current;
    return element ? seekTo(element, initialStart) : undefined;
  }, [initialStart]);

  // Keeps playback inside the clip: it loops or stops at the end, and starts
  // from the clip start when played from outside it or from its end. While
  // paused, the user may seek anywhere to pick a new boundary.
  useEffect(() => {
    const element = videoRef.current;
    if (!element) {
      return;
    }
    const handleTime = () => {
      const time = element.currentTime;
      if (!element.paused) {
        if (time >= previewEnd) {
          if (loop) {
            element.currentTime = previewStart;
          } else {
            element.pause();
            element.currentTime = previewEnd;
          }
        } else if (time < previewStart) {
          element.currentTime = previewStart;
        }
      }
      setPlayhead(element.currentTime);
    };
    const handlePlay = () => {
      setPlaying(true);
      // Like "Play clip": playing from the clip end (e.g. with the native
      // controls after a stop) restarts the clip instead of pausing again.
      if (element.currentTime >= previewEnd - END_SLACK_SECONDS) {
        element.currentTime = previewStart;
      }
      handleTime();
    };
    const handlePause = () => setPlaying(false);
    element.addEventListener("timeupdate", handleTime);
    element.addEventListener("seeked", handleTime);
    element.addEventListener("play", handlePlay);
    element.addEventListener("pause", handlePause);
    element.addEventListener("ended", handlePause);
    return () => {
      element.removeEventListener("timeupdate", handleTime);
      element.removeEventListener("seeked", handleTime);
      element.removeEventListener("play", handlePlay);
      element.removeEventListener("pause", handlePause);
      element.removeEventListener("ended", handlePause);
    };
  }, [previewStart, previewEnd, loop]);

  const togglePlayback = useCallback(() => {
    const element = videoRef.current;
    if (!element) {
      return;
    }
    if (!element.paused) {
      element.pause();
      return;
    }
    setPlayerError(null);
    if (element.currentTime < previewStart || element.currentTime >= previewEnd - END_SLACK_SECONDS) {
      element.currentTime = previewStart;
    }
    try {
      const played = element.play() as Promise<void> | undefined;
      played?.catch(() => setPlayerError("The browser did not start playback. Try the video's own play button."));
    } catch {
      setPlayerError("The browser did not start playback. Try the video's own play button.");
    }
  }, [previewStart, previewEnd]);

  function restart() {
    const element = videoRef.current;
    if (element) {
      element.currentTime = previewStart;
      setPlayhead(previewStart);
    }
  }

  function setBoundaryInput(boundary: Boundary, value: string) {
    setNotice(null);
    setBoundaryError(null);
    if (boundary === "start") {
      setStartInput(value);
    } else {
      setEndInput(value);
    }
  }

  function nudge(boundary: Boundary, delta: number) {
    const input = boundary === "start" ? startInput : endInput;
    // An invalid input is nudged from the saved value.
    const base = parseTimestamp(input) ?? (boundary === "start" ? savedBounds.start : savedBounds.end);
    setBoundaryInput(boundary, formatInputSeconds(nudgeTime(base, delta, duration)));
  }

  function setToCurrentTime(boundary: Boundary) {
    const element = videoRef.current;
    if (!element || !Number.isFinite(element.currentTime)) {
      return;
    }
    // The player reports source-video time, the time base of the boundaries.
    setBoundaryInput(boundary, formatInputSeconds(nudgeTime(element.currentTime, 0, duration)));
  }

  function updateDraft(key: string, change: Partial<Omit<CaptionDraft, "key">>) {
    setNotice(null);
    setCaptionError(null);
    setDrafts((current) => current.map((draft) => (draft.key === key ? { ...draft, ...change } : draft)));
  }

  function addCaption() {
    if (drafts.length >= MAX_CAPTIONS) {
      setCaptionError({ message: `A clip can have at most ${MAX_CAPTIONS} captions.`, index: null });
      return;
    }
    const times = nextCaptionTimes(drafts, clipLength);
    if (!times) {
      setCaptionError({ message: "There is no room for another caption after the last one.", index: null });
      return;
    }
    newCaptionCount.current += 1;
    const key = `new-${newCaptionCount.current}`;
    setNotice(null);
    setCaptionError(null);
    setDrafts((current) => [
      ...current,
      { key, start: formatInputSeconds(times.start), end: formatInputSeconds(times.end), text: "" },
    ]);
  }

  function removeCaption(key: string) {
    setNotice(null);
    setCaptionError(null);
    setDrafts((current) => current.filter((draft) => draft.key !== key));
  }

  async function generate() {
    if (busy || boundariesDirty) {
      return;
    }
    if (
      drafts.length > 0 &&
      !window.confirm("Regenerate the captions from the transcript? This replaces every caption, including your edits.")
    ) {
      return;
    }
    setGenerating(true);
    setNotice(null);
    setCaptionError(null);
    try {
      const list = await generateCaptions(savedClip.id);
      setSavedCaptions(list.items);
      setDrafts(draftsFromCaptions(list.items));
      setNotice(
        list.items.length > 0
          ? `Generated ${list.items.length} caption${list.items.length === 1 ? "" : "s"}.`
          : "The transcript has no speech in this clip, so no captions were generated.",
      );
    } catch (caught) {
      setCaptionError({ message: `Could not generate captions. ${getErrorMessage(caught)}`, index: null });
    } finally {
      setGenerating(false);
    }
  }

  async function save() {
    if (busy || !dirty || boundariesBlocked || captionsBlocked) {
      return;
    }
    setSaving(true);
    setNotice(null);
    setBoundaryError(null);
    setCaptionError(null);
    const saved: string[] = [];
    try {
      if (boundariesDirty && boundaryCheck.bounds) {
        try {
          const updated = await updateClipRange(savedClip.id, {
            start_seconds: boundaryCheck.bounds.start,
            end_seconds: boundaryCheck.bounds.end,
          });
          setSavedClip(updated);
          setStartInput(formatInputSeconds(updated.start_seconds));
          setEndInput(formatInputSeconds(updated.end_seconds));
          saved.push("boundaries");
        } catch (caught) {
          // Captions are validated against the new length, so they are not sent either.
          setBoundaryError(`Could not save the boundaries. ${getErrorMessage(caught)}`);
          return;
        }
      }
      if (captionsDirty && captionCheck.captions) {
        try {
          const list = await replaceCaptions(savedClip.id, captionCheck.captions);
          setSavedCaptions(list.items);
          setDrafts(draftsFromCaptions(list.items));
          saved.push("captions");
        } catch (caught) {
          const index = caught instanceof ApiError ? captionErrorIndex(caught.details) : null;
          const where = index === null ? "" : `Caption ${index + 1}: `;
          setCaptionError({ message: `Could not save the captions. ${where}${getErrorMessage(caught)}`, index });
        }
      }
    } finally {
      setSaving(false);
      if (saved.length > 0) {
        setNotice(`Saved the ${saved.join(" and ")}.`);
      }
    }
  }

  function discard() {
    if (!dirty || busy || !window.confirm("Discard your unsaved changes and go back to the last saved version?")) {
      return;
    }
    setStartInput(formatInputSeconds(savedBounds.start));
    setEndInput(formatInputSeconds(savedBounds.end));
    setDrafts(draftsFromCaptions(savedCaptions));
    setBoundaryError(null);
    setCaptionError(null);
    setNotice("Restored the last saved version.");
  }

  const clipTime = toClipTime(playhead, previewStart);
  const inClip = playhead >= previewStart && playhead < previewEnd;
  const previewCaptions = inClip ? captionsAt(captionCheck.captions ?? previewableDrafts(drafts), clipTime) : [];
  const videoReady = video.status === "ready";

  return (
    <div className="flex flex-col gap-10">
      <header className="flex flex-col gap-3">
        <p>
          <Link href={`/videos/${encodeURIComponent(video.id)}`} className={linkClasses}>
            Back to the video
          </Link>
        </p>
        <h1 className="text-3xl font-semibold tracking-tight break-all text-fg">Edit clip: {savedClip.title}</h1>
        <p className="text-sm text-fg-muted">From {video.original_filename}</p>
        {!videoReady ? (
          <p role="alert" className="text-sm text-fg">
            The video is not ready, so the clip cannot be changed right now.
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-3">
          <p role="status" data-testid="save-state" className="text-sm font-medium text-fg">
            {saving ? "Saving…" : dirty ? "Unsaved changes" : "All changes saved"}
          </p>
          <Button onClick={() => void save()} disabled={busy || !dirty || boundariesBlocked || captionsBlocked}>
            {saving ? "Saving…" : "Save changes"}
          </Button>
          <Button variant="secondary" onClick={discard} disabled={busy || !dirty}>
            Discard changes
          </Button>
        </div>
        {dirty && (boundariesBlocked || captionsBlocked) ? (
          <p className="text-sm text-danger">
            Fix the highlighted problems before saving.
            {rangeCutsCaptions
              ? " The new range must still fit every caption: edit or remove the highlighted captions, or change the range back."
              : ""}
          </p>
        ) : null}
        <div role="status">{notice ? <p className="text-sm text-fg-muted">{notice}</p> : null}</div>
      </header>

      <section aria-labelledby={`${formId}-preview`} className="flex flex-col gap-3">
        <h2 id={`${formId}-preview`} className="text-xl font-semibold text-fg">
          Preview
        </h2>
        <div
          className="relative overflow-hidden rounded-lg border border-border bg-black"
          style={{ containerType: "size", aspectRatio: video.width && video.height ? `${video.width} / ${video.height}` : "16 / 9" }}
        >
          {/* The caption list below is the text alternative of the overlay. */}
          <video
            ref={videoRef}
            src={videoMediaUrl(video.id)}
            preload="metadata"
            controls
            aria-label={`Clip preview: ${savedClip.title}`}
            className="absolute inset-0 h-full w-full"
          />
          <div
            data-testid="caption-overlay"
            aria-hidden="true"
            className={`pointer-events-none absolute inset-0 flex flex-col items-center px-[5%] py-[8%] text-center ${OVERLAY_ALIGNMENT[style.position]}`}
          >
            {previewCaptions.map((caption, index) => (
              <p key={`${caption.start_seconds}-${index}`} style={overlayTextStyle(style)}>
                {renderedCaptionLines(caption.text, style.max_chars_per_line, style.uppercase).map((line, lineIndex) => (
                  <span key={lineIndex} className="block">
                    {line}
                  </span>
                ))}
              </p>
            ))}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={togglePlayback}>{playing ? "Pause" : "Play clip"}</Button>
          <Button variant="secondary" onClick={restart}>
            Restart clip
          </Button>
          <label className="flex items-center gap-2 text-sm text-fg">
            <input type="checkbox" checked={loop} onChange={(event) => setLoop(event.target.checked)} />
            Loop playback
          </label>
          <p className="text-sm text-fg-muted" data-testid="playhead">
            {inClip ? (
              <>
                Clip time {formatPreciseSeconds(clipTime)} of {formatPreciseSeconds(clipLength)} (source{" "}
                {formatPreciseSeconds(playhead)})
              </>
            ) : (
              <>Playhead outside the clip (source {formatPreciseSeconds(playhead)})</>
            )}
          </p>
        </div>
        <div role="alert">{playerError ? <p className="text-sm text-fg">{playerError}</p> : null}</div>
        <p className="text-xs text-fg-muted">
          Playback {loop ? "loops" : "stops"} at the clip end. Caption timing matches the exported captions; the
          export may be reframed, so position and size are approximate here. While paused you can seek anywhere in
          the video to pick a new start or end.
        </p>
      </section>

      <section aria-labelledby={`${formId}-bounds`} className="flex flex-col gap-4">
        <h2 id={`${formId}-bounds`} className="text-xl font-semibold text-fg">
          Clip boundaries
        </h2>
        <p className="text-sm text-fg-muted">
          Times are seconds of the source video
          {duration !== null ? ` (0 to ${formatInputSeconds(duration)} s)` : ""}.{" "}
          {limits
            ? `A clip must be ${limits.min_seconds} to ${limits.max_seconds} seconds long.`
            : "The allowed clip length could not be loaded; it is checked when you save."}{" "}
          Nudges stay within the video. Captions keep their times from the clip start, so moving the start moves
          them too; regenerate them to follow the speech again.
        </p>
        <div className="grid gap-6 sm:grid-cols-2">
          {(["start", "end"] as const).map((boundary) => {
            const name = boundary;
            const label = boundary === "start" ? "Start" : "End";
            return (
              <div key={boundary} className="flex flex-col gap-2">
                <TextField
                  id={`${formId}-${boundary}`}
                  label={`${label} (seconds)`}
                  hint="Seconds, such as 12.5, or m:ss"
                  value={boundary === "start" ? startInput : endInput}
                  onChange={(event) => setBoundaryInput(boundary, event.target.value)}
                  error={boundaryCheck.errors[boundary]}
                  inputMode="decimal"
                  autoComplete="off"
                />
                <div role="group" aria-label={`Adjust the ${name}`} className="flex flex-wrap gap-2">
                  {[...NUDGE_STEPS]
                    .reverse()
                    .map((step) => (
                      <Button
                        key={`minus-${step}`}
                        variant="secondary"
                        onClick={() => nudge(boundary, -step)}
                        aria-label={`Move the ${name} ${stepLabel(step)} earlier`}
                      >
                        −{step} s
                      </Button>
                    ))}
                  {NUDGE_STEPS.map((step) => (
                    <Button
                      key={`plus-${step}`}
                      variant="secondary"
                      onClick={() => nudge(boundary, step)}
                      aria-label={`Move the ${name} ${stepLabel(step)} later`}
                    >
                      +{step} s
                    </Button>
                  ))}
                  <Button variant="secondary" onClick={() => setToCurrentTime(boundary)}>
                    Set {name} to current time
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
        {orderedBounds ? (
          <p className="text-sm text-fg">Length: {formatSeconds(orderedBounds.end - orderedBounds.start)}</p>
        ) : null}
        <div role="alert">
          {boundaryCheck.errors.length ? <p className="text-sm text-danger">{boundaryCheck.errors.length}</p> : null}
          {boundaryError ? (
            <p className="rounded-md border border-danger px-3 py-2 text-sm text-fg">{boundaryError}</p>
          ) : null}
        </div>
      </section>

      <section aria-labelledby={`${formId}-captions`} className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <h2 id={`${formId}-captions`} className="text-xl font-semibold text-fg">
            Captions
          </h2>
          <Button variant="secondary" onClick={() => void generate()} disabled={busy || boundariesDirty || !videoReady}>
            {generating ? "Generating…" : drafts.length > 0 ? "Regenerate from transcript" : "Generate captions"}
          </Button>
        </div>
        <p className="text-sm text-fg-muted">
          Caption times are seconds from the clip start (0 to {formatInputSeconds(clipLength)} s). Captions must be
          in order and must not overlap; each line may have at most {style.max_chars_per_line} characters.
          {boundariesDirty ? " Save the new boundaries before generating captions for them." : ""}
        </p>
        <div role="alert">
          {captionCheck.listError ? <p className="text-sm text-danger">{captionCheck.listError}</p> : null}
          {captionError ? (
            <p className="rounded-md border border-danger px-3 py-2 text-sm text-fg">{captionError.message}</p>
          ) : null}
        </div>
        {drafts.length === 0 ? (
          <p className="text-sm text-fg-muted">This clip has no captions yet.</p>
        ) : (
          <ol aria-label="Caption lines" className="flex flex-col gap-3">
            {drafts.map((draft, index) => {
              const errors = captionCheck.errors[index] ?? {};
              const serverError = captionError?.index === index;
              const textId = `${formId}-caption-${draft.key}-text`;
              const number = index + 1;
              return (
                <li
                  key={draft.key}
                  data-testid={`caption-row-${index}`}
                  className={`flex flex-col gap-3 rounded-lg border bg-surface p-4 ${serverError ? "border-danger" : "border-border"}`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h3 className="text-base font-medium text-fg">Caption {number}</h3>
                    <Button
                      variant="secondary"
                      onClick={() => removeCaption(draft.key)}
                      disabled={busy}
                      aria-label={`Remove caption ${number}`}
                    >
                      Remove
                    </Button>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <TextField
                      id={`${formId}-caption-${draft.key}-start`}
                      label={`Caption ${number} start (seconds)`}
                      value={draft.start}
                      onChange={(event) => updateDraft(draft.key, { start: event.target.value })}
                      error={errors.start}
                      inputMode="decimal"
                      autoComplete="off"
                    />
                    <TextField
                      id={`${formId}-caption-${draft.key}-end`}
                      label={`Caption ${number} end (seconds)`}
                      value={draft.end}
                      onChange={(event) => updateDraft(draft.key, { end: event.target.value })}
                      error={errors.end}
                      inputMode="decimal"
                      autoComplete="off"
                    />
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <label htmlFor={textId} className="text-sm font-medium text-fg">
                      Caption {number} text
                    </label>
                    <textarea
                      id={textId}
                      value={draft.text}
                      rows={2}
                      maxLength={4000}
                      onChange={(event) => updateDraft(draft.key, { text: event.target.value })}
                      aria-invalid={errors.text || serverError ? true : undefined}
                      aria-describedby={`${textId}-error`}
                      className={
                        "rounded-md border bg-surface-raised px-3 py-2 text-sm text-fg " +
                        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
                        (errors.text || serverError ? "border-danger" : "border-border")
                      }
                    />
                    <p id={`${textId}-error`} aria-live="polite" className="min-h-5 text-sm text-danger">
                      {errors.text}
                    </p>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
        <div>
          <Button variant="secondary" onClick={addCaption} disabled={busy}>
            Add caption
          </Button>
        </div>
      </section>
    </div>
  );
}

/** Drafts with usable times, for previewing while some caption is still invalid. */
function previewableDrafts(drafts: readonly CaptionDraft[]) {
  return drafts.flatMap((draft) => {
    const start = parseTimestamp(draft.start);
    const end = parseTimestamp(draft.end);
    return start !== null && end !== null && end > start
      ? [{ start_seconds: start, end_seconds: end, text: draft.text }]
      : [];
  });
}
