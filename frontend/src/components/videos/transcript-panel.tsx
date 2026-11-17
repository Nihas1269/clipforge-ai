"use client";

import { useCallback, useId, useState, type ReactNode } from "react";

import { Button } from "@/components/button";
import { useTrackedJob, type TrackedJob } from "@/components/videos/use-tracked-job";
import { getErrorMessage } from "@/lib/api/client";
import { startTranscription, subtitleUrl, type Transcript } from "@/lib/api/transcripts";
import { formatTimestamp } from "@/lib/timestamps";

/** Offered in the language select; the empty value asks the backend to detect it. */
export const TRANSCRIPT_LANGUAGES: readonly { value: string; label: string }[] = [
  { value: "", label: "Detect automatically" },
  { value: "en", label: "English" },
  { value: "hi", label: "Hindi" },
  { value: "te", label: "Telugu" },
  { value: "ta", label: "Tamil" },
  { value: "kn", label: "Kannada" },
  { value: "ml", label: "Malayalam" },
  { value: "es", label: "Spanish" },
  { value: "fr", label: "French" },
  { value: "de", label: "German" },
];

export type TranscriptLoad =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "loaded"; transcript: Transcript | null };

export interface TranscriptPanelProps {
  videoId: string;
  /** Transcription needs a `ready` video. */
  videoReady: boolean;
  load: TranscriptLoad;
  /** True while the transcript is being fetched again; the previous one stays visible. */
  refreshing: boolean;
  onRefresh: () => Promise<void>;
  onSeek: (seconds: number) => void;
}

const linkClasses =
  "text-sm font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

function jobProgressLabel(job: TrackedJob): string {
  return job.status === "queued" ? "Transcription is queued…" : "Transcribing the video…";
}

/**
 * Starts transcription (with an optional language), follows the job until it
 * finishes, and shows the transcript as a scrollable list of timestamped
 * segments. Clicking a segment seeks the player to its start.
 */
export function TranscriptPanel({ videoId, videoReady, load, refreshing, onRefresh, onSeek }: TranscriptPanelProps) {
  const headingId = useId();
  const languageId = useId();
  const [language, setLanguage] = useState("");
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const handleFinished = useCallback(
    (finished: TrackedJob) => {
      if (finished.status === "succeeded") {
        void onRefresh();
      }
    },
    [onRefresh],
  );
  const { job, active, track } = useTrackedJob(handleFinished);

  async function generate() {
    if (starting || active) {
      return;
    }
    setStarting(true);
    setStartError(null);
    try {
      track(await startTranscription(videoId, language || undefined));
    } catch (caught) {
      setStartError(`Could not start transcription. ${getErrorMessage(caught)}`);
    } finally {
      setStarting(false);
    }
  }

  const transcript = load.state === "loaded" ? load.transcript : null;
  const busy = starting || active;

  let jobMessage: ReactNode = null;
  let jobError: string | null = startError;
  if (job && active) {
    jobMessage = jobProgressLabel(job);
  } else if (job?.status === "succeeded") {
    jobMessage = "The transcript is ready.";
  } else if (job?.status === "failed") {
    jobError = `Transcription failed. ${job.errorMessage ?? "Please try again."}`;
  } else if (job?.status === "cancelled") {
    jobError = "Transcription was cancelled.";
  } else if (job?.status === "unknown") {
    jobError = job.errorMessage;
  }

  let body: ReactNode;
  if (load.state === "loading") {
    body = (
      <p role="status" className="text-sm text-fg-muted">
        Loading the transcript…
      </p>
    );
  } else if (load.state === "error") {
    body = (
      <div className="flex flex-col items-start gap-3 rounded-lg border border-danger px-4 py-4">
        <p role="alert" className="text-sm text-fg">
          Could not load the transcript. {load.message}
        </p>
        <Button variant="secondary" onClick={() => void onRefresh()} disabled={refreshing}>
          Try again
        </Button>
      </div>
    );
  } else if (!transcript) {
    body = <p className="text-sm text-fg-muted">This video has no transcript yet.</p>;
  } else if (transcript.segments.length === 0) {
    body = <p className="text-sm text-fg-muted">The transcript is empty: no speech was found in the video.</p>;
  } else {
    body = (
      <ol
        aria-label="Transcript segments"
        className="flex max-h-96 flex-col gap-1 overflow-y-auto rounded-lg border border-border bg-surface p-2"
      >
        {transcript.segments.map((segment) => {
          const start = segment.start_seconds;
          const content = (
            <>
              <span className="shrink-0 font-mono text-xs text-fg-muted">
                {start === null ? "--:--" : <time dateTime={`PT${start}S`}>{formatTimestamp(start)}</time>}
              </span>
              <span className="text-fg">{segment.text}</span>
            </>
          );
          return (
            <li key={segment.id}>
              {start === null ? (
                <p className="flex gap-3 px-2 py-1.5 text-sm">{content}</p>
              ) : (
                <button
                  type="button"
                  onClick={() => onSeek(start)}
                  className="flex w-full gap-3 rounded-md px-2 py-1.5 text-left text-sm hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                >
                  {content}
                </button>
              )}
            </li>
          );
        })}
      </ol>
    );
  }

  return (
    <section aria-labelledby={headingId} className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <h2 id={headingId} className="text-xl font-semibold text-fg">
          Transcript
        </h2>
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <label htmlFor={languageId} className="text-xs font-medium text-fg-muted">
              Language
            </label>
            <select
              id={languageId}
              value={language}
              disabled={busy}
              onChange={(event) => setLanguage(event.target.value)}
              className="rounded-md border border-border bg-surface-raised px-2 py-2 text-sm text-fg focus-visible:outline-2 focus-visible:outline-accent"
            >
              {TRANSCRIPT_LANGUAGES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <Button onClick={() => void generate()} disabled={!videoReady || busy}>
            {busy ? "Transcribing…" : transcript ? "Regenerate transcript" : "Generate transcript"}
          </Button>
        </div>
      </div>
      {!videoReady ? (
        <p className="text-sm text-fg-muted">The video can be transcribed once it is ready.</p>
      ) : null}

      <div role="alert">
        {jobError ? <p className="rounded-md border border-danger px-3 py-2 text-sm text-fg">{jobError}</p> : null}
      </div>
      <div role="status">{jobMessage ? <p className="text-sm text-fg-muted">{jobMessage}</p> : null}</div>

      {body}

      {transcript ? (
        <div className="flex flex-wrap items-center gap-4">
          <a href={subtitleUrl(videoId, "srt")} download className={linkClasses}>
            Download SRT
          </a>
          <a href={subtitleUrl(videoId, "vtt")} download className={linkClasses}>
            Download VTT
          </a>
          <Button variant="secondary" onClick={() => void onRefresh()} disabled={refreshing}>
            {refreshing ? "Refreshing…" : "Refresh transcript"}
          </Button>
          {transcript.language ? (
            <p className="text-sm text-fg-muted">Language: {transcript.language}</p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
