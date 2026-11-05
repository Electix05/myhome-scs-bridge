"""What runs in the background of the bridge.

* :class:`Outbox` - two lanes in one queue.  What a person just asked for goes
  first; the periodic refresh waits.  Sharing one lane let a command sit behind
  fifty status requests - measured at 42 seconds.
* Each lane has its own sender, and it hands its lane to the gateway one frame
  at a time.  How fast frames may leave - hard ceiling, command reserve and
  adaptive brake - is decided by :class:`~..own.gateway.BusPacing` alone; the
  sender only says which lane a frame comes from and which frames are probes,
  whose refusal is an answer, not overload (CHANGELOG 1.12.1).
* The refresh asks every device for its state, one frame every REFRESH_PACE
  seconds: a burst of fifty requests is what makes a busy gateway refuse.
* The poller repeats the refresh every ``poll_interval`` seconds.
* The ticker calls the entities that need it once a second and samples the
  bus load.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..own import messages as msg
from ..own.gateway import RepeatLimiter
from .registry import Registry

_LOGGER = logging.getLogger(__name__)

REFRESH_PACE = 0.5
# A refresh round spreads over half the poll interval, but never slower than
# this - otherwise a small installation with a huge interval would trickle its
# round over an hour and nothing would ever look up to date.
MAX_REFRESH_PACE = 5.0
TICK_INTERVAL = 1.0
LOAD_SAMPLES = 60
# Upper bound of the background lane.  Requests are never queued twice, so it
# only matters on a very large installation behind a very slow gateway.
MAX_BACKGROUND_FRAMES = 500

_REQUEST_KINDS = (msg.KIND_STATUS_REQUEST, msg.KIND_DIMENSION_REQUEST)
_ANSWERS = {True: "acknowledged", False: "refused", None: "no answer"}


class GatewayLink(Protocol):
    """The part of :class:`~..own.gateway.Gateway` the background needs."""

    connected: bool
    sent: int
    received: int
    throttled: int
    refused: int
    unanswered: int
    brake_gap: float

    async def send(self, frame: str, *, quiet: bool = False,
                   background: bool = False) -> bool | None: ...


@dataclass(frozen=True)
class QueuedFrame:
    frame: str
    probe: bool = False
    background: bool = False
    # False for the second and later question about the same address within one
    # refresh round (see refresh_all): only the first one counts towards
    # silencing that address.  A heating zone alone asks up to six different
    # things per round, and a busy gateway refuses "in rows" (WISSEN.md
    # "Buslast") - three of those six must not look like a dead zone.
    countable: bool = True
    # When the frame was queued (monotonic clock) - for the latency in the log.
    queued_at: float = field(default=0.0, compare=False)


class Outbox:
    """Frames waiting for the bus, in two lanes."""

    def __init__(self, max_background: int = MAX_BACKGROUND_FRAMES,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._foreground: deque[QueuedFrame] = deque()
        self._background: deque[QueuedFrame] = deque()
        self._max_background = max_background
        self._clock = clock
        # One signal per lane: each lane has its own sender, so a command is
        # never stuck behind a refresh frame that waits for the brake.
        self._waiting = {False: asyncio.Event(), True: asyncio.Event()}
        self._log = RepeatLimiter(_LOGGER)
        self.dropped = 0

    def qsize(self, background: bool | None = None) -> int:
        """How many frames wait: in one lane, or in both."""
        if background is None:
            return len(self._foreground) + len(self._background)
        return len(self._background if background else self._foreground)

    def put(self, frame: str, probe: bool = False, background: bool = False,
            countable: bool = True) -> None:
        if self._is_request(frame) and self._already_queued(frame, background):
            return
        item = QueuedFrame(frame, probe, background, countable, self._clock())
        if background:
            if len(self._background) >= self._max_background:
                lost = self._background.popleft()
                self.dropped += 1
                self._log.log("dropped", logging.WARNING,
                              "the gateway cannot keep up - dropping queued request %s",
                              lost.frame)
            self._background.append(item)
        else:
            # Commands and what entities ask for are never dropped.
            self._foreground.append(item)
        self._waiting[background].set()

    @staticmethod
    def _is_request(frame: str) -> bool:
        return msg.parse(frame).kind in _REQUEST_KINDS

    def _already_queued(self, frame: str, background: bool) -> bool:
        """A request that already waits need not wait twice.

        While the gateway is away, sensors and shutters keep asking every few
        seconds; without this, thousands of stale requests would flood the bus
        once it is back.  A foreground request takes the place of an equal one
        in the background lane, so a drive is not held up by the refresh.
        """
        if any(item.frame == frame for item in self._foreground):
            return True
        for item in self._background:
            if item.frame == frame:
                if background:
                    return True
                self._background.remove(item)
                return False
        return False

    def clear_background(self) -> int:
        """Throw the queued requests away; returns how many there were.

        Used when a bus scan ends: the refresh frames that piled up meanwhile
        are minutes old, and the scan has just asked every device anyway.
        Measured on 24.09.2026: after a scan 40 stale requests ran on for
        minutes.  Commands and what entities ask for are never touched.
        """
        count = len(self._background)
        self._background.clear()
        return count

    async def get(self, background: bool = False) -> QueuedFrame:
        """The next frame of one lane; waits while that lane is empty.

        Each lane is drained by its own sender.  Sharing one sender would undo
        the whole point of the two lanes: while it waits for the brake with a
        refresh frame in its hands, a command can only watch (measured
        24.09.2026 - switching took seconds while a refresh was braked).
        """
        lane = self._background if background else self._foreground
        while not lane:
            self._waiting[background].clear()
            await self._waiting[background].wait()
        return lane.popleft()


class BackgroundTasks:
    """Sender, refresh, poller and ticker of one bridge."""

    def __init__(
        self,
        gateway: GatewayLink,
        registry: Registry,
        *,
        poll_interval: float,
        auto_discover: Callable[[], bool] = lambda: True,
        is_scanning: Callable[[], bool] = lambda: False,
        outbox: Outbox | None = None,
        refresh_pace: float = REFRESH_PACE,
        tick_interval: float = TICK_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.gateway = gateway
        self.registry = registry
        self.outbox = outbox or Outbox(clock=clock)
        self.poll_interval = poll_interval
        self._auto_discover = auto_discover
        self._is_scanning = is_scanning
        self._refresh_pace = refresh_pace
        self._tick_interval = tick_interval
        self._clock = clock
        self._refresh_task: asyncio.Task[None] | None = None
        self._load: deque[tuple[float, int, int]] = deque(maxlen=LOAD_SAMPLES)
        self._log = RepeatLimiter(_LOGGER)

    # ------------------------------------------------------------------ api

    def queue_frame(self, frame: str, probe: bool = False, background: bool = False,
                    countable: bool = True) -> None:
        """Put a frame on the way to the bus (this is ``Context.send``)."""
        self.outbox.put(frame, probe=probe, background=background, countable=countable)

    def queue_depth(self) -> int:
        return self.outbox.qsize()

    def drop_queued_requests(self) -> int:
        """Forget the queued requests (after a bus scan); returns how many."""
        dropped = self.outbox.clear_background()
        if dropped:
            _LOGGER.info("%d queued requests dropped - they are out of date", dropped)
        return dropped

    async def run(self) -> None:
        """Run sender, poller and ticker until cancelled."""
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._sender(False), name="bridge-sender")
                group.create_task(self._sender(True), name="bridge-sender-background")
                group.create_task(self._poller(), name="bridge-poller")
                group.create_task(self._ticker(), name="bridge-ticker")
        finally:
            await self.stop_refresh()

    def start_refresh(self) -> None:
        """Ask every device for its state - unless a refresh is still running."""
        if self._refresh_task is not None and not self._refresh_task.done():
            _LOGGER.debug("A refresh is still running - skipping this round")
            return
        self._refresh_task = asyncio.create_task(self.refresh_all(), name="bridge-refresh")
        self._refresh_task.add_done_callback(self._refresh_finished)

    @property
    def refreshing(self) -> bool:
        return self._refresh_task is not None and not self._refresh_task.done()

    async def stop_refresh(self) -> None:
        task, self._refresh_task = self._refresh_task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def refresh_pace(self, frames: int) -> float:
        """Seconds between two frames of a refresh round.

        One round should spread over at most half the poll interval, so the
        bus stays quiet for the other half.  Measured on 24.09.2026: 40
        devices made 68 requests, and at the fixed pace of 0.5 s they went out
        as a 34-second burst of two frames a second - no matter whether the
        interval was 60 or 300 seconds.  That burst is what showed up as "60
        sent, 180 received per minute" out of nowhere.

        Never faster than ``refresh_pace`` (the bus limit would only queue them
        up) and never slower than :data:`MAX_REFRESH_PACE`.
        """
        if frames <= 0 or self.poll_interval <= 0:
            return self._refresh_pace
        half = 0.5 * self.poll_interval / frames
        return min(MAX_REFRESH_PACE, max(self._refresh_pace, half))

    async def refresh_all(self) -> None:
        """Queue every poll frame, spread over half the poll interval."""
        # (frame, countable): only the first question about an address may
        # silence it, see QueuedFrame.countable.
        frames: list[tuple[str, bool]] = []
        if self._auto_discover():
            # A general status request makes every lighting actuator answer,
            # which feeds the auto discovery on a fresh installation.
            frames.append((msg.status_request(msg.WHO_LIGHTING, "0"), True))
        probes: set[str] = set()
        for entity in list(self.registry.entities):
            try:
                if not entity.poll_due():
                    continue
                entity_frames = list(entity.poll_frames())
                if entity.silenced:
                    # A silenced address gets one single knock, not a round.
                    entity_frames = entity_frames[:1]
                entity_probes = set(entity.probe_frames())
            except Exception:  # noqa: BLE001 - one broken entity must not stop the refresh of the rest
                self._log.log(f"poll {entity.object_id}", logging.ERROR,
                              "cannot collect the poll frames of %s", entity.name, exc_info=True)
                continue
            if entity.silenced and entity_frames:
                # Book the single revival knock only now that there is a frame
                # for it; poll_due() stays a plain yes or no.
                entity.note_poll_attempt()
            # Both or nothing: probes without their poll frames mean nothing.
            frames.extend((frame, index == 0) for index, frame in enumerate(entity_frames))
            probes.update(entity_probes)
        pace = self.refresh_pace(len(frames))
        _LOGGER.debug("Refreshing %d frames, one every %.2fs (about %.0f seconds)",
                      len(frames), pace, len(frames) * pace)
        if frames and pace * len(frames) > 0.5 * self.poll_interval > 0:
            # The floor binds: the round cannot be stretched any further
            # without falling behind the interval.
            self._log.log("pace", logging.INFO,
                          "%d requests per round need %.0fs at %.1f frames a second - "
                          "that is more than half of poll_interval (%ds). A longer "
                          "interval would keep the bus quieter.",
                          len(frames), len(frames) * pace, 1 / pace, self.poll_interval)
        for frame, countable in frames:
            self.queue_frame(frame, probe=frame in probes, background=True,
                             countable=countable)
            await asyncio.sleep(pace)

    def bus_load(self) -> dict[str, float | int]:
        """How much traffic the add-on itself puts on the bus."""
        rate_out = rate_in = 0.0
        if len(self._load) >= 2:
            first, last = self._load[0], self._load[-1]
            span = last[0] - first[0]
            if span > 0:
                rate_out = (last[1] - first[1]) / span
                rate_in = (last[2] - first[2]) / span
        return {
            "sent": self.gateway.sent,
            "received": self.gateway.received,
            "sent_per_minute": round(rate_out * 60, 1),
            "received_per_minute": round(rate_in * 60, 1),
            "queue": self.outbox.qsize(),
            "throttled": self.gateway.throttled,
            "refused": self.gateway.refused,
            "unanswered": self.gateway.unanswered,
            "brake_gap": round(self.gateway.brake_gap, 2),
            "dropped": self.outbox.dropped,
        }

    # ---------------------------------------------------------------- loops

    def _refresh_finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("The refresh failed", exc_info=task.exception())

    async def _sender(self, background: bool) -> None:
        # One sender per lane.  No pause in here: the gateway's BusPacing
        # decides how fast frames leave, for this queue and the bus scan alike.
        while True:
            item = await self.outbox.get(background)
            picked = self._clock()
            try:
                # The lane travels with the frame: only background traffic
                # waits for the brake and for the command reserve.
                result = await self.gateway.send(item.frame, quiet=item.probe,
                                                 background=item.background)
            except Exception:  # noqa: BLE001 - outermost loop: the queue must keep moving
                _LOGGER.exception("Unexpected error while sending %s", item.frame)
                continue
            self._book_answer(item, result)
            # The latency from a click to the bus, split into waiting in the
            # queue and waiting for the bus limit, brake and answer.
            _LOGGER.debug("sent %s after %.2fs in the queue, %.2fs until: %s",
                          item.frame, picked - item.queued_at, self._clock() - picked,
                          _ANSWERS.get(result, result))

    def _book_answer(self, item: QueuedFrame, result: bool | None) -> None:
        """Tell the entity of that address how the gateway answered.

        A refusal counts against the address: after a few of them in a row it
        is silenced instead of asked again in every round (Entity.note_refusal).
        An acknowledgement clears the count.  Three cases deliberately book
        nothing:

        * **Silence** says nothing about the address - then the link is in
          trouble, not the device.
        * The refusal of a **probe** is an answer ("this zone has no fan").
        * The second and later question of one round (``countable``): a busy
          gateway refuses in rows, and three of a heating zone's six questions
          are no proof that the zone is gone.

        An address two entities share on purpose (a BMSE sensor next to a
        light point, ``exclusive_address = False``) is left out of the
        silencing altogether: from the address alone the two cannot be told
        apart, and a presence sensor that stops answering must not take the
        lamp next to it down with it.
        """
        if result is None or (not result and (item.probe or not item.countable)):
            return
        targets = self.registry.for_frame(item.frame)
        if len(targets) != 1:
            return
        entity = targets[0]
        try:
            if result:
                entity.note_alive()
            else:
                entity.note_refusal()
        except Exception:  # noqa: BLE001 - one broken entity must not stop the queue
            self._log.log(f"book {entity.object_id}", logging.ERROR,
                          "cannot book the answer to %s for %s", item.frame, entity.name,
                          exc_info=True)

    async def _poller(self) -> None:
        if self.poll_interval <= 0:
            return
        while True:
            await asyncio.sleep(self.poll_interval)
            if self.gateway.connected and not self._is_scanning():
                self.start_refresh()

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self._tick_interval)
            self._load.append((self._clock(), self.gateway.sent, self.gateway.received))
            for entity in self.registry.ticking():
                try:
                    entity.tick()
                except Exception:  # noqa: BLE001 - one broken entity must not stop the others
                    self._log.log(f"tick {entity.object_id}", logging.ERROR,
                                  "tick failed for %s", entity.name, exc_info=True)
