import pytest

from app.core.rate_limit import InMemoryRateLimiter, RateLimitExceededError


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_max_attempts_per_window():
    clock = FakeClock()
    limiter = InMemoryRateLimiter(3, 60, clock=clock)

    for _ in range(3):
        limiter.hit("key")

    with pytest.raises(RateLimitExceededError) as exc_info:
        limiter.hit("key")
    assert exc_info.value.retry_after_seconds == 60


def test_attempts_expire_after_the_window():
    clock = FakeClock()
    limiter = InMemoryRateLimiter(1, 60, clock=clock)
    limiter.hit("key")

    clock.now += 30
    with pytest.raises(RateLimitExceededError) as exc_info:
        limiter.hit("key")
    assert exc_info.value.retry_after_seconds == 30

    clock.now += 30
    limiter.hit("key")


def test_rejected_attempts_do_not_extend_the_window():
    clock = FakeClock()
    limiter = InMemoryRateLimiter(1, 10, clock=clock)
    limiter.hit("key")
    for _ in range(5):
        with pytest.raises(RateLimitExceededError):
            limiter.hit("key")

    clock.now += 10
    limiter.hit("key")


def test_keys_are_independent_and_reset():
    limiter = InMemoryRateLimiter(1, 60, clock=FakeClock())
    limiter.hit("a")
    limiter.hit("b")

    with pytest.raises(RateLimitExceededError):
        limiter.hit("a")
    limiter.reset("a")
    limiter.hit("a")


def test_stale_keys_are_pruned(monkeypatch):
    clock = FakeClock()
    limiter = InMemoryRateLimiter(1, 10, clock=clock)
    monkeypatch.setattr(InMemoryRateLimiter, "MAX_TRACKED_KEYS", 5)
    for index in range(6):
        limiter.hit(f"key-{index}")

    clock.now += 11
    limiter.hit("fresh")

    assert set(limiter._hits) == {"fresh"}


@pytest.mark.parametrize(("attempts", "window"), [(0, 60), (1, 0), (-1, 60)])
def test_rejects_invalid_configuration(attempts, window):
    with pytest.raises(ValueError):
        InMemoryRateLimiter(attempts, window)
