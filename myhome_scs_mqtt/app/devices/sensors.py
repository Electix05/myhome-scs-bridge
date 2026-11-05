"""WHO=18 energy meters and the diagnostic entities of the add-on itself."""

from __future__ import annotations

import logging
from typing import Any, Callable

from .. import __version__
from ..own import messages as msg
from .base import MANUFACTURER, Context, Discovery, Entity

_LOGGER = logging.getLogger(__name__)

DIM_ACTIVE_POWER = "113"
DIM_TOTALIZER = "51"
DIM_DAY_TOTALIZER = "54"
# Asks the meter to push active power updates for <minutes> minutes (max 255).
DIM_AUTO_UPDATE = "1200#1"
DIM_AUTO_UPDATE_PREFIX = "1200"
AUTO_UPDATE_MINUTES = 30

LINK_CONNECTED = "connected"
LINK_DISCONNECTED = "disconnected"
SCAN_PRESS = "PRESS"
SCAN_DEEP = "DEEP"
MAX_STATE_LENGTH = 255
# Bus counters shown as diagnostic sensors: frames the gateway refused (probes
# not counted) and frames it did not answer at all.
BUS_COUNTERS = ("refused", "unanswered")
# During a storm of refusals the counters change several times a second; every
# change would become a row in the Home Assistant database.
COUNTER_INTERVAL = 5.0


def _signed_number(text: str) -> int | None:
    body = text[1:] if text.startswith("-") else text
    if not (body.isascii() and body.isdigit()):
        return None
    return int(text)


class EnergyMeter(Entity):
    who = msg.WHO_ENERGY
    component = "sensor"
    topic_group = "energy"
    section = "energy"
    default_name = "Energy meter"
    model = "Energy meter"

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context, config)
        self.with_total = bool(config.get("energy_total", True))
        self.with_today = bool(config.get("energy_today", False))
        self.power: int | None = None
        self.total: float | None = None
        self.today: float | None = None

    def to_config(self) -> dict[str, Any]:
        config = super().to_config()
        config["energy_total"] = self.with_total
        if self.with_today:
            config["energy_today"] = True
        return config

    @property
    def power_topic(self) -> str:
        return self.topic_root + "/power"

    @property
    def total_topic(self) -> str:
        return self.topic_root + "/energy"

    @property
    def today_topic(self) -> str:
        return self.topic_root + "/energy_today"

    def discovery(self) -> list[Discovery]:
        power = self.base_payload("power", name="Power")
        power.update({"state_topic": self.power_topic, "device_class": "power",
                      "unit_of_measurement": "W", "state_class": "measurement"})
        entities: list[Discovery] = [(self.component, power["unique_id"], power)]
        if self.with_total:
            total = self.base_payload("energy", name="Energy")
            total.update({"state_topic": self.total_topic, "device_class": "energy",
                          "unit_of_measurement": "kWh", "state_class": "total_increasing"})
            entities.append((self.component, total["unique_id"], total))
        if self.with_today:
            today = self.base_payload("energy_today", name="Energy today")
            today.update({"state_topic": self.today_topic, "device_class": "energy",
                          "unit_of_measurement": "kWh", "state_class": "total_increasing"})
            entities.append((self.component, today["unique_id"], today))
        return entities

    def poll_frames(self) -> tuple[str, ...]:
        frames = [
            # Re-arm the push updates; they expire after AUTO_UPDATE_MINUTES.
            msg.dimension_write(self.who, self.where, DIM_AUTO_UPDATE, str(AUTO_UPDATE_MINUTES)),
            msg.dimension_request(self.who, self.where, DIM_ACTIVE_POWER),
        ]
        if self.with_total:
            frames.append(msg.dimension_request(self.who, self.where, DIM_TOTALIZER))
        if self.with_today:
            frames.append(msg.dimension_request(self.who, self.where, DIM_DAY_TOTALIZER))
        return tuple(frames)

    def on_own(self, message: msg.OwnMessage) -> None:
        if message.kind != msg.KIND_DIMENSION_RESPONSE or not message.values:
            return
        value = _signed_number(message.values[0])
        if value is None:
            return
        dimension = message.dimension
        if dimension == DIM_ACTIVE_POWER or dimension.startswith(DIM_AUTO_UPDATE_PREFIX):
            self.power = value
        elif dimension == DIM_TOTALIZER:
            self.total = value / 1000.0          # the totalizer counts Wh
        elif dimension == DIM_DAY_TOTALIZER:
            self.today = value / 1000.0
        else:
            return
        self.publish_state()

    def publish_state(self) -> None:
        if self.power is not None:
            self.publish(self.power_topic, str(self.power))
        if self.total is not None:
            self.publish(self.total_topic, f"{self.total:.3f}")
        if self.today is not None:
            self.publish(self.today_topic, f"{self.today:.3f}")


