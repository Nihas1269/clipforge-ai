"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type ChangeEvent, type DragEvent } from "react";

import { Button } from "@/components/button";
import { ApiError, getErrorMessage } from "@/lib/api/client";
import { getUploadLimits, uploadVideo } from "@/lib/api/videos";
import {
  formatExtensions,
  formatMegabytes,
  getFallbackUploadRules,
  rulesFromLimits,
  validateVideoFile,
  type VideoUploadRules,
} from "@/lib/validation/video";

type Phase = "idle" | "uploading" | "finishing" | "done";

const INPUT_ID = "video-file";
const HINT_ID = "video-upload-hint";
const ERROR_ID = "video-upload-error";

function videoDetailPath(id: string): string {
  return `/videos/${encodeURIComponent(id)}`;
}

/**
 * Picks a video (file input or drag and drop), checks it against the
 * backend's limits, uploads it with progress and cancel, and opens the new
 * video. Rendered only inside the authenticated layout.
 */
export function VideoUploadForm() {
  const router = useRouter();
  const [rules, setRules] = useState<VideoUploadRules>(getFallbackUploadRules);
  const [phase, setPhase] = useState<Phase>("idle");
  const [fileName, setFileName] = useState("");
  const [percent, setPercent] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  // Incremented to move focus back to the drop zone once an upload ends.
  const [focusRequest, setFocusRequest] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const dropZoneRef = useRef<HTMLButtonElement>(null);
  // A stable holder, so the unmount cleanup sees the current upload.
  const uploadRef = useRef<{ controller: AbortController | null }>({ controller: null });

  const busy = phase !== "idle";

  useEffect(() => {
    let active = true;
    getUploadLimits()
      .then((limits) => {
        if (active) {
          setRules(rulesFromLimits(limits));
        }
      })
      .catch(() => {
        // Keep the fallback rules; the backend enforces its limits anyway.
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const upload = uploadRef.current;
    return () => upload.controller?.abort();
  }, []);

  useEffect(() => {
    if (focusRequest > 0) {
      dropZoneRef.current?.focus();
    }
  }, [focusRequest]);

  async function startUpload(file: File) {
    // The ref is set synchronously below, so it also blocks a second change or
    // drop event that arrives before React re-renders with the new phase.
    if (busy || uploadRef.current.controller) {
      return;
    }
    const validationError = validateVideoFile(file, rules);
    setError(validationError ?? null);
    setNotice(null);
    if (validationError) {
      return;
    }

    const controller = new AbortController();
    uploadRef.current.controller = controller;
    setFileName(file.name);
    setPercent(0);
    setPhase("uploading");
    try {
      const video = await uploadVideo(file, {
        signal: controller.signal,
        onProgress: ({ loaded, total }) =>
          setPercent(total > 0 ? Math.min(100, Math.floor((loaded / total) * 100)) : 0),
        onSent: () => {
          setPercent(100);
          setPhase("finishing");
        },
      });
      setPhase("done");
      setNotice("Upload complete. Opening your video…");
      router.push(videoDetailPath(video.id));
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "upload_cancelled") {
        setNotice("Upload cancelled.");
      } else {
        setError(getErrorMessage(caught));
      }
      setPhase("idle");
      setFocusRequest((count) => count + 1);
    } finally {
      if (uploadRef.current.controller === controller) {
        uploadRef.current.controller = null;
      }
    }
  }

  function handleChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    // Reset so choosing the same file again fires another change event.
    event.target.value = "";
    if (file) {
      void startUpload(file);
    }
  }

  function handleDragOver(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    if (!busy) {
      event.dataTransfer.dropEffect = "copy";
      setDragging(true);
    }
  }

  function handleDrop(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    setDragging(false);
    if (busy) {
      return;
    }
    const files = event.dataTransfer.files;
    if (files.length !== 1) {
      setNotice(null);
      setError("Drop one video file at a time.");
      return;
    }
    void startUpload(files[0]);
  }

  function cancel() {
    uploadRef.current.controller?.abort();
  }

  const hint = `${formatExtensions(rules.extensions)} files up to ${formatMegabytes(rules.maxBytes)}.`;
  let status = notice;
  if (phase === "uploading") {
    status = `Uploading ${fileName}…`;
  } else if (phase === "finishing") {
    status = `Upload sent. Checking and saving ${fileName}…`;
  }

  return (
    <section aria-labelledby="video-upload-heading" aria-busy={busy} className="flex flex-col gap-6">
      <h1 id="video-upload-heading" className="text-3xl font-semibold tracking-tight text-fg">
        Upload a video
      </h1>

      <div role="alert" aria-live="assertive" id={ERROR_ID}>
        {error ? (
          <p className="rounded-md border border-danger px-3 py-2 text-sm text-fg">{error}</p>
        ) : null}
      </div>
      <div role="status">
        {status ? <p className="text-sm text-fg-muted">{status}</p> : null}
      </div>

      <button
        ref={dropZoneRef}
        type="button"
        disabled={busy}
        onClick={() => inputRef.current?.click()}
        onDragOver={handleDragOver}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        aria-describedby={HINT_ID}
        data-dragging={dragging || undefined}
        className={[
          "flex flex-col items-center gap-2 rounded-lg border-2 border-dashed px-6 py-12 text-center",
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
          "disabled:cursor-not-allowed disabled:opacity-50",
          dragging ? "border-accent bg-surface-hover" : "border-border bg-surface",
        ].join(" ")}
      >
        <span className="text-base font-semibold text-fg">Drag and drop a video here</span>
        <span className="text-sm text-fg-muted">or press to choose a file</span>
      </button>

      <div className="flex flex-col gap-2">
        <label htmlFor={INPUT_ID} className="text-sm font-medium text-fg">
          Video file
        </label>
        <input
          ref={inputRef}
          id={INPUT_ID}
          name="file"
          type="file"
          accept={rules.extensions.join(",")}
          disabled={busy}
          onChange={handleChange}
          aria-describedby={`${HINT_ID} ${ERROR_ID}`}
          aria-invalid={error ? true : undefined}
          className="text-sm text-fg file:mr-4 file:rounded-md file:border file:border-border file:bg-surface-raised file:px-4 file:py-2 file:text-fg"
        />
        <p id={HINT_ID} className="text-sm text-fg-muted">
          {hint}
        </p>
      </div>

      {phase === "uploading" || phase === "finishing" ? (
        <div className="flex flex-col gap-3">
          <div
            role="progressbar"
            aria-label="Upload progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            aria-valuetext={`${percent}% uploaded`}
            className="h-2 w-full overflow-hidden rounded-full bg-surface-raised"
          >
            <div className="h-full bg-accent" style={{ width: `${percent}%` }} />
          </div>
          <p className="text-sm text-fg-muted" aria-hidden="true">
            {percent}%
          </p>
          <div>
            <Button variant="secondary" onClick={cancel} disabled={phase !== "uploading"}>
              Cancel upload
            </Button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
