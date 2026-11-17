"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/button";
import { StatusBadge } from "@/components/dashboard/status-badge";
import { ClipsPanel } from "@/components/videos/clips-panel";
import { playRange, seekTo } from "@/components/videos/player";
import { TranscriptPanel, type TranscriptLoad } from "@/components/videos/transcript-panel";
import { usePolling } from "@/components/videos/use-polling";
import { ApiError, getErrorMessage } from "@/lib/api/client";
import { getTranscript } from "@/lib/api/transcripts";
import { ACTIVE_VIDEO_STATUSES, getVideo, videoMediaUrl, type Video } from "@/lib/api/videos";
import { formatBytes, formatDate, formatDuration } from "@/lib/format";

/** How often the video is re-read while it is still being processed. */
export const VIDEO_POLL_INTERVAL_MS = 4_000;

type VideoLoad =
  | { state: "loading" }
  | { state: "not_found" }
  | { state: "error"; message: string }
  | { state: "loaded"; video: Video };

const linkClasses =
  "text-sm font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

function resolution(video: Video): string {
  return video.width && video.height ? `${video.width} × ${video.height}` : "Unknown";
}

/**
 * The video detail page: the source video player, metadata and status, the
 * transcript (with transcription) and the clip suggestions (with detection,
 * review and manual clips). The video is re-read while it is `uploaded` or
 * `processing`.
 */