class BridgeStatus(Entity):
    """Diagnostic entities of the add-on: gateway link, bus scan, scan buttons
    and the bus counters."""

    who = msg.WHO_GATEWAY
    component = "binary_sensor"
    default_name = "MyHome SCS Bridge"
    model = "OpenWebNet gateway"
    # Looks at the bus counters now and then.
    needs_tick = True

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context, config)
        # The add-on itself is one device; its id must not follow the gateway IP.
        self.object_id = context.bridge_id
        self.connected = False
        self.last_frame = ""
        self.scan_state = "bereit"
        # Replaced by the bridge with the real scan trigger.
        self.on_scan: Callable[[bool], None] = lambda _deep: None
        # Replaced by the bridge: the gateway's counters by name (BUS_COUNTERS).
        self.read_counters: Callable[[], dict[str, int]] = dict
        self.counters: dict[str, int] = {}
        self._counters_checked: float | None = None

    @property
    def link_topic(self) -> str:
        return self.ctx.topic("bridge", "link")

    @property
    def scan_topic(self) -> str:
        return self.ctx.topic("bridge", "scan")

    @property
    def scan_command_topic(self) -> str:
        return self.scan_topic + "/set"

    def counter_topic(self, name: str) -> str:
        return self.ctx.topic("bridge", name)

    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": [self.ctx.bridge_id],
            "name": self.name,
            "manufacturer": MANUFACTURER,
            "model": self.model,
            "sw_version": __version__,
        }

    def availability(self) -> dict[str, Any]:
        """These must stay visible exactly when the SCS link is down."""
        topic = self.ctx.bridge_availability or self.ctx.availability_topic
        return {"availability_topic": topic, "payload_available": "online",
                "payload_not_available": "offline"}

    def discovery(self) -> list[Discovery]:
        link = self.base_payload("link", name="Gateway link")
        link.update({"state_topic": self.link_topic, "payload_on": LINK_CONNECTED,
                     "payload_off": LINK_DISCONNECTED, "device_class": "connectivity",
                     "entity_category": "diagnostic"})
        scan = self.base_payload("scan", name="Bus-Scan")
        scan.update({"state_topic": self.scan_topic, "entity_category": "diagnostic",
                     "icon": "mdi:radar"})
        button = self.base_payload("scan_button", name="Bus scannen")
        button.update({"command_topic": self.scan_command_topic, "payload_press": SCAN_PRESS,
                       "entity_category": "config", "icon": "mdi:radar"})
        deep = self.base_payload("scan_deep_button", name="Bus gründlich scannen")
        deep.update({"command_topic": self.scan_command_topic, "payload_press": SCAN_DEEP,
                     "entity_category": "config", "icon": "mdi:magnify-scan"})
        refused = self.base_payload("refused", name="Abgelehnte Frames")
        refused.update({"state_topic": self.counter_topic("refused"),
                        "entity_category": "diagnostic", "state_class": "total_increasing",
                        "icon": "mdi:cancel"})
        unanswered = self.base_payload("unanswered", name="Unbeantwortete Frames")
        unanswered.update({"state_topic": self.counter_topic("unanswered"),
                           "entity_category": "diagnostic", "state_class": "total_increasing",
                           "icon": "mdi:timer-sand-empty"})
        return [
            ("binary_sensor", link["unique_id"], link),
            ("sensor", scan["unique_id"], scan),
            ("button", button["unique_id"], button),
            ("button", deep["unique_id"], deep),
            ("sensor", refused["unique_id"], refused),
            ("sensor", unanswered["unique_id"], unanswered),
        ]

    def subscriptions(self) -> tuple[str, ...]:
        return (self.scan_command_topic,)

    def on_command(self, topic: str, payload: str) -> None:
        if topic == self.scan_command_topic:
            self.on_scan(payload.strip().upper() == SCAN_DEEP)

    def set_connected(self, state: bool) -> None:
        self.connected = state
        self.publish(self.link_topic, LINK_CONNECTED if state else LINK_DISCONNECTED)

    def set_last_frame(self, frame: str) -> None:
        # For the web interface only.  As an entity every bus frame ended up in
        # the Home Assistant logbook, which buried everything else.
        self.last_frame = frame[:MAX_STATE_LENGTH]

    def set_scan_state(self, state: str) -> None:
        self.scan_state = state[:MAX_STATE_LENGTH]
        self.publish(self.scan_topic, self.scan_state)

    def tick(self) -> None:
        now = self.ctx.now()
        if self._counters_checked is not None and now - self._counters_checked < COUNTER_INTERVAL:
            return
        self._counters_checked = now
        self._publish_counters(changed_only=True)

    def _publish_counters(self, changed_only: bool) -> None:
        values = self.read_counters()
        for name in BUS_COUNTERS:
            value = int(values.get(name, 0))
            if changed_only and self.counters.get(name) == value:
                continue
            self.counters[name] = value
            self.publish(self.counter_topic(name), str(value))

    def publish_state(self) -> None:
        self.publish(self.link_topic, LINK_CONNECTED if self.connected else LINK_DISCONNECTED)
        self.publish(self.scan_topic, self.scan_state)
        self._publish_counters(changed_only=False)
