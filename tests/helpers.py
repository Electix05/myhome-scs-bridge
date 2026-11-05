"""Small helpers shared by the test modules."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from myhome_scs_mqtt.app.devices.base import Context
from myhome_scs_mqtt.app.own import messages as msg


async def wait_until(predicate: Callable[[], object], timeout: float = 5.0,
                     interval: float = 0.02) -> bool:
    """Poll ``predicate`` until it is truthy; False when the time runs out."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if predicate():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)


class FakeClock:
    """A monotonic clock that only moves when the test says so."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass
class ScheduledCall:
    due: float
    callback: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class Recorder:
    """A Context for one entity under test that records everything it does.

    Time only moves with :meth:`advance`, which also runs the callbacks that
    became due - no sleeping, no event loop.
    """

    clock: FakeClock = field(default_factory=FakeClock)
    published: list[tuple[str, str, bool]] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    rediscovered: list[object] = field(default_factory=list)
    scheduled: list[ScheduledCall] = field(default_factory=list)
    depth: int = 0

    def context(self) -> Context:
        return Context(
            base_topic="myhome",
            discovery_prefix="homeassistant",
            availability_topic="myhome/bridge/availability",
            publish=self._publish,
            send=self.sent.append,
            rediscover=self.rediscovered.append,
            queue_depth=lambda: self.depth,
            call_later=self._call_later,
            bridge_availability="myhome/bridge/lwt",
            now=self.clock,
        )

    def _publish(self, topic: str, payload: str, retain: bool = True, qos: int = 0) -> None:
        self.published.append((topic, payload, retain))

    def _call_later(self, delay: float, callback: Callable[[], None]) -> ScheduledCall:
        call = ScheduledCall(self.clock() + delay, callback)
        self.scheduled.append(call)
        return call

    def advance(self, seconds: float) -> None:
        """Move the clock and run what became due, in order."""
        target = self.clock() + seconds
        while True:
            due = sorted((c for c in self.scheduled if not c.cancelled and c.due <= target),
                         key=lambda c: c.due)
            if not due:
                break
            call = due[0]
            self.scheduled.remove(call)
            self.clock.value = max(self.clock.value, call.due)
            call.callback()
        self.clock.value = target

    def last(self, topic: str) -> str | None:
        for published_topic, payload, _retain in reversed(self.published):
            if published_topic == topic:
                return payload
        return None

    def retained(self, topic: str) -> bool | None:
        for published_topic, _payload, retain in reversed(self.published):
            if published_topic == topic:
                return retain
        return None

    def clear(self) -> None:
        self.published.clear()
        self.sent.clear()


def frame(raw: str) -> msg.OwnMessage:
    """Shorthand for a parsed frame in device tests."""
    return msg.parse(raw)
