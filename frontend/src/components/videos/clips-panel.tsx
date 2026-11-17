"use client";

import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";

import { Button } from "@/components/button";
import { TextField } from "@/components/text-field";
import { useTrackedJob, type TrackedJob } from "@/components/videos/use-tracked-job";
import { getErrorMessage } from "@/lib/api/client";
import { createClip, detectClips, listClips, updateClipStatus, type Clip, type ClipStatus } from "@/lib/api/clips";
import { formatSeconds, formatTimestamp, parseTimestamp } from "@/lib/timestamps";

export const CLIP_STATUS_LABELS: Record<ClipStatus, string> = {
  suggested: "Suggested",
  accepted: "Accepted",
  rejected: "Rejected",
};

export interface ClipsPanelProps {
  videoId: string;
  /** Clip detection needs a transcript; the button is disabled without one. */
  hasTranscript: boolean;
  /** Clips can be changed only while the video is `ready`. */
  videoReady: boolean;
  /** The video's duration in seconds, used to check manual clips; `null` when unknown. */
  durationSeconds: number | null;
  onPreview: (start: number, end: number) => void;
}

export interface ManualClipErrors {
  start?: string;
  end?: string;
}

export interface ManualClipRange {
  start: number;
  end: number;
}

/**
 * Checks a manual clip in the browser: both times must be valid timestamps,
 * `0 <= start < end`, and the end must lie within the video. The backend checks
 * the range (and the allowed clip length) again.
 */
export function validateManualClip(
  startInput: string,
  endInput: string,
  durationSeconds: number | null,
): { range: ManualClipRange; errors: null } | { range: null; errors: ManualClipErrors } {
  const errors: ManualClipErrors = {};
  const start = parseTimestamp(startInput);
  const end = parseTimestamp(endInput);
  if (!startInput.trim()) {
    errors.start = "Enter a start time.";
  } else if (start === null) {
    errors.start = "Enter a time such as 1:05 or 65.";
  }
  if (!endInput.trim()) {
    errors.end = "Enter an end time.";
  } else if (end === null) {
    errors.end = "Enter a time such as 1:35 or 95.";
  }
  if (start !== null && end !== null) {
    if (end <= start) {
      errors.end = "The end must be after the start.";
    } else if (durationSeconds !== null && end > durationSeconds) {
      errors.end = `The end must be within the video (${formatTimestamp(durationSeconds)}).`;
    }
  }
  if (start !== null && end !== null && !errors.start && !errors.end) {
    return { range: { start, end }, errors: null };
  }
  return { range: null, errors };
}

type ClipsLoad = { state: "loading" } | { state: "error"; message: string } | { state: "loaded"; clips: Clip[] };

function replaceClip(clips: Clip[], updated: Clip): Clip[] {
  return clips.map((clip) => (clip.id === updated.id ? updated : clip));
}

/**
 * Finds clips (once a transcript exists), lists the suggestions with Accept,
 * Reject and Preview actions, and adds clips manually.
 */
