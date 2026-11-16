"""In-process sliding-window rate limiting.

State lives in process memory, so limits apply per worker process and reset
on restart. A shared (for example Redis-backed) limiter can replace it behind
the same `hit` interface.
"""

import threading
import time
from collections import deque
from collections.abc import Callable


class RateLimitExceededError(Exception):
    """Raised when a key has used up its attempts for the current window."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Too many attempts. Try again later.")
        self.retry_after_seconds = retry_after_seconds


class InMemoryRateLimiter:
    """Allow at most `max_attempts` hits per key within `window_seconds`."""

    # Above this many tracked keys, keys without recent hits are pruned so
    # memory does not grow without bound.
    MAX_TRACKED_KEYS = 10_000

    def __init__(
        self,
        max_attempts: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts <= 0 or window_seconds <= 0:
            raise ValueError("max_attempts and window_seconds must be positive.")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> None:
        """Record an attempt for the key or raise RateLimitExceededError."""
        with self._lock:
            now = self._clock()
            cutoff = now - self.window_seconds
            if len(self._hits) > self.MAX_TRACKED_KEYS:
                self._prune(cutoff)
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.max_attempts:
                retry_after = hits[0] + self.window_seconds - now
                raise RateLimitExceededError(max(1, int(retry_after + 0.999)))
            hits.append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def _prune(self, cutoff: float) -> None:
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]
