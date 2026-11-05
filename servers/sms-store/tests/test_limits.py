from pdl_sms_store import limits
from pdl_sms_store.limits import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_blocks_over_limit_then_recovers():
    clock = Clock()
    rl = RateLimiter(limit=2, window_s=10, clock=clock)
    assert rl.allow("a") and rl.allow("a")
    assert not rl.allow("a")
    assert rl.allow("b")
    assert rl.retry_after("a") == 10
    clock.t = 4.5
    assert rl.retry_after("a") == 6
    clock.t = 10.0
    assert rl.allow("a")


def test_retry_after_unknown_key_is_one():
    assert RateLimiter().retry_after("nobody") == 1


def test_prunes_stale_keys(monkeypatch):
    monkeypatch.setattr(limits, "_PRUNE_AT", 3)
    clock = Clock()
    rl = RateLimiter(limit=1, window_s=1, clock=clock)
    for key in "abc":
        rl.allow(key)
    clock.t = 5
    rl.allow("d")
    assert list(rl._hits) == ["d"]


def test_utcnow_is_aware():
    assert limits.utcnow().tzinfo is not None
