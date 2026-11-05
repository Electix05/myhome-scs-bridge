"""Push-only devices: dry contacts (WHO=25), CEN / CEN+ keypads and scenarios.

Two rules from WISSEN.md apply to every key press:

* Events are **not retained**.  A retained press would be handed to every
  automation again after a restart - a reaction to a press from yesterday.
* The payload of an event **stays constant**.  A timestamp reads nicely in an
  MQTT browser but cannot be matched in an automation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..own import messages as msg
from .base import Context, Discovery, Entity, device_class, is_number, slug

_LOGGER = logging.getLogger(__name__)

# Measured on a real installation: WHAT 32 arrives when the contact closes and
# WHAT 31 when it opens - the other way round than the specification suggests.
CONTACT_OPEN = "31"
CONTACT_CLOSED = "32"
CONTACT_PREFIX = "3"          # contact 99 is 399 on the bus
KEYPAD_PREFIX = "2"           # CEN+ keypads live on 2xx

# CEN+ (WHO=25) button actions.
CEN_PLUS_ACTIONS = {
    "21": "press",
    "22": "long_press_start",
    "23": "long_press",
    "24": "long_press_release",
}
# CEN (WHO=15): a bare WHAT is the press, the parameter tells the rest.
CEN_ACTIONS = {
    "": "press",
    "1": "release",
    "2": "long_press",
    "3": "long_press_release",
}
EVENT_TYPES = ["press", "release", "long_press_start", "long_press", "long_press_release"]
KIND_CEN_PLUS = "cen_plus"
KIND_CEN = "cen"


def _as_numbers(values: list[str]) -> list[int | str]:
    return [int(value) if is_number(value) else value for value in values]


class DryContact(Entity):
    """A contact interface reported on the bus as a binary sensor."""

    who = msg.WHO_CEN_PLUS
    component = "binary_sensor"
    topic_group = "contact"
    section = "binary_sensors"
    default_name = "Contact"
    model = "Dry contact / IR interface"

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context, config)
        self.device_class = device_class(
            self.component, config.get("device_class"), config.get("name", ""))
        # The address is the contact number, as printed on the device and set
        # in MyHOME_Suite.  On the bus it carries a leading 3 (4 for an IR
        # interface) - that belongs in here, not in the head of the user.
        self.bus_prefix = str(config.get("bus_prefix", CONTACT_PREFIX))
        self.bus_where = self.bus_prefix + self.where
        if self.bus_prefix != CONTACT_PREFIX:
            # Contact 1 (31) and IR interface 1 (41) are two devices, so their
            # ids and topics must differ as well; plain contacts keep theirs.
            self.key = slug(f"{self.bus_prefix}_{self.where}")
            self.object_id = f"{context.bridge_id}_{self.who}_{self.key}"
        # Two checkboxes instead of one "inverted" flag: until somebody says
        # which kind it is, guessing would be worse than staying quiet.
        self.make_contact = bool(config.get("schliesser") or config.get("make_contact"))
        self.break_contact = bool(config.get("oeffner") or config.get("break_contact")
                                  or config.get("invert"))   # invert: older spelling
        self.is_on = False
        self._known = False

    @property
    def configured(self) -> bool:
        """Exactly one of the two kinds has to be chosen."""
        return self.make_contact != self.break_contact

    def match_keys(self) -> tuple[str, ...]:
        return (self.bus_where,)

    def to_config(self) -> dict[str, Any]:
        config = super().to_config()
        config["schliesser"] = self.make_contact
        config["oeffner"] = self.break_contact
        for old in ("invert", "make_contact", "break_contact"):
            config.pop(old, None)
        return config

    def discovery(self) -> list[Discovery]:
        if not self.configured:
            if self.make_contact and self.break_contact:
                _LOGGER.warning("%s: Schließer und Öffner zugleich - der Kontakt bleibt aus",
                                self.name)
            return []
        payload = self.base_payload()
        payload.update({"state_topic": self.state_topic, "payload_on": "ON",
                        "payload_off": "OFF"})
        if self.device_class:
            payload["device_class"] = self.device_class
        return [(self.component, self.object_id, payload)]

    def poll_frames(self) -> tuple[str, ...]:
        if not self.configured:
            return ()
        return (msg.status_request(self.who, self.bus_where),)

    def on_own(self, message: msg.OwnMessage) -> None:
        if message.kind != msg.KIND_NORMAL or not self.configured:
            return
        if message.what == CONTACT_CLOSED:
            closed = True
        elif message.what == CONTACT_OPEN:
            closed = False
        else:
            return
        # A make contact is triggered while closed, a break contact while open.
        self.is_on = closed if self.make_contact else not closed
        self._known = True
        self.publish_state()

    def publish_state(self) -> None:
        # Nothing before the first frame: Home Assistant honestly shows
        # "unknown" instead of a guess.
        if self._known and self.configured:
            self.publish(self.state_topic, "ON" if self.is_on else "OFF")


class Keypad(Entity):
    """A CEN or CEN+ keypad, exposed as one HA event entity per button."""

    component = "event"
    topic_group = "keypad"
    section = "buttons"
    default_name = "Keypad"
    model = "CEN / CEN+ keypad"

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        self.kind = str(config.get("kind", KIND_CEN_PLUS)).lower()
        # The WHO follows the kind unless the entry names one itself.
        self.who = msg.WHO_CEN_PLUS if self.kind == KIND_CEN_PLUS else msg.WHO_CEN
        super().__init__(context, config)
        buttons = config.get("buttons") or list(range(1, 9))
        self.buttons = [str(button) for button in buttons]

    def to_config(self) -> dict[str, Any]:
        config = super().to_config()
        config["kind"] = self.kind
        config["buttons"] = _as_numbers(self.buttons)
        return config

    def button_topic(self, button: str) -> str:
        return f"{self.topic_root}/button/{slug(button)}"

    def action_topic(self, button: str) -> str:
        """Plain text next to the JSON that Home Assistant insists on."""
        return self.button_topic(button) + "/action"

    def event_topic(self, button: str, action: str) -> str:
        """A topic of its own per action, so an automation can pick one."""
        return f"{self.button_topic(button)}/{action}"

    def group_topic(self, action: str) -> str:
        """All keypads, one topic per action."""
        return self.ctx.topic(self.topic_group, action)

    @property
    def last_topic(self) -> str:
        """One topic for every keypad - subscribe once, hear everything."""
        return self.ctx.topic(self.topic_group, "last")

    def discovery(self) -> list[Discovery]:
        entities: list[Discovery] = []
        for button in self.buttons:
            payload = self.base_payload(f"btn{slug(button)}", name=f"Button {button}")
            payload.update({"state_topic": self.button_topic(button),
                            "event_types": EVENT_TYPES, "device_class": "button"})
            entities.append((self.component, payload["unique_id"], payload))
        return entities

    def on_own(self, message: msg.OwnMessage) -> None:
        if message.kind != msg.KIND_NORMAL:
            return
        if self.kind == KIND_CEN_PLUS:
            action = CEN_PLUS_ACTIONS.get(message.what)
            button = message.what_params[0] if message.what_params else None
        else:
            parameter = message.what_params[0] if message.what_params else ""
            action = CEN_ACTIONS.get(parameter)
            button = message.what
        if action is None or button is None:
            return
        if button not in self.buttons:
            _LOGGER.debug("%s: ignoring unconfigured button %s", self.name, button)
            return
        # Home Assistant's event platform needs the JSON; everything else is
        # easier to use as plain text, so both go out - none of it retained.
        self.publish(self.button_topic(button), json.dumps({"event_type": action}),
                     retain=False)
        self.publish(self.action_topic(button), action, retain=False)
        self.publish(self.event_topic(button, action), action, retain=False)
        self.publish(self.group_topic(action), f"{self.where}/{button}", retain=False)
        self.publish(self.last_topic, f"{self.where}/{button}/{action}", retain=False)

    def publish_state(self) -> None:
        """Events have no state to restore."""


class Scenario(Entity):
    """WHO=0 - scenario modules (F420 and friends), exposed as HA scenes."""

    who = msg.WHO_SCENARIO
    component = "scene"
    topic_group = "scene"
    section = "scenes"
    default_name = "Scenario module"
    model = "Scenario module"

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context, config)
        scenarios = config.get("scenarios") or list(range(1, 5))
        self.scenarios = [str(scenario) for scenario in scenarios]
        names = config.get("scenario_names") or {}
        # YAML reads "1: Kino" with a number as key; look names up as text.
        self.names = {str(key): str(value) for key, value in names.items()} \
            if isinstance(names, dict) else {}

    def to_config(self) -> dict[str, Any]:
        config = super().to_config()
        config["scenarios"] = _as_numbers(self.scenarios)
        return config

    def scenario_topic(self, scenario: str) -> str:
        return f"{self.topic_root}/scenario/{slug(scenario)}/set"

    def discovery(self) -> list[Discovery]:
        entities: list[Discovery] = []
        for scenario in self.scenarios:
            name = self.names.get(scenario) or f"Scenario {scenario}"
            payload = self.base_payload(f"scn{slug(scenario)}", name=name)
            payload.update({"command_topic": self.scenario_topic(scenario),
                            "payload_on": "ON"})
            entities.append((self.component, payload["unique_id"], payload))
        return entities

    def subscriptions(self) -> tuple[str, ...]:
        return tuple(self.scenario_topic(scenario) for scenario in self.scenarios)

    def on_command(self, topic: str, payload: str) -> None:
        for scenario in self.scenarios:
            if topic == self.scenario_topic(scenario):
                self.send(msg.command(self.who, scenario, self.where))
                return

    def publish_state(self) -> None:
        """Scenes are stateless."""
