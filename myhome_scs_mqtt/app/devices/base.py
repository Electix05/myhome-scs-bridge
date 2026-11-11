"""Common ground for every entity the bridge exposes.

An :class:`Entity` is one physical MyHome address, shown in Home Assistant as
one or more entities.  It talks to the outside world only through its
:class:`Context`, which makes every device type testable on its own.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Protocol

from .. import __version__

if TYPE_CHECKING:
    from ..own.messages import OwnMessage

_LOGGER = logging.getLogger(__name__)

# Default prefix of every unique_id and discovery object id; the option
# "instance_id" replaces it (settings.DEFAULT_INSTANCE_ID holds the same value,
# a test keeps the two in step).  The previous add-on "MyHome SCS MQTT Bridge"
# (slug myhome_scs_mqtt) uses "myhome_scs_bridge" and removes every retained
# discovery message whose topic *contains* that text - so this id must not
# contain it.  Both add-ons may run side by side without sharing a single
# unique_id or taking each other's entities; our own clean-up matches the
# discovery node exactly (see bridge/discovery.py).
BRIDGE_ID = "scs_bridge"
MANUFACTURER = "BTicino / Legrand"
ORIGIN_NAME = "MyHome SCS Bridge"
PAYLOAD_ONLINE = "online"
PAYLOAD_OFFLINE = "offline"

# How long a silenced address is left alone before one single retry.  Ten
# minutes: often enough for a corrected entry to come back on its own, rare
# enough that a whole wrong device file costs no measurable bus load.
REVIVE_AFTER = 600.0

# (component, object_id, payload) - one HA entity announced via MQTT discovery.
Discovery = tuple[str, str, dict[str, Any]]


class Cancellable(Protocol):
    """What ``call_later`` hands back: something that can be cancelled."""

    def cancel(self) -> None: ...


def _no_rediscover(_entity: Entity) -> None:
    return None


def _no_queue() -> int:
    return 0


def _no_scheduler(_delay: float, _callback: Callable[[], None]) -> Cancellable | None:
    return None


@dataclass
class Context:
    """Everything an entity needs to talk to the outside world."""

    base_topic: str
    discovery_prefix: str
    # "online" only while the SCS link is up.
    availability_topic: str
    # publish(topic, payload, retain=True, qos=0)
    publish: Callable[..., None]
    # Queue a frame for the bus.
    send: Callable[[str], None]
    # Identity of this instance of the add-on (option "instance_id"): prefix of
    # every unique_id and object_id, and the node level of the discovery topics.
    # One Home Assistant may run the add-on several times, one instance per
    # MyHome installation - two instances must not share this.
    bridge_id: str = BRIDGE_ID
    # Put in front of every device name, e.g. "Haus 2 Küche".  Empty by default,
    # and then names are exactly what the device file says.
    name_prefix: str = ""
    # Re-announce an entity that learned something about itself.
    rediscover: Callable[[Entity], None] = _no_rediscover
    # How many frames are still queued for the bus - lets entities hold back.
    queue_depth: Callable[[], int] = _no_queue
    # Run a callback after a delay; the returned handle's cancel() drops it
    # again - a slat impulse that has been overtaken must not fire its stop
    # into the next command.
    call_later: Callable[[float, Callable[[], None]], Cancellable | None] = _no_scheduler
    # "online" while the add-on itself runs (the MQTT last will topic).
    bridge_availability: str = ""
    # Monotonic clock in seconds; replaced by a fake clock in tests.
    now: Callable[[], float] = time.monotonic
    # After this many refusals in a row an address counts as dead: it is no
    # longer polled and shows up as unavailable.  0 switches that off.
    dead_after: int = 0
    # Seconds until a silenced address gets one single retry.
    revive_after: float = REVIVE_AFTER

    def topic(self, *parts: object) -> str:
        return "/".join([self.base_topic] + [str(part) for part in parts])

    def device_name(self, name: str) -> str:
        """``name`` with the instance's prefix in front, if there is one."""
        return f"{self.name_prefix} {name}" if self.name_prefix else name


# Home Assistant rejects a discovery message with an unknown device class
# completely - the entity then never appears, without a word.  Better to drop
# the class than to lose the whole device.
DEVICE_CLASSES = {
    "binary_sensor": {
        "battery", "battery_charging", "carbon_monoxide", "cold", "connectivity",
        "door", "garage_door", "gas", "heat", "light", "lock", "moisture",
        "motion", "moving", "occupancy", "opening", "plug", "power", "presence",
        "problem", "running", "safety", "smoke", "sound", "tamper", "update",
        "vibration", "window",
    },
    "cover": {
        "awning", "blind", "curtain", "damper", "door", "garage", "gate",
        "shade", "shutter", "window",
    },
    "switch": {"outlet", "switch"},
}