export function VideoDetail({ videoId }: { videoId: string }) {
  const [videoLoad, setVideoLoad] = useState<VideoLoad>({ state: "loading" });
  const [videoReload, setVideoReload] = useState(0);
  const [transcriptLoad, setTranscriptLoad] = useState<TranscriptLoad>({ state: "loading" });
  const [refreshingTranscript, setRefreshingTranscript] = useState(false);

  const videoRef = useRef<HTMLVideoElement>(null);
  const playbackCleanup = useRef<(() => void) | null>(null);
  const transcriptSeq = useRef(0);

  const fetchVideo = useCallback(async (): Promise<VideoLoad> => {
    try {
      return { state: "loaded", video: await getVideo(videoId) };
    } catch (caught) {
      if (caught instanceof ApiError && (caught.status === 404 || caught.status === 422)) {
        return { state: "not_found" };
      }
      return { state: "error", message: getErrorMessage(caught) };
    }
  }, [videoId]);

  useEffect(() => {
    let cancelled = false;
    void fetchVideo().then((result) => {
      if (!cancelled) {
        setVideoLoad(result);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [fetchVideo, videoReload]);

  const fetchTranscript = useCallback(async (): Promise<TranscriptLoad | null> => {
    transcriptSeq.current += 1;
    const seq = transcriptSeq.current;
    let result: TranscriptLoad;
    try {
      result = { state: "loaded", transcript: await getTranscript(videoId) };
    } catch (caught) {
      result = { state: "error", message: getErrorMessage(caught) };
    }
    // Only the latest request is applied, so an older response never overwrites a newer one.
    return seq === transcriptSeq.current ? result : null;
  }, [videoId]);

  useEffect(() => {
    let cancelled = false;
    void fetchTranscript().then((result) => {
      if (result && !cancelled) {
        setTranscriptLoad(result);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [fetchTranscript]);

  const refreshTranscript = useCallback(async () => {
    setRefreshingTranscript(true);
    const result = await fetchTranscript();
    if (result) {
      setTranscriptLoad(result);
      setRefreshingTranscript(false);
    }
  }, [fetchTranscript]);

  const video = videoLoad.state === "loaded" ? videoLoad.video : null;
  const processing = video !== null && ACTIVE_VIDEO_STATUSES.includes(video.status);

  const pollVideo = useCallback(async () => {
    const result = await fetchVideo();
    // A failed poll keeps the video already shown; the next poll tries again.
    if (result.state === "loaded") {
      setVideoLoad(result);
    }
  }, [fetchVideo]);

  usePolling(processing, pollVideo, VIDEO_POLL_INTERVAL_MS);

  // Ends a seek or preview still waiting for metadata, and its listeners, on unmount.
  useEffect(
    () => () => {
      playbackCleanup.current?.();
      playbackCleanup.current = null;
    },
    [],
  );

  const handleSeek = useCallback((seconds: number) => {
    const element = videoRef.current;
    if (!element) {
      return;
    }
    playbackCleanup.current?.();
    playbackCleanup.current = seekTo(element, seconds);
  }, []);

  const handlePreview = useCallback((start: number, end: number) => {
    const element = videoRef.current;
    if (!element) {
      return;
    }
    playbackCleanup.current?.();
    playbackCleanup.current = playRange(element, start, end);
  }, []);

  if (videoLoad.state === "loading") {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading the video…
      </p>
    );
  }
  if (videoLoad.state === "not_found") {
    return (
      <div className="flex flex-col items-start gap-3">
        <h1 className="text-3xl font-semibold tracking-tight text-fg">Video not found</h1>
        <p className="text-base text-fg-muted">This video does not exist or you do not have access to it.</p>
        <Link href="/dashboard" className={linkClasses}>
          Back to the dashboard
        </Link>
      </div>
    );
  }
  if (videoLoad.state === "error") {
    return (
      <div className="flex flex-col items-start gap-3 rounded-lg border border-danger px-4 py-4">
        <p role="alert" className="text-sm text-fg">
          Could not load the video. {videoLoad.message}
        </p>
        <Button
          variant="secondary"
          onClick={() => {
            setVideoLoad({ state: "loading" });
            setVideoReload((count) => count + 1);
          }}
        >
          Try again
        </Button>
      </div>
    );
  }

  const loaded = videoLoad.video;
  const ready = loaded.status === "ready";
  const hasTranscript = transcriptLoad.state === "loaded" && transcriptLoad.transcript !== null;

  return (
    <div className="flex flex-col gap-10">
      <header className="flex flex-col gap-3">
        <p>
          <Link href="/dashboard" className={linkClasses}>
            Back to the dashboard
          </Link>
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-3xl font-semibold tracking-tight break-all text-fg">{loaded.original_filename}</h1>
          <StatusBadge status={loaded.status} />
        </div>
        {processing ? (
          <p role="status" className="text-sm text-fg-muted">
            The video is being processed. This page updates automatically.
          </p>
        ) : null}
        {loaded.status === "failed" ? (
          <p role="alert" className="text-sm text-fg">
            Processing failed. {loaded.error_message ?? "Please upload the video again."}
          </p>
        ) : null}
      </header>

      <div className="grid gap-6 lg:grid-cols-[2fr_1fr]">
        <div className="overflow-hidden rounded-lg border border-border bg-black">
          {/* The transcript below is the text alternative of the audio. */}
          <video
            ref={videoRef}
            src={videoMediaUrl(loaded.id)}
            controls
            preload="metadata"
            aria-label={`Source video: ${loaded.original_filename}`}
            className="aspect-video w-full"
          />
        </div>
        <dl className="grid grid-cols-[auto_1fr] content-start gap-x-4 gap-y-2 text-sm">
          <dt className="text-fg-muted">Duration</dt>
          <dd className="text-fg">{formatDuration(loaded.duration_seconds)}</dd>
          <dt className="text-fg-muted">Resolution</dt>
          <dd className="text-fg">{resolution(loaded)}</dd>
          <dt className="text-fg-muted">Frame rate</dt>
          <dd className="text-fg">{loaded.fps ? `${Math.round(loaded.fps * 100) / 100} fps` : "Unknown"}</dd>
          <dt className="text-fg-muted">Size</dt>
          <dd className="text-fg">{formatBytes(loaded.size_bytes)}</dd>
          <dt className="text-fg-muted">Uploaded</dt>
          <dd className="text-fg">
            <time dateTime={loaded.created_at}>{formatDate(loaded.created_at)}</time>
          </dd>
          {loaded.project ? (
            <>
              <dt className="text-fg-muted">Project</dt>
              <dd className="text-fg">{loaded.project.name}</dd>
            </>
          ) : null}
        </dl>
      </div>

      <TranscriptPanel
        videoId={loaded.id}
        videoReady={ready}
        load={transcriptLoad}
        refreshing={refreshingTranscript}
        onRefresh={refreshTranscript}
        onSeek={handleSeek}
      />

      <ClipsPanel
        videoId={loaded.id}
        hasTranscript={hasTranscript}
        videoReady={ready}
        durationSeconds={loaded.duration_seconds}
        onPreview={handlePreview}
      />
    </div>
  );
}
