"use client";

import { useEffect, useRef } from "react";

/**
 * Runs `task` every `intervalMs` while `enabled` is true.
 *
 * The next run is scheduled only after the previous one finished, so runs
 * never overlap. Polling pauses while the page is hidden and runs at once
 * when it is shown again. The timer and the listener are removed when
 * `enabled` turns false and on unmount. `task` must handle its own errors.
 */
export function usePolling(enabled: boolean, task: () => Promise<void>, intervalMs: number): void {
  const taskRef = useRef(task);

  useEffect(() => {
    taskRef.current = task;
  }, [task]);

  useEffect(() => {
    if (!enabled) {
      return;
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    let running = false;

    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(() => void tick(), intervalMs);
    }

    async function tick() {
      if (stopped || running || document.visibilityState === "hidden") {
        // A hidden page is resumed by the visibilitychange handler; a running
        // task schedules the next run itself.
        return;
      }
      running = true;
      try {
        await taskRef.current();
      } catch {
        // The task reports its own errors.
      } finally {
        running = false;
      }
      if (!stopped) {
        schedule();
      }
    }

    function handleVisibilityChange() {
      if (document.visibilityState === "hidden") {
        clearTimeout(timer);
      } else {
        clearTimeout(timer);
        void tick();
      }
    }

    schedule();
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [enabled, intervalMs]);
}
