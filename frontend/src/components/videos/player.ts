/**
 * Seeking and range playback on a `<video>` element. Each function returns a
 * cleanup that cancels anything still pending and removes its listeners.
 */

/** `HTMLMediaElement.HAVE_METADATA`: the duration and dimensions are known. */
const HAVE_METADATA = 1;

const noop = () => {};

/** Runs `action` once the element's metadata is loaded (at once if it already is). */
function whenMetadataLoaded(video: HTMLVideoElement, action: () => void): () => void {
  if (video.readyState >= HAVE_METADATA) {
    action();
    return noop;
  }
  const handleLoaded = () => {
    video.removeEventListener("loadedmetadata", handleLoaded);
    action();
  };
  video.addEventListener("loadedmetadata", handleLoaded);
  return () => video.removeEventListener("loadedmetadata", handleLoaded);
}

/** Keeps `seconds` within `[0, duration]` (only `>= 0` while the duration is unknown). */
function clampToMedia(video: HTMLVideoElement, seconds: number): number {
  const duration = video.duration;
  const max = Number.isFinite(duration) && duration > 0 ? duration : Number.POSITIVE_INFINITY;
  return Math.min(Math.max(0, seconds), max);
}

function setCurrentTime(video: HTMLVideoElement, seconds: number): boolean {
  try {
    video.currentTime = seconds;
    return true;
  } catch {
    return false;
  }
}

/** Moves the playhead to `seconds` once the metadata is loaded. */
export function seekTo(video: HTMLVideoElement, seconds: number): () => void {
  if (!Number.isFinite(seconds)) {
    return noop;
  }
  return whenMetadataLoaded(video, () => {
    setCurrentTime(video, clampToMedia(video, seconds));
  });
}

/**
 * Plays the source from `start` and pauses at `end`, once the metadata is
 * loaded. The preview ends (and its listeners are removed) when it reaches
 * `end`, when playback pauses or ends for any other reason, when `play()` is
 * refused, or when the returned cleanup is called.
 */
export function playRange(video: HTMLVideoElement, start: number, end: number): () => void {
  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    return noop;
  }
  let stopped = false;
  let cancelWait: () => void = noop;
  let to = end;

  const handleTimeUpdate = () => {
    if (video.currentTime >= to) {
      stop();
      video.pause();
    }
  };

  function stop() {
    if (stopped) {
      return;
    }
    stopped = true;
    cancelWait();
    video.removeEventListener("timeupdate", handleTimeUpdate);
    video.removeEventListener("pause", stop);
    video.removeEventListener("ended", stop);
  }

  cancelWait = whenMetadataLoaded(video, () => {
    if (stopped) {
      return;
    }
    const from = clampToMedia(video, start);
    to = clampToMedia(video, end);
    if (!(from < to) || !setCurrentTime(video, from)) {
      stop();
      return;
    }
    video.addEventListener("timeupdate", handleTimeUpdate);
    video.addEventListener("pause", stop);
    video.addEventListener("ended", stop);
    try {
      const played = video.play() as Promise<void> | undefined;
      played?.catch(() => stop());
    } catch {
      stop();
    }
  });
  return stop;
}