def device_class(component: str, value: object, name: str = "") -> str:
    """Return the class if Home Assistant knows it, otherwise nothing."""
    if not value:
        return ""
    known = DEVICE_CLASSES.get(component)
    text = str(value).strip().lower()
    if known is None or text in known:
        return text
    _LOGGER.warning(
        "%s: Home Assistant kennt die Geräteklasse '%s' nicht - sie wird "
        "weggelassen, damit das Gerät überhaupt erscheint", name or component, value
    )
    return ""


def slug(value: object) -> str:
    """Make an OpenWebNet address safe for MQTT topics and entity ids.

    ``#`` is an MQTT wildcard, so group addresses get a readable prefix
    instead: ``#3`` becomes ``grp_3`` and stays distinct from ambient ``3``.
    """
    text = re.sub(r"[^0-9a-zA-Z]+", "_", str(value)).strip("_")
    if str(value).startswith("#"):
        text = "grp_" + text
    return text or "0"


def is_number(text: str) -> bool:
    """ASCII digits only - "²".isdigit() is True, but int() cannot read it."""
    return text.isascii() and text.isdigit()


class Entity:
    """Base class: one physical MyHome address, one or more HA entities.

    The attributes and methods below are the whole contract between an entity
    and the bridge; subclasses override what applies to them.
    """

    who: str = "1"
    component: str = "sensor"
    topic_group: str = "sensor"     # readable second level of the MQTT topic
    section: str = ""               # the devices.yaml section of this entity
    default_name: str = "MyHome device"
    model: str = "SCS device"
    # tick() is called once a second when this is True.
    needs_tick: bool = False
    # False when another entity of the same WHO may legitimately share the
    # address - a BMSE presence sensor sits on the address of a light point.
    exclusive_address: bool = True

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        self.ctx = context
        # A copy: the caller's dict must not change under our feet, and ours
        # keeps every field the user wrote, known or not.
        self.config = dict(config)
        where = config.get("where")
        if where is None or str(where).strip() == "":
            raise ValueError(f"{type(self).__name__} needs a 'where' address")
        # A few entity types (switches, sensors) can be bound to another WHO.
        self.who = str(config.get("who", self.who))
        # Addresses stay text: "04" is not the number 4.
        self.where = str(where).strip()
        self.key = slug(self.where)
        # The name as the device file writes it (or the fallback).  The prefix
        # of the instance is not part of it: to_config() writes this name back,
        # and a stored one would collect another prefix on every save.
        self.base_name = str(config.get("name") or f"{self.default_name} {self.where}")
        self.name = context.device_name(self.base_name)
        self.object_id = f"{context.bridge_id}_{self.who}_{self.key}"
        self.discovered = bool(config.get("discovered"))
        # Reachability: an address the gateway refuses again and again is
        # silenced (see note_refusal).
        self.silenced = False
        self._refusals = 0
        self._silenced_at = 0.0
        # True once a frame from the bus was handed to this entity.  Before
        # that, "off" or "open" is only the value it was created with - the
        # web interface shows nothing rather than a guess.
        self.heard = False

    # ------------------------------------------------------------------ ids

    @property
    def topic_root(self) -> str:
        """``myhome/light/12`` reads better than ``myhome/who1/12``.

        The address stays the second level: it is what the bus talks about and
        it does not change when the device is renamed.  The friendly name is
        published next to it so the tree explains itself.
        """
        return self.ctx.topic(self.topic_group, self.key)

    @property
    def name_topic(self) -> str:
        return self.topic_root + "/name"

    @property
    def state_topic(self) -> str:
        return self.topic_root + "/state"

    @property
    def command_topic(self) -> str:
        return self.topic_root + "/set"

    @property
    def configured(self) -> bool:
        """False for an entry that still lacks a decision (see DryContact)."""
        return True

    @property
    def reachability_topic(self) -> str:
        """Whether this address answers at all: "offline" while it is silenced."""
        return self.topic_root + "/reachable"

    # --------------------------------------------------------- reachability

    def note_refusal(self) -> None:
        """The gateway refused a frame for this address.

        After ``ctx.dead_after`` refusals in a row the address is silenced: it
        is no longer polled and Home Assistant shows it as unavailable.  An
        address that does not exist is refused on every single round; measured
        on 24.09.2026, one of them was enough to slow the whole installation
        down.  Commands still go out - the user may always try again.
        """
        if not self.ctx.dead_after or self.silenced:
            return
        self._refusals += 1
        if self._refusals < self.ctx.dead_after:
            return
        self.silenced = True
        self._silenced_at = self.ctx.now()
        _LOGGER.warning("Adresse %s (%s) wurde %d-mal abgelehnt - sie wird vorerst "
                        "nicht mehr abgefragt", self.where, self.name, self._refusals)
        self.publish_reachability()

    def note_alive(self) -> None:
        """Something came back from this address - it is there after all."""
        self._refusals = 0
        if not self.silenced:
            return
        self.silenced = False
        _LOGGER.info("Adresse %s (%s) antwortet wieder - sie wird wieder abgefragt",
                     self.where, self.name)
        self.publish_reachability()

    def poll_due(self) -> bool:
        """May this address be asked right now?  A plain yes or no.

        True for everything that is not silenced.  A silenced address is due
        for one single revival question every ``ctx.revive_after`` seconds;
        :meth:`note_poll_attempt` books that question once a frame for it is
        really on its way.
        """
        if not self.silenced:
            return True
        return self.ctx.now() - self._silenced_at >= self.ctx.revive_after

    def note_poll_attempt(self) -> None:
        """Book the single revival knock - call it when the frame is queued."""
        if not self.silenced:
            return
        self._silenced_at = self.ctx.now()
        _LOGGER.debug("Asking the silenced address %s once more", self.where)

    def publish_reachability(self) -> None:
        """Publish whether this address answers (retained, only devices)."""
        if not self.section:
            return
        self.publish(self.reachability_topic,
                     PAYLOAD_OFFLINE if self.silenced else PAYLOAD_ONLINE)

    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": [self.object_id],
            "name": self.name,
            "manufacturer": MANUFACTURER,
            "model": self.model,
            "via_device": self.ctx.bridge_id,
        }

    def availability(self) -> dict[str, Any]:
        """Available while the add-on runs, the SCS link is up *and* the address answers.

        Only the add-on topic carries the last will: with the link topic alone
        an entity would stay "available" after the add-on died.  A device also
        carries its own topic, so a silenced address shows up as unavailable
        instead of pretending to know a state (it is published retained, so
        Home Assistant knows it right after a restart).
        """
        topics = [topic for topic in (self.ctx.bridge_availability,
                                      self.ctx.availability_topic,
                                      self.reachability_topic if self.section else "") if topic]
        if len(topics) == 1:
            return {
                "availability_topic": topics[0],
                "payload_available": PAYLOAD_ONLINE,
                "payload_not_available": PAYLOAD_OFFLINE,
            }
        return {
            "availability": [
                {"topic": topic, "payload_available": PAYLOAD_ONLINE,
                 "payload_not_available": PAYLOAD_OFFLINE}
                for topic in topics
            ],
            "availability_mode": "all",
        }

    def base_payload(self, suffix: str = "", name: str = "") -> dict[str, Any]:
        """Discovery fields that every entity shares."""
        payload: dict[str, Any] = {
            "unique_id": self.object_id + (("_" + suffix) if suffix else ""),
            "device": self.device_info(),
            "origin": {"name": ORIGIN_NAME, "sw_version": __version__},
            # None: the main entity takes the name of its device.
            "name": name or None,
        }
        payload.update(self.availability())
        return payload

    # ------------------------------------------------------------- interface

    def discovery(self) -> list[Discovery]:
        """Entities to announce.  Subclasses must implement this."""
        raise NotImplementedError

    def to_config(self) -> dict[str, Any]:
        """This entity as a devices.yaml entry.

        Starts from what the user actually wrote, so that options a subclass
        does not model survive the round trip through the web interface -
        otherwise saving twice would quietly drop them.  Subclasses start with
        ``super().to_config()``.

        The name goes back in without the instance's prefix: it belongs to the
        instance, not to the device, and saving twice would stack it up.
        """
        config = {
            key: value for key, value in self.config.items()
            if key not in ("section", "discovered")
        }
        config["where"] = self.where
        config["name"] = self.base_name
        return config

    def match_keys(self) -> Iterable[str]:
        """Bus addresses (WHERE with suffix) routed to this entity."""
        return (self.where,)

    def subscriptions(self) -> Iterable[str]:
        """MQTT command topics this entity listens on."""
        return ()

    def poll_frames(self) -> Iterable[str]:
        """Frames that refresh our state, sent at start-up and periodically."""
        return ()

    def probe_frames(self) -> Iterable[str]:
        """The subset of poll frames whose refusal is an answer, not overload."""
        return ()

    def followup_frames(self) -> Iterable[str]:
        """Questions a bus scan asks after it found this device."""
        return ()

    def on_command(self, topic: str, payload: str) -> None:
        """Handle an MQTT command addressed to one of our topics."""

    def on_own(self, message: OwnMessage) -> None:
        """Handle an OpenWebNet frame that carries our WHO/WHERE."""

    def tick(self) -> None:
        """Called once a second when ``needs_tick`` is set."""

    def publish_state(self) -> None:
        """Publish the current state."""

    def republish(self) -> None:
        """Publish everything again, e.g. after Home Assistant restarted."""
        self.publish_state()

    # -------------------------------------------------------------- helpers

    def send(self, frame: str) -> None:
        self.ctx.send(frame)

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        self.ctx.publish(topic, payload, retain=retain)
