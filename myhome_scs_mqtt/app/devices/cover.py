"""WHO=2 - automation: roller shutters, blinds and awnings.

Measured on real hardware (WISSEN.md "Rollläden" and "Wenn viele Rollläden
zugleich fahren"):

* Dimension 10 carries ``<state>*<position>*<priority>*<info>``; state 10 =
  stopped, 11 = moving up, 12 = moving down; position 0 = closed.
* The gateway never reports a position by itself - it only answers requests.
* A position command is accepted in exactly one form,
  ``*#2*<where>*#11#001#1*<value>##`` with the value *without* leading zeros,
  and the actuator then announces its arrival unasked.
* An acknowledgement proves nothing: one address acknowledged the frame and did
  not move.  If no movement is reported within NATIVE_GRACE seconds, the closed
  loop (start the motor, poll the position, stop on target) takes over.
* Reporting a position and accepting a position command are two different
  abilities and are handled separately.
* An end position costs one frame: the actuator stops there by itself.
* While more than four frames are queued, position polls are skipped - and a
  skipped poll must never count as a stalled drive.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from ..own import messages as msg
from .base import Cancellable, Context, Discovery, Entity, device_class, is_number

_LOGGER = logging.getLogger(__name__)

WHAT_STOP = "0"
WHAT_UP = "1"
WHAT_DOWN = "2"
# Basic actuators use WHAT 0/1/2; dimension 10 reports 10/11/12 instead.
STOP_CODES = (WHAT_STOP, "10")
UP_CODES = (WHAT_UP, "11")
DOWN_CODES = (WHAT_DOWN, "12")

DIM_SHUTTER_STATUS = "10"
# "Move to level" - the one form of five plausible ones that is accepted.
DIM_GOTO = "11#001#1"

# How long an actuator may stay silent after the command before the closed loop
# takes over - counted from the moment the frame left the queue, not from when
# it was queued.  NATIVE_LIMIT is the outer bound for a queue that never empties.
NATIVE_GRACE = 3.0
NATIVE_LIMIT = 30.0

# Closed loop positioning.  One or two percent of overshoot are normal.
POSITION_TOLERANCE = 1
STALL_TIMEOUT = 10.0
DRIVE_TIMEOUT = 180.0
PENDING_TIMEOUT = 3.0
# Above this many queued frames a position poll is skipped for one tick.
MAX_QUEUE_FOR_POLLING = 4

# Venetian blinds have no slat command on SCS: a short motor impulse turns
# them, a full turn takes about a second.
TILT_STEP_MIN = 0.1
TILT_SETTLE = 0.6
# How long to follow a movement somebody else started before giving up.
TRACKING_TIMEOUT = 180.0

POSITION_CONTROLS = ("auto", "native", "software", "off")

STATE_OPEN = "open"
STATE_CLOSED = "closed"
STATE_OPENING = "opening"
STATE_CLOSING = "closing"


def _parse_percent(payload: str) -> int | None:
    """'40', '40.0' -> 40, clamped to 0..100; None for anything else."""
    try:
        value = float(payload)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return max(0, min(100, int(value)))


class Cover(Entity):
    who = msg.WHO_AUTOMATION
    component = "cover"
    topic_group = "cover"
    section = "covers"
    default_name = "Cover"
    model = "Shutter actuator"
    needs_tick = True

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context, config)
        # Reporting and accepting turned out to go together on the measured
        # hardware, so the position command is the normal way; "advanced:
        # false" or "position_control: software" force the closed loop.
        self.accepts_position = bool(config.get("advanced", True))
        self.invert_position = bool(config.get("invert_position"))
        self.travel_time = float(config.get("travel_time") or 0)
        self.device_class = device_class(
            self.component, config.get("device_class", "shutter"), config.get("name", "")
        )
        self.state = STATE_OPEN
        self.position: float = 100 if not self.travel_time else 50
        self.reports_position = bool(self.travel_time > 0 or config.get("position_feedback"))
        self._native_target: int | None = None
        self._native_since = 0.0
        self._native_ordered = 0.0
        self._moving_since = 0.0
        self._moving_dir = 0
        self._target: int | None = None
        self._target_dir = 0
        self._target_started = 0.0
        self._last_progress = 0.0
        self._pending_target: int | None = None
        self._pending_since = 0.0
        self._tracking = False
        self._tracking_since = 0.0
        # Slat control for venetian blinds, opt-in via "tilt_time".
        self.tilt_time = float(config.get("tilt_time") or 0)
        self.tilt = int(config.get("tilt_start", 50))
        self._tilting = False
        self._tilt_jobs: list[Cancellable] = []
        mode = str(config.get("position_control", "auto")).lower()
        self._position_control = mode if mode in POSITION_CONTROLS else "auto"

    @property
    def supports_tilt(self) -> bool:
        return self.tilt_time > 0

    @property
    def can_set_position(self) -> bool:
        """Whether Home Assistant should offer a position slider."""
        if self._position_control == "off":
            return False
        if self._position_control == "native":
            return True
        return self.reports_position or self.travel_time > 0

    def to_config(self) -> dict[str, Any]:
        config = super().to_config()
        if self.tilt_time:
            config["tilt_time"] = self.tilt_time
        if self._position_control != "auto":
            config["position_control"] = self._position_control
        if self.reports_position and not self.travel_time:
            # Remember it: after a restart there is no scan to find it again.
            config["position_feedback"] = True
        if not self.accepts_position:
            config["advanced"] = False
        if self.travel_time:
            config["travel_time"] = self.travel_time
        if self.invert_position:
            config["invert_position"] = True
        return config

    # ------------------------------------------------------------- discovery

    @property
    def position_topic(self) -> str:
        return self.topic_root + "/position"

    @property
    def set_position_topic(self) -> str:
        return self.topic_root + "/position/set"

    @property
    def tilt_topic(self) -> str:
        return self.topic_root + "/tilt"

    @property
    def set_tilt_topic(self) -> str:
        return self.topic_root + "/tilt/set"

    def discovery(self) -> list[Discovery]:
        payload = self.base_payload()
        payload.update(
            {
                "state_topic": self.state_topic,
                "command_topic": self.command_topic,
                "payload_open": "OPEN",
                "payload_close": "CLOSE",
                "payload_stop": "STOP",
                "state_open": STATE_OPEN,
                "state_opening": STATE_OPENING,
                "state_closed": STATE_CLOSED,
                "state_closing": STATE_CLOSING,
                "optimistic": False,
            }
        )
        if self.device_class:
            payload["device_class"] = self.device_class
        if self.reports_position or self.can_set_position:
            payload.update({"position_topic": self.position_topic,
                            "position_open": 100, "position_closed": 0})
            if self.can_set_position:
                payload["set_position_topic"] = self.set_position_topic
        if self.supports_tilt:
            payload.update(
                {
                    "tilt_command_topic": self.set_tilt_topic,
                    "tilt_status_topic": self.tilt_topic,
                    "tilt_min": 0,
                    "tilt_max": 100,
                    "tilt_closed_value": 0,
                    "tilt_opened_value": 100,
                    "tilt_optimistic": False,
                }
            )
        return [(self.component, self.object_id, payload)]

    def subscriptions(self) -> tuple[str, ...]:
        # Always listen: whether the slider is offered can change at runtime,
        # and a subscription nobody publishes to costs nothing.
        return (self.command_topic, self.set_position_topic, self.set_tilt_topic)

    def poll_frames(self) -> tuple[str, ...]:
        # Dimension 10 also re-detects position feedback after a restart; a
        # basic actuator simply refuses it.
        status = self._status_request()
        if self.reports_position:
            return (status,)
        return (msg.status_request(self.who, self.where), status)

    def followup_frames(self) -> tuple[str, ...]:
        return () if self.reports_position else (self._status_request(),)

    def _status_request(self) -> str:
        return msg.dimension_request(self.who, self.where, DIM_SHUTTER_STATUS)

    # --------------------------------------------------------------- MQTT in

    def on_command(self, topic: str, payload: str) -> None:
        payload = payload.strip()
        if topic == self.set_tilt_topic:
            # Home Assistant sends the stop button of the slat row here, not to
            # the command topic.  Throwing it away left the motor running.
            if payload.upper() == "STOP":
                self.on_command(self.command_topic, "STOP")
                return
            tilt = _parse_percent(payload)
            if tilt is None:
                _LOGGER.warning("%s: not a tilt angle: %s", self.name, payload)
                return
            self.set_tilt(tilt)
            return
        if topic == self.set_position_topic:
            target = _parse_percent(payload)
            if target is None:
                _LOGGER.warning("%s: not a position: %s", self.name, payload)
                return
            self.set_position(target)
            return

        command = payload.upper()
        if command not in ("OPEN", "CLOSE", "STOP"):
            _LOGGER.warning("%s: unknown cover command %s", self.name, payload)
            return
        self._clear_drive()          # a manual command overrides a drive
        self._cancel_tilt()          # ... and a slat impulse that is still running
        what = {"OPEN": WHAT_UP, "CLOSE": WHAT_DOWN, "STOP": WHAT_STOP}[command]
        self.send(msg.command(self.who, what, self.where))

    def set_position(self, target: int) -> None:
        """Move to a position - natively if the actuator can, otherwise by hand."""
        target = max(0, min(100, int(target)))
        if self.accepts_position and self._position_control != "software":
            value = 100 - target if self.invert_position else target
            self._clear_drive()
            self._native_target = target
            self._native_since = self._native_ordered = self.ctx.now()
            # str(int) never has leading zeros - the measured form needs that.
            self.send(msg.dimension_write(self.who, self.where, DIM_GOTO, str(value)))
            return

        # The end positions need no supervision: one frame, the actuator stops.
        if target >= 100 - POSITION_TOLERANCE:
            self._clear_drive()
            self.send(msg.command(self.who, WHAT_UP, self.where))
            return
        if target <= POSITION_TOLERANCE:
            self._clear_drive()
            self.send(msg.command(self.who, WHAT_DOWN, self.where))
            return

        # Which way to go depends on where the shutter is *now*.  A stored
        # value can be minutes old - so ask first and start on the answer.
        self._pending_target = target
        self._pending_since = self.ctx.now()
        self.send(self._status_request())

    def set_tilt(self, target: int) -> None:
        """Turn the slats with a short impulse of the motor."""
        if not self.supports_tilt:
            _LOGGER.warning("%s: no tilt_time configured", self.name)
            return
        target = max(0, min(100, target))
        delta = target - self.tilt
        duration = abs(delta) / 100.0 * self.tilt_time
        if duration < TILT_STEP_MIN:
            self.tilt = target
            self.publish_state()
            return

        self._clear_drive()
        self._cancel_tilt()
        self._tilting = True
        self.tilt = target
        _LOGGER.debug("%s: turning the slats to %d%% (%.2fs impulse)",
                      self.name, target, duration)
        self.send(msg.command(self.who, WHAT_UP if delta > 0 else WHAT_DOWN, self.where))
        self._schedule(duration, self._end_tilt)
        self.publish_state()

    def _schedule(self, delay: float, callback) -> None:
        handle = self.ctx.call_later(delay, callback)
        if handle is not None:
            self._tilt_jobs.append(handle)

    def _cancel_tilt(self) -> None:
        """Drop a running impulse.

        Its scheduled stop belongs to a command that has been overtaken; left
        alone it fires later and halts whatever is running by then, which looks
        exactly like an actuator that ignores its inputs.
        """
        for job in self._tilt_jobs:
            job.cancel()
        self._tilt_jobs = []
        self._tilting = False

    def _end_tilt(self) -> None:
        self._tilt_jobs = []
        self.send(msg.command(self.who, WHAT_STOP, self.where))
        # An impulse moves the blind a little as well - find out how much.
        self._schedule(TILT_SETTLE, self._after_tilt)

    def _after_tilt(self) -> None:
        self._tilt_jobs = []
        self._tilting = False
        self.send(self._status_request())

    def _begin_drive(self, target: int) -> None:
        """Start a closed loop drive using a position we just confirmed."""
        current = self.position
        if abs(current - target) <= POSITION_TOLERANCE:
            self.publish_state()
            return
        now = self.ctx.now()
        upwards = target > current
        self._target = target
        self._target_dir = 1 if upwards else -1
        self._target_started = now
        self._last_progress = now
        _LOGGER.debug("%s: driving from %d%% to %d%%", self.name, current, target)
        self.send(msg.command(self.who, WHAT_UP if upwards else WHAT_DOWN, self.where))
        self.send(self._status_request())

    def _bus_congested(self) -> bool:
        """Skip a position poll when the bus is already busy."""
        if self.ctx.queue_depth() <= MAX_QUEUE_FOR_POLLING:
            return False
        _LOGGER.debug("%s: bus busy, skipping a position poll", self.name)
        return True

    def _clear_drive(self) -> None:
        self._target = None
        self._target_dir = 0
        self._pending_target = None
        self._native_target = None

    def _check_target(self) -> None:
        """Stop the motor once the requested position is reached."""
        if self._target is None:
            return
        self._last_progress = self.ctx.now()
        if self._target_dir > 0:
            reached = self.position >= self._target - POSITION_TOLERANCE
        else:
            reached = self.position <= self._target + POSITION_TOLERANCE
        if reached:
            _LOGGER.debug("%s: reached %d%% - stopping", self.name, self.position)
            self._target = None
            self._target_dir = 0
            self.send(msg.command(self.who, WHAT_STOP, self.where))

    # ---------------------------------------------------------------- OWN in

    def on_own(self, message: msg.OwnMessage) -> None:
        if message.kind == msg.KIND_NORMAL:
            self._apply_movement(message.what)
        elif (message.kind == msg.KIND_DIMENSION_RESPONSE
              and message.dimension == DIM_SHUTTER_STATUS):
            # An answer to dimension 10 proves the actuator knows where it is.
            self._enable_position_feedback()
            values = message.values
            if values:
                self._apply_movement(values[0])
            if len(values) > 1 and is_number(values[1]):
                level = max(0, min(100, int(values[1])))
                self.position = 100 - level if self.invert_position else level
                if (self._native_target is not None and self._moving_dir == 0
                        and abs(self.position - self._native_target) <= POSITION_TOLERANCE):
                    # Arrived.  A halt anywhere else belongs to a change of
                    # direction - an actuator asked to reverse stops first.
                    self._native_target = None
                if self._moving_dir == 0:
                    self.state = STATE_CLOSED if self.position == 0 else STATE_OPEN
                if self._pending_target is not None:
                    target, self._pending_target = self._pending_target, None
                    self._begin_drive(target)
                else:
                    self._check_target()
        else:
            return
        self.publish_state()

    def _enable_position_feedback(self) -> None:
        if self.reports_position:
            return
        self.reports_position = True
        _LOGGER.info("%s reports its position - enabling position control", self.name)
        self.ctx.rediscover(self)

    def _apply_movement(self, what: str) -> None:
        """Track a start/stop of the motor and keep the estimate in sync."""
        now = self.ctx.now()
        if self._tilting:
            # A tilt impulse is a twitch, not a journey: it must not turn into
            # an "opening" state or start the position tracking.
            return
        if what in UP_CODES or what in DOWN_CODES:
            # Something is moving - no matter who started it, a wall switch
            # included.  Follow it so the percentage stays live.
            self._tracking = True
            self._tracking_since = now
            if self.supports_tilt:
                # Travelling up opens the slats, travelling down closes them.
                self.tilt = 100 if what in UP_CODES else 0
        if what in UP_CODES:
            self._start_move(1, now)
        elif what in DOWN_CODES:
            self._start_move(-1, now)
        elif what in STOP_CODES:
            # Whether this halt ended a drive is decided where the fresh
            # position is known: during a native drive nothing is polled.
            self._tracking = False
            self._integrate(now)
            self._moving_dir = 0
            if self.reports_position:
                self.state = STATE_CLOSED if self.position <= 0 else STATE_OPEN
            else:
                # No feedback at all: assume a full travel was completed.
                self.state = STATE_OPEN if self.state == STATE_OPENING else STATE_CLOSED
                self.position = 100 if self.state == STATE_OPEN else 0

    def _start_move(self, direction: int, now: float) -> None:
        self._integrate(now)
        self._moving_dir = direction
        self._moving_since = now
        self.state = STATE_OPENING if direction > 0 else STATE_CLOSING

    def _integrate(self, now: float) -> None:
        """Advance the estimated position by the time the motor was running."""
        if self._moving_dir and self.travel_time > 0:
            elapsed = now - self._moving_since
            delta = (elapsed / self.travel_time) * 100 * self._moving_dir
            self.position = max(0, min(100, self.position + delta))
        self._moving_since = now

    def tick(self) -> None:
        """Once a second: watch a running drive and the time based estimate."""
        now = self.ctx.now()

        if self._pending_target is not None and now - self._pending_since > PENDING_TIMEOUT:
            # The gateway stayed silent; go with the position we have.
            target, self._pending_target = self._pending_target, None
            _LOGGER.debug("%s: no fresh position, using the stored one", self.name)
            self._begin_drive(target)

        if self._native_target is not None:
            # The actuator drives on its own and announces its arrival, so
            # there is nothing to poll.  Only its silence is worth noticing.
            if self._moving_dir:
                pass
            elif self.ctx.queue_depth() and now - self._native_ordered < NATIVE_LIMIT:
                # As long as the command still waits in the queue, silence is
                # no answer - the actuator has not even been asked yet.
                self._native_since = now
            elif now - self._native_since > NATIVE_GRACE:
                target, self._native_target = self._native_target, None
                _LOGGER.info("%s: no reaction to the position command - "
                             "driving to %d%% by hand instead", self.name, target)
                self._pending_target = target
                self._pending_since = now
                self.send(self._status_request())
        elif self._target is not None:
            if self._bus_congested():
                # A question we never asked must not count as a missing answer.
                # Thirty shutters starting at once fill the queue for a good ten
                # seconds; DRIVE_TIMEOUT remains as the outer limit.
                self._last_progress = now
                if now - self._target_started > DRIVE_TIMEOUT:
                    self._give_up(now)
            elif (now - self._last_progress > STALL_TIMEOUT
                    or now - self._target_started > DRIVE_TIMEOUT):
                self._give_up(now)
            else:
                self.send(self._status_request())
        elif self._tracking:
            if now - self._tracking_since > TRACKING_TIMEOUT:
                self._tracking = False
            elif not self._bus_congested():
                self.send(self._status_request())

        if not self._moving_dir or self.travel_time <= 0:
            return
        self._integrate(now)
        if self.position <= 0 or self.position >= 100:
            self._moving_dir = 0
            self.state = STATE_CLOSED if self.position <= 0 else STATE_OPEN
        self.publish_state()

    def _give_up(self, _now: float) -> None:
        _LOGGER.warning("%s: no progress towards %d%% - stopping at %d%%",
                        self.name, self._target, self.position)
        self._clear_drive()
        self.send(msg.command(self.who, WHAT_STOP, self.where))

    def publish_state(self) -> None:
        self.publish(self.state_topic, self.state)
        if self.reports_position:
            self.publish(self.position_topic, str(int(round(self.position))))
        if self.supports_tilt:
            self.publish(self.tilt_topic, str(int(round(self.tilt))))