export function ClipsPanel({ videoId, hasTranscript, videoReady, durationSeconds, onPreview }: ClipsPanelProps) {
  const headingId = useId();
  const formId = useId();
  const [load, setLoad] = useState<ClipsLoad>({ state: "loading" });
  const [reloadCount, setReloadCount] = useState(0);
  const [detecting, setDetecting] = useState(false);
  const [detectError, setDetectError] = useState<string | null>(null);
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(new Set());
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [startInput, setStartInput] = useState("");
  const [endInput, setEndInput] = useState("");
  const [titleInput, setTitleInput] = useState("");
  const [fieldErrors, setFieldErrors] = useState<ManualClipErrors>({});
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const requestSeq = useRef(0);

  const fetchClips = useCallback(async (): Promise<ClipsLoad | null> => {
    requestSeq.current += 1;
    const seq = requestSeq.current;
    try {
      const list = await listClips(videoId);
      return seq === requestSeq.current ? { state: "loaded", clips: list.items } : null;
    } catch (caught) {
      return seq === requestSeq.current ? { state: "error", message: getErrorMessage(caught) } : null;
    }
  }, [videoId]);

  const refresh = useCallback(async () => {
    const result = await fetchClips();
    if (result) {
      // A failed refresh keeps the clips already shown.
      setLoad((previous) => (result.state === "error" && previous.state === "loaded" ? previous : result));
    }
  }, [fetchClips]);

  useEffect(() => {
    let cancelled = false;
    void fetchClips().then((result) => {
      if (result && !cancelled) {
        setLoad(result);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [fetchClips, reloadCount]);

  const handleFinished = useCallback(
    (finished: TrackedJob) => {
      if (finished.status === "succeeded") {
        void refresh();
      }
    },
    [refresh],
  );
  const { job, active, track } = useTrackedJob(handleFinished);

  async function findClips() {
    if (detecting || active || !hasTranscript) {
      return;
    }
    setDetecting(true);
    setDetectError(null);
    try {
      track(await detectClips(videoId));
    } catch (caught) {
      setDetectError(`Could not start clip detection. ${getErrorMessage(caught)}`);
    } finally {
      setDetecting(false);
    }
  }

  async function changeStatus(clip: Clip, status: "accepted" | "rejected") {
    if (pendingIds.has(clip.id)) {
      return;
    }
    setPendingIds((ids) => new Set(ids).add(clip.id));
    setActionError(null);
    setNotice(null);
    try {
      const updated = await updateClipStatus(clip.id, status);
      setLoad((previous) =>
        previous.state === "loaded" ? { state: "loaded", clips: replaceClip(previous.clips, updated) } : previous,
      );
      setNotice(`${status === "accepted" ? "Accepted" : "Rejected"} ${updated.title}.`);
    } catch (caught) {
      setActionError(`Could not update ${clip.title}. ${getErrorMessage(caught)}`);
    } finally {
      setPendingIds((ids) => {
        const next = new Set(ids);
        next.delete(clip.id);
        return next;
      });
    }
  }

  async function addClip(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (creating) {
      return;
    }
    setCreateError(null);
    setNotice(null);
    const result = validateManualClip(startInput, endInput, durationSeconds);
    if (!result.range) {
      setFieldErrors(result.errors);
      return;
    }
    setFieldErrors({});
    setCreating(true);
    try {
      const created = await createClip(videoId, {
        start_seconds: result.range.start,
        end_seconds: result.range.end,
        title: titleInput.trim() || undefined,
      });
      setLoad((previous) =>
        previous.state === "loaded" ? { state: "loaded", clips: [...previous.clips, created] } : previous,
      );
      setStartInput("");
      setEndInput("");
      setTitleInput("");
      setNotice(`Added ${created.title}.`);
    } catch (caught) {
      setCreateError(`Could not add the clip. ${getErrorMessage(caught)}`);
    } finally {
      setCreating(false);
    }
  }

  let jobMessage: string | null = null;
  let jobError: string | null = detectError;
  if (job && active) {
    jobMessage = job.status === "queued" ? "Clip detection is queued…" : "Finding clips…";
  } else if (job?.status === "succeeded") {
    jobMessage = "Clip suggestions are ready.";
  } else if (job?.status === "failed") {
    jobError = `Clip detection failed. ${job.errorMessage ?? "Please try again."}`;
  } else if (job?.status === "cancelled") {
    jobError = "Clip detection was cancelled.";
  } else if (job?.status === "unknown") {
    jobError = job.errorMessage;
  }

  const findBusy = detecting || active;

  let body: ReactNode;
  if (load.state === "loading") {
    body = (
      <p role="status" className="text-sm text-fg-muted">
        Loading clips…
      </p>
    );
  } else if (load.state === "error") {
    body = (
      <div className="flex flex-col items-start gap-3 rounded-lg border border-danger px-4 py-4">
        <p role="alert" className="text-sm text-fg">
          Could not load the clips. {load.message}
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
  } else if (load.clips.length === 0) {
    body = <p className="text-sm text-fg-muted">No clips yet.</p>;
  } else {
    body = (
      <ul aria-label="Clips" className="flex flex-col gap-3">
        {load.clips.map((clip) => {
          const pending = pendingIds.has(clip.id);
          return (
            <li
              key={clip.id}
              data-testid={`clip-${clip.id}`}
              className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-4"
            >
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="text-base font-medium text-fg">{clip.title}</h3>
                <span
                  data-status={clip.status}
                  className="inline-flex items-center rounded-full border border-border px-2 py-0.5 text-xs font-medium text-fg"
                >
                  <span className="sr-only">Status: </span>
                  {CLIP_STATUS_LABELS[clip.status]}
                </span>
              </div>
              <dl className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-fg-muted">
                <div className="flex gap-1">
                  <dt>Time</dt>
                  <dd>
                    {formatTimestamp(clip.start_seconds)} – {formatTimestamp(clip.end_seconds)}
                  </dd>
                </div>
                <div className="flex gap-1">
                  <dt>Duration</dt>
                  <dd>{formatSeconds(clip.end_seconds - clip.start_seconds)}</dd>
                </div>
                <div className="flex gap-1">
                  <dt>Score</dt>
                  <dd>{Math.round(clip.score * 100)}%</dd>
                </div>
              </dl>
              {clip.reason ? <p className="text-sm text-fg">{clip.reason}</p> : null}
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="secondary"
                  onClick={() => onPreview(clip.start_seconds, clip.end_seconds)}
                  aria-label={`Preview ${clip.title}`}
                >
                  Preview
                </Button>
                <Button
                  onClick={() => void changeStatus(clip, "accepted")}
                  disabled={pending || !videoReady || clip.status === "accepted"}
                  aria-label={`Accept ${clip.title}`}
                >
                  Accept
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => void changeStatus(clip, "rejected")}
                  disabled={pending || !videoReady || clip.status === "rejected"}
                  aria-label={`Reject ${clip.title}`}
                >
                  Reject
                </Button>
              </div>
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <h2 id={headingId} className="text-xl font-semibold text-fg">
          Clips
        </h2>
        <Button onClick={() => void findClips()} disabled={!hasTranscript || !videoReady || findBusy}>
          {findBusy ? "Finding clips…" : "Find clips"}
        </Button>
      </div>
      {!hasTranscript ? (
        <p className="text-sm text-fg-muted">Generate a transcript first to find clips.</p>
      ) : null}

      <div role="alert">
        {jobError ? <p className="rounded-md border border-danger px-3 py-2 text-sm text-fg">{jobError}</p> : null}
        {actionError ? (
          <p className="rounded-md border border-danger px-3 py-2 text-sm text-fg">{actionError}</p>
        ) : null}
      </div>
      <div role="status">
        {jobMessage ? <p className="text-sm text-fg-muted">{jobMessage}</p> : null}
        {notice ? <p className="text-sm text-fg-muted">{notice}</p> : null}
      </div>

      {body}

      <form
        aria-labelledby={`${formId}-heading`}
        noValidate
        onSubmit={(event) => void addClip(event)}
        className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-4"
      >
        <h3 id={`${formId}-heading`} className="text-base font-medium text-fg">
          Add clip manually
        </h3>
        <div className="grid gap-3 sm:grid-cols-3">
          <TextField
            id={`${formId}-start`}
            label="Start time"
            hint="m:ss or seconds"
            value={startInput}
            onChange={(event) => setStartInput(event.target.value)}
            error={fieldErrors.start}
            inputMode="decimal"
            autoComplete="off"
          />
          <TextField
            id={`${formId}-end`}
            label="End time"
            hint="m:ss or seconds"
            value={endInput}
            onChange={(event) => setEndInput(event.target.value)}
            error={fieldErrors.end}
            inputMode="decimal"
            autoComplete="off"
          />
          <TextField
            id={`${formId}-title`}
            label="Title (optional)"
            value={titleInput}
            maxLength={200}
            onChange={(event) => setTitleInput(event.target.value)}
            autoComplete="off"
          />
        </div>
        <div role="alert">
          {createError ? (
            <p className="rounded-md border border-danger px-3 py-2 text-sm text-fg">{createError}</p>
          ) : null}
        </div>
        <div>
          <Button type="submit" disabled={creating || !videoReady}>
            {creating ? "Adding…" : "Add clip"}
          </Button>
        </div>
      </form>
    </section>
  );
}
