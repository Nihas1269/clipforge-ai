import { afterEach, describe, expect, it, vi } from "vitest";

import { playRange, seekTo } from "@/components/videos/player";

function fakeVideo({ readyState = 1, duration = 60 } = {}) {
  const element = document.createElement("video");
  const media = { readyState, currentTime: 0 };
  Object.defineProperty(element, "readyState", { configurable: true, get: () => media.readyState });
  Object.defineProperty(element, "duration", { configurable: true, get: () => duration });
  Object.defineProperty(element, "currentTime", {
    configurable: true,
    get: () => media.currentTime,
    set: (value: number) => {
      media.currentTime = value;
    },
  });
  const play = vi.fn(() => Promise.resolve());
  const pause = vi.fn();
  element.play = play;
  element.pause = pause;
  return { element, media, play, pause };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("seekTo", () => {
  it("clamps the position to the media", () => {
    const { element, media } = fakeVideo({ duration: 60 });

    seekTo(element, 90);
    expect(media.currentTime).toBe(60);
    seekTo(element, -3);
    expect(media.currentTime).toBe(0);
  });

  it("does not seek after its cleanup ran", () => {
    const { element, media } = fakeVideo({ readyState: 0 });

    const cleanup = seekTo(element, 10);
    cleanup();
    media.readyState = 1;
    element.dispatchEvent(new Event("loadedmetadata"));

    expect(media.currentTime).toBe(0);
  });
});

describe("playRange", () => {
  it("stops listening when playback is refused", async () => {
    const { element, media, play, pause } = fakeVideo();
    play.mockReturnValue(Promise.reject(new DOMException("blocked", "NotAllowedError")));
    const removed = vi.spyOn(element, "removeEventListener");

    playRange(element, 5, 20);
    await Promise.resolve();
    await Promise.resolve();

    expect(removed).toHaveBeenCalledWith("timeupdate", expect.any(Function));
    media.currentTime = 25;
    element.dispatchEvent(new Event("timeupdate"));
    expect(pause).not.toHaveBeenCalled();
  });

  it("does nothing for an empty range", () => {
    const { element, play } = fakeVideo({ duration: 60 });

    playRange(element, 70, 80);

    expect(play).not.toHaveBeenCalled();
  });

  it("ends when the user pauses and when cleaned up", () => {
    const { element, media, pause } = fakeVideo();

    playRange(element, 5, 20);
    element.dispatchEvent(new Event("pause"));
    media.currentTime = 25;
    element.dispatchEvent(new Event("timeupdate"));
    expect(pause).not.toHaveBeenCalled();

    const stop = playRange(element, 5, 20);
    stop();
    element.dispatchEvent(new Event("timeupdate"));
    expect(pause).not.toHaveBeenCalled();
  });
});
