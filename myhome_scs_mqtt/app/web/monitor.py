"""The bus monitor of the web interface: the last frames, kept readable.

The monitor exists to find addresses - press a button, read its address.  It
must not drown in the add-on's own polling, so the gateway clock is left out
and a frame that repeats right away is counted instead of listed again.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Callable

from ..own import messages as msg
from ..scan import classify

MAX_FRAMES = 200
NEW_MARK = "  (neu)"


def _wall_clock() -> str:
    return time.strftime("%H:%M:%S")


class BusMonitor:
    """The last :data:`MAX_FRAMES` frames of the bus for the web interface."""

    def __init__(self, known: Callable[[str, str], bool], size: int = MAX_FRAMES,
                 now: Callable[[], str] = _wall_clock) -> None:
        """``known(section, where)`` tells whether a device already exists."""
        self._known = known
        self._now = now
        self._frames: deque[dict[str, Any]] = deque(maxlen=size)

    def record(self, message: msg.OwnMessage) -> None:
        """Keep a frame that came from the bus."""
        if message.is_reply or message.who == msg.WHO_GATEWAY:
            return
        if self._frames and self._frames[-1]["frame"] == message.raw:
            self._frames[-1]["count"] += 1
            return
        hint = ""
        classified = classify(message)
        if classified is not None:
            hint = f"{classified.section} {classified.where}"
            if not self._known(classified.section, classified.where):
                hint += NEW_MARK
        self._frames.append({"frame": message.raw, "hint": hint,
                             "time": self._now(), "count": 1})

    def frames(self) -> list[dict[str, Any]]:
        """Oldest first; copies, so a reader cannot change the monitor."""
        return [dict(entry) for entry in self._frames]
