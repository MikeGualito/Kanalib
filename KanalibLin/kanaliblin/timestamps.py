"""
Working with the device's frame timestamps.

Every :class:`~kanaliblin.events.LinFrame` carries ``timestamp_us``, captured in
the MCU's interrupt handler at the start of the frame. It is a **32-bit**
microsecond counter, so it wraps back to zero every ~71.6 minutes. Below that
horizon you can subtract two values directly; above it you cannot, and a
logger that ignores this sees time jump backwards.

Two tools, depending on what you are doing:

* :func:`delta_us` -- stateless, for the gap between two frames. Correct across
  one wrap, which is all a gap shorter than 71 minutes can ever cross.
* :class:`TimestampUnwrapper` -- stateful, for a continuous timeline over a long
  capture.
"""

from __future__ import annotations

import threading

__all__ = ["TIMESTAMP_MODULUS", "TIMESTAMP_WRAP_SECONDS", "delta_us", "TimestampUnwrapper"]

#: The counter is 32 bits wide, so it counts 0 .. 2**32 - 1 microseconds.
TIMESTAMP_MODULUS = 1 << 32

#: How long the counter takes to wrap: about 71.6 minutes.
TIMESTAMP_WRAP_SECONDS = TIMESTAMP_MODULUS / 1e6


def delta_us(earlier: int, later: int) -> int:
    """
    Microseconds from ``earlier`` to ``later``, correct across a wrap.

    Assumes the real interval is under ~71.6 minutes -- longer than that is
    indistinguishable from a shorter one, because the counter has come all the
    way round. Use :class:`TimestampUnwrapper` for long captures.

    ::

        gap = delta_us(previous.timestamp_us, frame.timestamp_us)
    """
    return (later - earlier) % TIMESTAMP_MODULUS


class TimestampUnwrapper:
    """
    Turn wrapping 32-bit timestamps into a continuous, always-increasing timeline.

    Feed it every frame's ``timestamp_us`` **in arrival order** and it counts
    the wraps for you::

        timeline = TimestampUnwrapper()
        for frame in frames:
            print(timeline.seconds(frame.timestamp_us), frame)

    The first value seen becomes the origin, so the timeline starts near zero
    rather than at whatever the MCU's counter happened to be.

    Two limits worth knowing:

    * **Order matters.** A value lower than the previous one is read as a wrap.
      Frames arrive in order from the device, so feeding them as they come is
      fine -- but do not feed the same stream twice from two places, and do not
      feed a sorted-by-something-else list.
    * **A device reset looks like a wrap.** If the MCU restarts, its counter
      starts over near zero, which is indistinguishable from wrapping, and the
      timeline gains a phantom ~71 minutes. :meth:`reset` clears the state; call
      it whenever you reconnect to a device.

    Instances are safe to share between threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: int | None = None
        self._wraps = 0
        self._origin = 0

    def reset(self) -> None:
        """Forget the timeline. Call this after reconnecting to a device."""
        with self._lock:
            self._last = None
            self._wraps = 0
            self._origin = 0

    def unwrap(self, timestamp_us: int) -> int:
        """Return the continuous microsecond value for one raw timestamp."""
        with self._lock:
            if self._last is None:
                self._origin = timestamp_us
            elif timestamp_us < self._last:
                self._wraps += 1
            self._last = timestamp_us
            return self._wraps * TIMESTAMP_MODULUS + timestamp_us - self._origin

    def seconds(self, timestamp_us: int) -> float:
        """Same as :meth:`unwrap`, in seconds since the first timestamp seen."""
        return self.unwrap(timestamp_us) / 1e6

    def __call__(self, timestamp_us: int) -> int:
        """Shorthand for :meth:`unwrap`."""
        return self.unwrap(timestamp_us)

    @property
    def wraps(self) -> int:
        """How many times the counter has wrapped so far."""
        return self._wraps
