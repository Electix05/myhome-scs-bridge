"""MQTT discovery: announce the entities, list the devices, remove leftovers.

Home Assistant keeps an entity for as long as its retained discovery message
exists.  Removing a device from the configuration therefore has to remove that
message too - otherwise the entity could never be deleted at all.  Only topics
of this add-on are ever touched: under its own base topic, and discovery
messages under its own discovery node
(``<prefix>/<component>/<node>/<object_id>/config``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from ..devices.base import Entity
from ..mqtt import collect_retained
from ..settings import MqttConfig
from .registry import Registry

_LOGGER = logging.getLogger(__name__)

# Topics earlier versions published and this one no longer does.  They live
# under the bridge's own prefix, so the general rule would keep them.
RETIRED_BRIDGE_TOPICS = ("last_frame",)
INVENTORY_SECONDS = 3.0

# The safety belt of the clean-up.  Removing a retained discovery message makes
# Home Assistant forget the entity - its name, its area, its place in every
# automation.  A few devices at a time is what removing them in the device file
# looks like.  Many at once is what a mistake looks like: a device file path
# that points nowhere, an add-on that was moved without its data, a discovery
# memory that is no longer read.  Then nothing is removed until somebody
# confirms it in the web interface.
PURGE_LIMIT_DEVICES = 5
# A device announces up to eight entities (a keypad); more leftover
# announcements than the devices that went away can explain are held as well -
# entities that vanish while their device stays are no ordinary removal.
ENTITIES_PER_DEVICE = 8
PURGE_EXAMPLES = 20

Collector = Callable[[MqttConfig, list[str], float], "list[str] | None"]


@dataclass(frozen=True)
class HeldPurge:
    """A clean-up that was too big to run without being asked.

    ``devices`` are the topic roots below the base topic that would go
    (``light/12``), ``entities`` counts the discovery messages.
    """

    devices: tuple[str, ...]
    entities: int

    def as_dict(self) -> dict[str, object]:
        """For the web interface: counts and the first few names."""
        return {"devices": len(self.devices), "entities": self.entities,
                "examples": list(self.devices[:PURGE_EXAMPLES])}


class Publisher(Protocol):
    def publish(self, topic: str, payload: str, retain: bool = False, qos: int = 0) -> None: ...


class Discovery:
    """Everything the bridge announces about its entities."""

    def __init__(self, publisher: Publisher, registry: Registry, config: MqttConfig,
                 collect: Collector = collect_retained) -> None:
        self.publisher = publisher
        self.registry = registry
        self.config = config
        self._collect = collect
        self._reported_unconfigured: set[str] = set()
        # Set while a clean-up waits for its confirmation, see purge_stale().
        self.held: HeldPurge | None = None

    @property
    def prefix(self) -> str:
        return self.config.discovery_prefix

    @property
    def base(self) -> str:
        return self.config.base_topic

    @property
    def node(self) -> str:
        """The node level of every discovery topic: the instance id.

        Home Assistant does not use it, but it keeps the announcements of this
        instance in a branch of their own: the old add-on (myhome_scs_mqtt)
        only looks at ``<prefix>/+/+/config`` and never sees them, our clean-up
        only ever collects ``<prefix>/+/<node>/+/config``, and a second
        instance of this add-on has a node of its own.
        """
        return self.config.instance_id

    @property
    def directory_topic(self) -> str:
        return f"{self.base}/bridge/devices"

    def config_topic(self, component: str, object_id: str) -> str:
        return f"{self.prefix}/{component}/{self.node}/{object_id}/config"

    def _is_our_config(self, topic: str) -> bool:
        """``<prefix>/<component>/<our node>/<object_id>/config`` - nothing else."""
        if not topic.startswith(self.prefix + "/"):
            return False
        parts = topic[len(self.prefix) + 1:].split("/")
        return len(parts) == 4 and parts[1] == self.node and parts[3] == "config"

    # ------------------------------------------------------------- announce

    def publish_all(self) -> int:
        """Announce every entity and the directory; returns the entity count."""
        count = 0
        for entity in self.registry.entities:
            count += self.publish_entity(entity)
        self.publish_directory()
        # Prefix and base topic in the line: after changing either option the
        # old retained topics stay behind - this is where a search starts.
        _LOGGER.info("Published MQTT discovery for %d Home Assistant entities "
                     "(%s/+/%s/..., topics under %s/)", count, self.prefix, self.node,
                     self.base)
        return count

    def publish_entity(self, entity: Entity) -> int:
        """Announce one entity (and its name next to its state)."""
        announced = entity.discovery()
        for component, object_id, payload in announced:
            self.publisher.publish(self.config_topic(component, object_id),
                                   json.dumps(payload, ensure_ascii=False), retain=True, qos=1)
        if entity.section:
            # Right next to the state, so an address in an MQTT browser can be
            # read without looking anything up.
            self.publisher.publish(entity.name_topic, entity.name, retain=True)
            # The announcement names a reachability topic; without a payload on
            # it Home Assistant would leave the entity unavailable for good.
            entity.publish_reachability()
            if not entity.configured and entity.object_id not in self._reported_unconfigured:
                self._reported_unconfigured.add(entity.object_id)
                _LOGGER.info("%s (%s %s): noch nicht zugeordnet - bleibt außen vor",
                             entity.name, entity.section, entity.where)
        return len(announced)

    def publish_directory(self) -> None:
        """One topic that lists every device - the map for an MQTT browser."""
        devices = sorted(
            ({"type": entity.topic_group, "where": entity.where, "name": entity.name,
              "topic": entity.topic_root} for entity in self.registry.device_entities()),
            key=lambda item: (item["type"], item["where"]),
        )
        self.publisher.publish(self.directory_topic,
                               json.dumps(devices, ensure_ascii=False, indent=1), retain=True)

    # ---------------------------------------------------------------- purge

    def stale_topics(self, stored: list[str]) -> list[str]:
        """The retained topics of this add-on that no entity publishes any more."""
        live_configs = {
            self.config_topic(component, object_id)
            for entity in self.registry.entities
            for component, object_id, _payload in entity.discovery()
        }
        bridge_prefix = f"{self.base}/bridge/"
        live_prefixes = tuple(entity.topic_root + "/" for entity in self.registry.entities)
        retired = {bridge_prefix + name for name in RETIRED_BRIDGE_TOPICS}

        stale = []
        for topic in stored:
            if topic in retired:
                stale.append(topic)
            elif topic.startswith(self.prefix + "/"):
                # Somebody else's device stays untouched - the old add-on's and
                # a second instance of this one alike: only announcements under
                # our own node count.
                if self._is_our_config(topic) and topic not in live_configs:
                    stale.append(topic)
            elif topic.startswith(self.base + "/"):
                if not topic.startswith(bridge_prefix) and not topic.startswith(live_prefixes):
                    stale.append(topic)
        return stale

    def too_much(self, stale: list[str]) -> HeldPurge | None:
        """What a clean-up of ``stale`` would take away, if that is a lot.

        None when it is small enough to simply run (see PURGE_LIMIT_DEVICES).
        """
        bridge_prefix = f"{self.base}/bridge/"
        devices: set[str] = set()
        entities: list[str] = []
        for topic in stale:
            if self._is_our_config(topic):
                entities.append(topic.split("/")[-2])
            elif topic.startswith(self.base + "/") and not topic.startswith(bridge_prefix):
                parts = topic[len(self.base) + 1:].split("/")
                if len(parts) >= 2:
                    devices.add("/".join(parts[:2]))
        if not entities:
            # Only states and names: Home Assistant loses nothing with those.
            return None
        explained = len(devices) * ENTITIES_PER_DEVICE
        if len(devices) <= PURGE_LIMIT_DEVICES and len(entities) <= explained + PURGE_LIMIT_DEVICES:
            return None
        # Entities without a device that went away: name them by their id.
        names = sorted(devices) or sorted(entities)
        return HeldPurge(tuple(names), len(entities))

    async def purge_stale(self, confirmed: bool = False) -> int | None:
        """Remove what the broker still holds for devices that are gone.

        Returns the number of removed topics, or None when the broker could not
        be asked - then nothing is removed.  A clean-up that would take many
        devices at once removes nothing either and returns 0; it waits in
        :attr:`held` until it is asked for again with ``confirmed=True``.
        """
        patterns = [f"{self.base}/#", f"{self.prefix}/+/{self.node}/+/config"]
        stored = await asyncio.to_thread(self._collect, self.config, patterns,
                                         INVENTORY_SECONDS)
        if stored is None:
            _LOGGER.warning("Leftovers of removed devices stay in the broker for now")
            return None
        stale = self.stale_topics(stored)
        self.held = None if confirmed else self.too_much(stale)
        if self.held is not None:
            _LOGGER.warning(
                "%d devices (%d Home Assistant entities) are no longer in the device "
                "file or the discovery memory: %s%s. That many at once looks like a "
                "mistake - a wrong devices_file, an add-on moved without its data, a "
                "typo. Nothing is removed from Home Assistant until it is confirmed in "
                "the web interface.",
                len(self.held.devices), self.held.entities,
                ", ".join(self.held.devices[:PURGE_EXAMPLES]),
                " ..." if len(self.held.devices) > PURGE_EXAMPLES else "")
            return 0
        for topic in stale:
            self.publisher.publish(topic, "", retain=True, qos=1)
        if stale:
            _LOGGER.info("Removed %d leftover topics of devices that no longer exist",
                         len(stale))
            for topic in stale[:10]:
                _LOGGER.debug("  removed %s", topic)
        return len(stale)
