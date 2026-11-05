"""BMSE light management sensors (WHO=1): presence and brightness.

Measured on a BMSE3001 (WISSEN.md "Lichtmanagement (BMSE)"):

===============================  =========================================
``*#1*<where>*6##``              brightness in lux (cross-checked on a second
                                 gateway)
``*#1*<where>*7##``              three values, the first is the presence
``*#1*<where>*5##``              constant, probably the operating mode
``*1*34*<where>##``              light management acted on this address
===============================  =========================================

Only a sensor programmed as a scenario device sends by itself, so presence has
to be polled - which costs bus traffic, so it is off by default.  A presence
sensor drops to zero between two movements; passing that through would make
it flicker, so a detection holds until ``off_delay`` seconds pass without one.
"""

from __future__ import annotations

import logging
from typing import Any

from ..own import messages as msg
from .base import Context, Discovery, Entity, device_class, is_number

_LOGGER = logging.getLogger(__name__)

DIM_MODE = "5"
DIM_LUX = "6"
DIM_PRESENCE = "7"
WHAT_LIGHT_MANAGEMENT = "34"
NO_PRESENCE = ("0", "00")

DEFAULT_LUX_INTERVAL = 60
DEFAULT_OFF_DELAY = 60
# The same rule as for shutters: above this many queued frames, polls wait.
MAX_QUEUE_FOR_POLLING = 4


class BmseSensor(Entity):
    who = msg.WHO_LIGHTING
    component = "sensor"
    topic_group = "sensor"
    section = "motion_sensors"
    default_name = "Light management sensor"
    model = "BMSE light management sensor"
    needs_tick = True
    # A light point may use the same address - both exist side by side.
    exclusive_address = False

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context, config)
        self.object_id = f"{context.bridge_id}_{self.who}_{self.key}_bmse"
        self.with_illuminance = bool(config.get("illuminance", True))
        self.with_motion = bool(config.get("motion", True))
        self.lux_interval = int(config.get("lux_interval", DEFAULT_LUX_INTERVAL))
        self.motion_poll = int(config.get("motion_poll", 0))
        self.off_delay = int(config.get("off_delay", DEFAULT_OFF_DELAY))
        self.device_class = device_class(
            "binary_sensor", config.get("device_class", "motion"), config.get("name", ""))
        self.lux: int | None = None
        self.motion = False
        self._last_detection = 0.0
        self._ticks = 0
        self._published: dict[str, str] = {}

    @property
    def lux_topic(self) -> str:
        return self.topic_root + "/illuminance"

    @property
    def motion_topic(self) -> str:
        return self.topic_root + "/motion"

    def to_config(self) -> dict[str, Any]:
        config = super().to_config()
        config.update({
            "illuminance": self.with_illuminance,
            "motion": self.with_motion,
            "lux_interval": self.lux_interval,
            "motion_poll": self.motion_poll,
            "off_delay": self.off_delay,
        })
        return config

    def discovery(self) -> list[Discovery]:
        entities: list[Discovery] = []
        if self.with_illuminance:
            lux = self.base_payload("lux", name="Brightness")
            lux.update({"state_topic": self.lux_topic, "device_class": "illuminance",
                        "unit_of_measurement": "lx", "state_class": "measurement"})
            entities.append(("sensor", lux["unique_id"], lux))
        if self.with_motion:
            motion = self.base_payload("motion", name="Motion")
            motion.update({"state_topic": self.motion_topic, "payload_on": "ON",
                           "payload_off": "OFF"})
            if self.device_class:
                motion["device_class"] = self.device_class
            # No "off_delay" for Home Assistant: its timer would start at the
            # first detection and expire while somebody is still walking about.
            entities.append(("binary_sensor", motion["unique_id"], motion))
        return entities

    def poll_frames(self) -> tuple[str, ...]:
        frames = []
        if self.with_illuminance:
            frames.append(msg.dimension_request(self.who, self.where, DIM_LUX))
        if self.with_motion:
            frames.append(msg.dimension_request(self.who, self.where, DIM_PRESENCE))
        return tuple(frames)

    def on_own(self, message: msg.OwnMessage) -> None:
        if message.kind == msg.KIND_DIMENSION_RESPONSE and message.values:
            first = message.values[0]
            if message.dimension == DIM_LUX and is_number(first):
                self.lux = int(first)
            elif message.dimension == DIM_PRESENCE:
                self._set_motion(first not in NO_PRESENCE)
            else:
                return
        elif (message.kind == msg.KIND_NORMAL
              and message.what == WHAT_LIGHT_MANAGEMENT and self.with_motion):
            # On a sensor programmed as a scenario device this is the detection.
            self._set_motion(True)
        else:
            return
        self.publish_state()

    def _set_motion(self, detected: bool) -> None:
        """Take a detection; the end of one is decided by the hold time."""
        if detected:
            self._last_detection = self.ctx.now()
            if not self.motion:
                _LOGGER.debug("%s: motion detected", self.name)
            self.motion = True
        elif not self.off_delay:
            self.motion = False                # off_delay 0: follow the sensor

    def tick(self) -> None:
        """Release the hold time; poll brightness slowly, presence if asked to."""
        self._ticks += 1
        if (self.motion and self.off_delay
                and self.ctx.now() - self._last_detection >= self.off_delay):
            _LOGGER.debug("%s: %ds without a detection - clear", self.name, self.off_delay)
            self.motion = False
            self.publish_state()

        if self.ctx.queue_depth() > MAX_QUEUE_FOR_POLLING:
            return
        if self.with_illuminance and self.lux_interval > 0 \
                and self._ticks % self.lux_interval == 0:
            self.send(msg.dimension_request(self.who, self.where, DIM_LUX))
        if self.with_motion and self.motion_poll > 0 and self._ticks % self.motion_poll == 0:
            self.send(msg.dimension_request(self.who, self.where, DIM_PRESENCE))

    def republish(self) -> None:
        self._published.clear()
        self.publish_state()

    def publish_state(self) -> None:
        """Publish what changed - presence may be polled every few seconds."""
        if self.lux is not None and self._published.get("lux") != str(self.lux):
            self._published["lux"] = str(self.lux)
            self.publish(self.lux_topic, str(self.lux))
        if self.with_motion:
            state = "ON" if self.motion else "OFF"
            if self._published.get("motion") != state:
                self._published["motion"] = state
                # Not retained: a detection is a moment, and off_delay ends it.
                self.publish(self.motion_topic, state, retain=False)
