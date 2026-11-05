"""Supervises the two sessions that make up a working OpenWebNet link.

An OpenWebNet gateway needs one *event* session that hears every state change
on the SCS bus, and one *command* session for our own requests - one session
cannot do both, and an MH201 even answers requests on the event session only.
Both are watched here: they reconnect on their own with a growing delay, and a
periodic keepalive stops the gateway from closing an idle command session.

How fast frames may go out is decided in one place, :class:`BusPacing`, for
every sender alike - commands, refresh, bus scan and keepalive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Awaitable, Callable, NamedTuple

from . import messages as msg
from .session import COMMAND_TIMEOUT, OwnAuthError, OwnError, OwnSession

_LOGGER = logging.getLogger(__name__)

RECONNECT_MIN = 5.0
RECONNECT_MAX = 60.0
# A gateway closes a command session after about 30 seconds without traffic -
# measured: silent 31 s, one request every 20 s stable for good.  At 30 s the
# keepalive races the timeout and the link broke every minute.
KEEPALIVE_INTERVAL = 20.0
# WHO=13 dimension 0 asks the gateway for its time.  Gateways that do not
# implement it answer with a NACK, which proves just as well that they live.
KEEPALIVE_FRAME = msg.dimension_request(msg.WHO_GATEWAY, "", msg.DIM_GATEWAY_TIME)

# Hard ceiling for everything this add-on puts on the bus: five frames per
# second, short peaks of up to ten so a single command never waits.  Under load
# a gateway refuses requests in rows; fifty status requests at once are enough.
MAX_FRAMES_PER_SECOND = 5.0
BUCKET_SIZE = 10.0

# Frames per second kept free for what a person just asked for.  Our own
# housekeeping - refresh, probes and the bus scan - may only draw
# MAX_FRAMES_PER_SECOND minus this reserve, so a command still gets through
# while a scan runs.  Measured on 24.09.2026 (berichte/22-messung-buslast.md):
# during a scan thirteen switch attempts were made and every one was refused.
COMMAND_RESERVE = 1.5

# The adaptive brake (WISSEN.md "Buslast": Drosselung bei Ablehnung).  It only
# engages once troubles pile up: BRAKE_THRESHOLD refusals or silences within
# BRAKE_WINDOW seconds, and refusals only count when they come from more than
# one address.  An address that does not exist is refused on every refresh; in
# the old add-on that alone slowed everything down for minutes (measured
# 24.09.2026).  A refused probe never counts - it is an answer.
# 3 troubles in 3 seconds is a provisional starting value (decided on
# 17.09.2026), adjustable in the add-on options; the final value is set after
# the measurement on real hardware.
BRAKE_THRESHOLD = 3
BRAKE_WINDOW = 3.0
# Every further trouble widens the gap between two frames by BRAKE_STEP, up to
# BRAKE_MAX; every acknowledged frame narrows it by BRAKE_RELIEF again.  On top
# of that the gap halves after every trouble-free BRAKE_WINDOW: with the relief
# alone it took twenty acknowledged frames to get out of a 2 s gap, which at
# two frames a second is minutes (measured after a scan: five minutes of
# aftermath).  Below BRAKE_FLOOR the brake is simply released.
BRAKE_STEP = 0.5
BRAKE_RELIEF = 0.1
BRAKE_MAX = 2.0
BRAKE_FLOOR = 0.05

# Waiting less than this is pointless and dangerous: a token count of
# 0.9999999999999998 would otherwise ask for a wait of 4e-17 seconds, which no
# clock can move on - the sender would spin without ever getting through.
MIN_WAIT = 1e-6

# How long send() waits for a command session before it gives the frame up.
WAIT_READY = 15.0
# How long a background frame may wait for its ACK/NACK.  Much shorter than
# COMMAND_TIMEOUT on purpose: OpenWebNet has no frame id, so one command
# session can only carry one exchange at a time (own/session.py, request()
# holds the send lock until the answer arrives).  A background frame the
# gateway never answers would otherwise block a command for the full six
# seconds - exactly the delay this version set out to remove.  Two seconds are
# generous for an acknowledgement (the bus carries about 1.4 frames per second
# at rest, WISSEN.md "Buslast"); what stays unanswered that long is booked as
# trouble anyway.
BACKGROUND_TIMEOUT = 2.0
# A problem that persists is repeated in the log at most this often.
LOG_REPEAT_INTERVAL = 600.0

Sleep = Callable[[float], Awaitable[None]]


class _Trouble(NamedTuple):
    """One refused or unanswered frame: when, for whom, and which of the two."""

    at: float
    address: str
    silent: bool


def frame_source(frame: str) -> str:
    """Which device a frame is addressed to: ``"WHO:WHERE"``, or "" if unknown.

    Used to tell "this one address does not exist" from "the bus cannot keep
    up".  The two address forms of a heating zone (``#8`` and ``8``) stay
    distinct on purpose - they are two different attempts.
    """
    message = msg.parse(frame)
    if not message.who or not message.full_where:
        return ""
    return f"{message.who}:{message.full_where}"


class BusPacing:
    """How fast this add-on may put frames on the bus - one place for every sender.

    Three rules from WISSEN.md "Buslast" work together:

    * **Hard ceiling:** at most ``rate`` frames per second, bursts of up to
      ``size`` so that a single command never waits (token bucket).  It holds
      whatever comes together - commands, refresh, scan and keepalive.
    * **Command reserve:** background traffic (refresh, probes, bus scan) draws
      from a second, slower bucket of ``rate - reserve`` frames per second.
      Whatever our own housekeeping does, ``reserve`` frames per second stay
      free for what a person just asked for.
    * **Adaptive brake:** once ``threshold`` frames within ``window`` seconds
      went unanswered or were refused **and** the troubles do not all come from
      one single address, the minimum gap between two frames grows by ``step``
      with every further trouble, up to ``max_gap``.  Every acknowledged frame
      narrows it by ``relief``, and it halves with every trouble-free
      ``window``.  A refused probe is an answer, not overload, and never
      counts; silence always does.
      **The brake holds back background traffic only** - it protects the bus
      from our own load, and that is never a reason to delay a key press
      (measured 24.09.2026: one address that does not exist made switching a
      lamp take seconds).

    The stricter rule wins - the waits do not add up.  The lock is held for the
    bookkeeping only, not while waiting: a command must be able to slip past a
    background frame that waits for the brake.
    """

    def __init__(self, rate: float = MAX_FRAMES_PER_SECOND, size: float = BUCKET_SIZE, *,
                 reserve: float = COMMAND_RESERVE,
                 threshold: int = BRAKE_THRESHOLD, window: float = BRAKE_WINDOW,
                 step: float = BRAKE_STEP, relief: float = BRAKE_RELIEF,
                 max_gap: float = BRAKE_MAX,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Sleep = asyncio.sleep) -> None:
        if rate <= 0 or size < 1:
            raise ValueError("the bus limit needs a positive rate and room for one frame")
        if threshold < 1 or window <= 0 or step < 0 or relief < 0 or max_gap < 0:
            raise ValueError("the brake needs a threshold of at least 1, a positive "
                             "window and no negative steps")
        if reserve < 0 or reserve >= rate:
            raise ValueError("the command reserve must leave the background some room")
        self.rate = rate
        self.size = size
        self.reserve = reserve
        self.threshold = threshold
        self.window = window
        self.step = step
        self.relief = relief
        self.max_gap = max_gap
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._tokens = size
        # The background bucket keeps the same ratio of burst to rate as the
        # main one, so a quiet minute still lets a refresh start briskly.
        self._background_rate = rate - reserve
        self._background_size = max(1.0, size * self._background_rate / rate)
        self._background_tokens = self._background_size
        self._updated = clock()
        self._last_release: float | None = None
        # Only the last `threshold` troubles matter: they either fit into the
        # window or not.  Bounded, however long a trouble lasts.
        self._troubles: deque[_Trouble] = deque(maxlen=threshold)
        self._gap = 0.0
        # When the last trouble was booked - the brake fades from here on.
        self._quiet_since = clock()
        self._log = RepeatLimiter(_LOGGER, clock=clock)
        # Diagnostics, counted since the start of the add-on.
        self.throttled = 0          # frames the hard ceiling held back
        self.refused = 0            # refusals that were no probe
        self.unanswered = 0         # frames without any answer

    @property
    def gap(self) -> float:
        """The current minimum pause between two frames; 0 while all is well.

        Reading it brings the fading up to date (see :meth:`_decay`), so the
        diagnostics never show a brake that has long since let go.
        """
        self._decay()
        return self._gap

    def _decay(self) -> None:
        """Let the brake fade: it halves with every trouble-free window.

        With the relief alone (0.1 s per acknowledged frame) it took twenty
        frames to get out of a 2 s gap - at two frames a second that is minutes
        (measured after a bus scan: five minutes of aftermath).  Halving is
        bounded: from 2 s it takes six windows to fall below BRAKE_FLOOR.
        """
        if self._gap <= 0:
            return
        while self._clock() - self._quiet_since >= self.window:
            self._quiet_since += self.window
            self._gap = round(self._gap / 2, 6)
            if self._gap < BRAKE_FLOOR:
                self._gap = 0.0
                _LOGGER.debug("no trouble for %.1fs - brake released", self.window)
                return

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        self._updated = now
        self._tokens = min(self.size, self._tokens + elapsed * self.rate)
        self._background_tokens = min(
            self._background_size,
            self._background_tokens + elapsed * self._background_rate)

    @staticmethod
    def _bucket_wait(tokens: float, rate: float) -> float:
        """How long until this bucket holds one whole token again."""
        return (1.0 - tokens) / rate if tokens < 1.0 else 0.0

    def _brake_wait(self) -> float:
        if self._gap <= 0 or self._last_release is None:
            return 0.0
        return self._last_release + self._gap - self._clock()

    async def take(self, background: bool = False) -> None:
        """Wait until the bus may carry one more frame.

        ``background`` marks our own housekeeping - refresh, probes and the bus
        scan.  Only those wait for the adaptive brake and for the reserve that
        is kept free for commands; a command from Home Assistant waits for the
        hard ceiling alone.
        """
        counted = False
        while True:
            async with self._lock:
                self._refill()
                self._decay()
                ceiling = self._bucket_wait(self._tokens, self.rate)
                wait = ceiling
                if background:
                    wait = max(wait, self._brake_wait(),
                               self._bucket_wait(self._background_tokens,
                                                 self._background_rate))
                if wait <= MIN_WAIT:
                    self._tokens -= 1.0
                    if background:
                        self._background_tokens -= 1.0
                    self._last_release = self._clock()
                    return
                if ceiling > MIN_WAIT and not counted:
                    # Diagnostics: only the hard ceiling counts as throttled -
                    # the brake and the reserve are deliberate pauses.
                    self.throttled += 1
                    counted = True
                _LOGGER.debug("holding back %.2fs (%s)", wait,
                              "background" if background else "command")
            # Outside the lock on purpose: whoever waits here must not keep a
            # command from being booked in the meantime.
            await self._sleep(wait)

    def report(self, result: bool | None, probe: bool = False, address: str = "") -> None:
        """Book how the gateway answered a frame.

        ``True`` = acknowledged, ``False`` = refused, ``None`` = no answer.
        ``probe`` marks frames whose refusal is an answer ("this zone has no
        fan"); their silence still counts as trouble.  ``address`` says which
        device the frame was meant for, so that refusals of one single address
        can be told apart from a bus that cannot keep up (see :meth:`_overload`).
        """
        self._decay()
        if result:
            if self._gap > 0:
                self._gap = max(0.0, round(self._gap - self.relief, 6))
                if self._gap == 0:
                    _LOGGER.debug("the gateway keeps up again - brake released")
            return
        if result is None:
            self.unanswered += 1
            reason = "no answer"
            silent = True
        elif probe:
            return
        else:
            self.refused += 1
            reason = "refused"
            silent = False

        now = self._clock()
        self._quiet_since = now
        self._troubles.append(_Trouble(now, address, silent))
        if len(self._troubles) < self.threshold or now - self._troubles[0].at > self.window:
            return
        if not self._overload():
            _LOGGER.debug("%d refusals, all of %s - that address's problem, not the bus's",
                          len(self._troubles), address or "one address")
            return
        before = self._gap
        self._gap = min(self.max_gap, round(self._gap + self.step, 6))
        if self._gap <= before:
            return
        if before == 0:
            self._log.log("busy", logging.INFO,
                          "gateway is busy (%d troubles within %.1fs, last: %s) - slowing down",
                          self.threshold, self.window, reason)
        _LOGGER.debug("brake - %.1fs between two frames after: %s", self._gap, reason)

    def _overload(self) -> bool:
        """Do the troubles in the window look like a bus that cannot keep up?

        Silence always does - that is the honest overload signal (WISSEN.md
        "Buslast").  Refusals only count when they come from at least two
        different addresses: an address that does not exist is refused in every
        single round, and on 24.09.2026 exactly one of them was enough to put
        the brake at its maximum and make switching a lamp take seconds.  An
        unknown address cannot be recognised as "the same one" and therefore
        counts on its own.
        """
        sources = set()
        unknown = 0
        for trouble in self._troubles:
            if trouble.silent:
                return True
            if trouble.address:
                sources.add(trouble.address)
            else:
                unknown += 1
        return len(sources) + unknown >= 2

    def reset(self) -> None:
        """A new link starts without brake; the counters stay."""
        if self._gap > 0:
            _LOGGER.debug("new gateway link - brake released")
        self._gap = 0.0
        self._troubles.clear()
        self._quiet_since = self._clock()


class RepeatLimiter:
    """Logs a recurring problem once, then at most every ``interval`` seconds.

    While a gateway is switched off, every reconnect attempt fails the same
    way.  One line per attempt would fill the log within a day; the first line
    and a reminder with the number of repeats say the same.
    """

    def __init__(self, logger: logging.Logger, interval: float = LOG_REPEAT_INTERVAL,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._logger = logger
        self._interval = interval
        self._clock = clock
        self._last: dict[str, float] = {}
        self._repeats: dict[str, int] = {}

    def log(self, key: str, level: int, message: str, *args: object,
            exc_info: bool = False) -> None:
        """Log loudly or, within the interval, at debug level.

        ``exc_info`` adds the traceback to the loud message only.
        """
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            self._repeats[key] = self._repeats.get(key, 0) + 1
            self._logger.debug(message, *args)
            return
        repeats = self._repeats.pop(key, 0)
        if repeats:
            message += " (%d more times since the last message)"
            args = (*args, repeats)
        self._last[key] = now
        self._logger.log(level, message, *args, exc_info=exc_info)

    def reset(self, key: str) -> None:
        """The problem is gone: the next occurrence is reported right away."""
        self._last.pop(key, None)
        self._repeats.pop(key, None)


class Gateway:
    """Keeps the event and command sessions alive and exposes ``send``."""

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        auth_mode: str = "auto",
        on_message: Callable[[msg.OwnMessage], None] | None = None,
        on_connected: Callable[[bool], None] | None = None,
        *,
        reconnect_min: float = RECONNECT_MIN,
        reconnect_max: float = RECONNECT_MAX,
        keepalive_interval: float = KEEPALIVE_INTERVAL,
        keepalive_timeout: float = COMMAND_TIMEOUT,
        pacing: BusPacing | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """``sleep`` waits between reconnect attempts; tests record the delays."""
        self.host = host
        self.port = port
        self._on_message = on_message or (lambda _message: None)
        self._on_connected = on_connected or (lambda _state: None)
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self._keepalive_interval = keepalive_interval
        self._keepalive_timeout = keepalive_timeout
        self._pacing = pacing or BusPacing()
        self._sleep = sleep

        self._event = OwnSession(host, port, password, msg.SESSION_EVENT,
                                 self._handle_message, auth_mode, name="event")
        self._command = OwnSession(host, port, password, msg.SESSION_COMMAND,
                                   self._handle_message, auth_mode, name="command")
        self._tasks: list[asyncio.Task[None]] = []
        self._command_ready = asyncio.Event()
        self._connected = False
        self._stopping = False
        self._log = RepeatLimiter(_LOGGER)
        self.last_error = ""
        self.sent = 0
        self.received = 0

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Start supervising both sessions; returns at once."""
        if self._tasks:
            return
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._supervise(self._event), name="own-event"),
            asyncio.create_task(self._supervise(self._command), name="own-command"),
            asyncio.create_task(self._keepalive(), name="own-keepalive"),
        ]

    async def stop(self) -> None:
        """Cancel the supervisors and close both sessions."""
        self._stopping = True
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._event.close()
        await self._command.close()
        self._command_ready.clear()
        self._set_connected(False)

    @property
    def connected(self) -> bool:
        """True while both sessions are logged in."""
        return self._connected

    @property
    def throttled(self) -> int:
        """How often the hard bus limit held a frame back."""
        return self._pacing.throttled

    @property
    def refused(self) -> int:
        """Frames the gateway refused - refused probes not counted."""
        return self._pacing.refused

    @property
    def unanswered(self) -> int:
        """Frames the gateway did not answer at all."""
        return self._pacing.unanswered

    @property
    def brake_gap(self) -> float:
        """The pause the adaptive brake currently keeps between two frames."""
        return self._pacing.gap

    # ------------------------------------------------------------ supervisor

    async def _supervise(self, session: OwnSession) -> None:
        delay = self._reconnect_min
        while not self._stopping:
            try:
                await session.connect()
                delay = self._reconnect_min
                if session is self._command:
                    self._command_ready.set()
                self._update_connected()
                await session.run()
            except OwnAuthError as err:
                # Credentials will not fix themselves; keep retrying slowly so
                # the user can correct the add-on options without a restart.
                self.last_error = str(err)
                delay = self._reconnect_max
                self._log.log(f"{session.name} auth", logging.ERROR,
                              "[%s] authentication failed: %s - retrying in %.0fs",
                              session.name, err, delay)
            except (OwnError, OSError, TimeoutError) as err:
                self.last_error = str(err) or type(err).__name__
                self._log.log(f"{session.name} lost", logging.WARNING,
                              "[%s] connection lost: %s - retrying in %.0fs",
                              session.name, self.last_error, delay)
            except Exception:  # noqa: BLE001 - outermost loop: the link must survive a bug
                self.last_error = "unexpected error"
                _LOGGER.exception("[%s] unexpected error - retrying in %.0fs",
                                  session.name, delay)
            finally:
                if session is self._command:
                    self._command_ready.clear()
                await session.close()
                self._update_connected()

            if self._stopping:
                return
            await self._sleep(delay)
            delay = min(delay * 2, self._reconnect_max)

    def _update_connected(self) -> None:
        self._set_connected(self._event.connected and self._command.connected)

    def _set_connected(self, state: bool) -> None:
        if state == self._connected:
            return
        self._connected = state
        if state:
            # A rebooted gateway is not busy any more; the old brake must not
            # slow the fresh link down.
            self._pacing.reset()
            self._log.log("link up", logging.INFO, "gateway link is up (%s:%s)",
                          self.host, self.port)
        elif self._stopping:
            _LOGGER.debug("gateway link closed")
        else:
            self._log.log("link down", logging.WARNING, "gateway link is down")
        try:
            self._on_connected(state)
        except Exception:  # noqa: BLE001 - a broken callback must not stop the supervisor
            _LOGGER.exception("error in the link state callback")

    def _handle_message(self, message: msg.OwnMessage) -> None:
        self.received += 1
        self._on_message(message)

    async def _keepalive(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._keepalive_interval)
            if not self._command_ready.is_set():
                continue
            try:
                # Not background: one frame every 20 s is no load worth
                # braking, and a keepalive held back by the brake would cost
                # the command session (the gateway closes it after ~30 s).
                await self._pacing.take()
                self.sent += 1
                reply = await self._command.request(KEEPALIVE_FRAME, self._keepalive_timeout)
                # A gateway without a clock refuses the keepalive - an answer
                # just the same, so it is booked like a probe.
                self._pacing.report(None if reply is None else not reply.is_nack, probe=True)
            except (OwnError, OSError) as err:
                # The supervisor of the command session reports the break.
                _LOGGER.debug("keepalive could not be sent: %s", err)
                continue
            except Exception:  # noqa: BLE001 - outermost loop, like _supervise
                # A dead keepalive task would let the gateway drop the command
                # session every 30 seconds, without a word about why.
                _LOGGER.exception("unexpected error in the keepalive loop")
                continue
            if reply is None:
                self._log.log("keepalive", logging.WARNING,
                              "keepalive timed out - dropping the command session")
                await self._command.close()

    # --------------------------------------------------------------- sending

    async def _wait_ready(self, frame: str, wait_ready: float) -> bool:
        if self._command_ready.is_set():
            return True
        try:
            async with asyncio.timeout(wait_ready):
                await self._command_ready.wait()
        except TimeoutError:
            self._log.log("dropped", logging.WARNING,
                          "dropping %s - no command session available", frame)
            return False
        return True

    async def send(self, frame: str, wait_ready: float = WAIT_READY,
                   timeout: float | None = None, quiet: bool = False,
                   background: bool = False) -> bool | None:
        """Send one frame on the command session.

        ``True`` = acknowledged, ``False`` = refused, ``None`` = no answer or
        no link.  ``quiet`` marks frames whose refusal is an answer (probes).
        ``background`` marks our own housekeeping: only those frames wait for
        the adaptive brake and leave the command reserve alone, and they give
        up waiting for their answer sooner (:data:`BACKGROUND_TIMEOUT`) so a
        silent one cannot hold a command behind the session's send lock.
        ``timeout`` overrides that choice.
        """
        if not await self._wait_ready(frame, wait_ready):
            return None
        await self._pacing.take(background)
        self.sent += 1
        if timeout is None:
            timeout = BACKGROUND_TIMEOUT if background else COMMAND_TIMEOUT
        try:
            result = await self._command.send(frame, timeout, quiet)
        except (OwnError, OSError) as err:
            # A broken link is no sign of a busy bus: nothing is booked.
            self._log_send_failure(frame, err)
            await self._command.close()
            return None
        # With the address: refusals of one single address are that address's
        # problem, not a reason to slow the whole bus down.
        self._pacing.report(result, probe=quiet, address=frame_source(frame))
        return result

    async def post(self, frame: str, wait_ready: float = WAIT_READY,
                   background: bool = True) -> bool:
        """Send without waiting for the ACK - used by the bus scan.

        ``True`` when the frame went out, ``False`` when there was no link.
        The frame waits for the hard ceiling like every other one and, as
        background traffic, for the brake and the command reserve as well.  Its
        answer is not read here (:meth:`OwnSession.run` books the reply of a
        posted frame away), so a probe of an address that does not exist can
        never move the brake.
        """
        if not await self._wait_ready(frame, wait_ready):
            return False
        await self._pacing.take(background)
        self.sent += 1
        try:
            await self._command.post(frame)
        except (OwnError, OSError) as err:
            self._log_send_failure(frame, err)
            await self._command.close()
            return False
        return True

    def _log_send_failure(self, frame: str, err: Exception) -> None:
        if self._stopping:
            # A normal shutdown cut the exchange short - nothing is broken.
            _LOGGER.debug("send of %s cut short by shutdown: %s", frame, err)
        else:
            self._log.log("send failed", logging.WARNING, "could not send %s: %s", frame, err)
