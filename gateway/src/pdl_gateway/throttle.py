"""Process-wide lockout after repeated failures (approval passphrase, client secrets)."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ThrottleState:
    failures: tuple[float, ...] = ()
    locked_until: float = float("-inf")


class AttemptThrottle:
    """Locks for ``window_s`` after ``limit`` failures within ``window_s``.

    Process-wide, never per request or client, so fresh requests cannot reset the count.
    Successes do not lift a lock early.
    """

    def __init__(
        self,
        limit: int = 5,
        window_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        what: str = "approval",
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self.what = what
        self._clock = clock
        self._state = _ThrottleState()

    def blocked(self) -> bool:
        return self._clock() < self._state.locked_until

    def record_failure(self) -> None:
        now = self._clock()
        recent = tuple(t for t in self._state.failures if now - t < self.window_s) + (now,)
        if len(recent) >= self.limit:
            logger.warning(
                "%s locked for %ds after %d failed attempts", self.what, self.window_s, len(recent)
            )
            self._state = _ThrottleState((), now + self.window_s)
        else:
            self._state = _ThrottleState(recent, self._state.locked_until)
