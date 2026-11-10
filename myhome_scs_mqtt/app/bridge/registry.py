"""Which entities exist, and which of them a frame or a command belongs to.

Entities come from two sources: the device file (devices.yaml), which always
wins, and the automatic discovery, which is remembered in discovered.json so a
restart does not have to wait for every device to show up on the bus again.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..devices.base import Context, Entity
from ..devices.climate import Climate, ThermoActuator
from ..devices.cover import Cover
from ..devices.events import DryContact, Keypad, Scenario
from ..devices.light import Light, Switch
from ..devices.lightmgmt import BmseSensor
from ..devices.sensors import EnergyMeter
from ..own.messages import OwnMessage, parse
from ..scan import Classified
from ..settings import atomic_write_text

_LOGGER = logging.getLogger(__name__)

FACTORIES: dict[str, type[Entity]] = {
    factory.section: factory
    for factory in (Light, Switch, Cover, Climate, ThermoActuator, EnergyMeter,
                    DryContact, BmseSensor, Keypad, Scenario)
}

# Upper bound for automatically created entities - a bus full of garbage must
# not grow the registry (and Home Assistant) without end.
MAX_AUTO_ENTITIES = 500

# What a factory may raise on odd user data ("travel_time: fast").
_ENTRY_ERRORS = (ValueError, TypeError, AttributeError)


class Registry:
    """All entities, routed by bus address and by MQTT command topic."""

    def __init__(self, context: Context, max_auto_entities: int = MAX_AUTO_ENTITIES) -> None:
        self.context = context
        self.max_auto_entities = max_auto_entities
        self.entities: list[Entity] = []
        # The items as stored in discovered.json, in creation order.
        self.discovered: list[dict[str, Any]] = []
        self._by_address: dict[tuple[str, str], list[Entity]] = {}
        self._by_topic: dict[str, Entity] = {}
        self._limit_reported = False
        # What build() could not turn into an entity although it is written
        # down - in the device file (problems) or in the discovery memory
        # (memory_problems).  While either is not empty the bridge removes
        # nothing from the broker: the device behind a mistyped section or a
        # value that cannot be read still exists, and Home Assistant would
        # forget it for good.
        self.problems: list[str] = []
        self.memory_problems: list[str] = []

    # ---------------------------------------------------------------- build

    def build(self, definitions: dict[str, list[dict[str, Any]]],
              discovered: list[dict[str, Any]]) -> None:
        """Create the configured entities first, then the remembered ones.

        Whatever cannot be created is listed in :attr:`problems` (device file)
        or :attr:`memory_problems` (discovery memory).  An address that is
        merely defined twice is no problem - its device exists.
        """
        for section, items in definitions.items():
            factory = FACTORIES.get(section)
            if factory is None:
                _LOGGER.warning("Unknown section '%s' in the device file - ignored", section)
                if items:
                    self.problems.append(
                        f"Abschnitt '{section}' ist unbekannt (Tippfehler im Namen?) - "
                        f"nicht gelesen: {len(items)} "
                        f"{'Eintrag' if len(items) == 1 else 'Einträge'}")
                continue
            for item in items:
                entity = self._create(factory, item)
                if entity is None:
                    self.problems.append(
                        f"{section} {item.get('where')}: der Eintrag lässt sich nicht "
                        "lesen (ein Wert passt nicht, siehe Protokoll)")
                    continue
                existing = self.duplicate_of(entity)
                if existing is not None:
                    _LOGGER.warning("%s %s is defined twice - the second entry is ignored",
                                    section, entity.where)
                    continue
                self.register(entity)

        for item in discovered:
            section = str(item.get("section"))
            factory = FACTORIES.get(section)
            entity = None if factory is None else self._create(factory,
                                                               dict(item, discovered=True))
            if entity is None:
                # Kept in the memory as it is: dropping it here would lose the
                # entry with the next write, and its entity one start later.
                self.memory_problems.append(
                    f"Automatisch erkanntes Gerät {section} {item.get('where')} lässt "
                    "sich nicht lesen - es bleibt gemerkt, bis die Geräteliste in der "
                    "Weboberfläche gespeichert wird")
                self.discovered.append(dict(item))
                continue
            if self.duplicate_of(entity) is not None:
                continue    # the device file names this address itself
            self.register(entity)
            self.discovered.append(dict(item))

        _LOGGER.info("%d entities configured (%d of them auto-discovered)",
                     len(self.device_entities()), len(self.discovered))

    def _create(self, factory: type[Entity], item: dict[str, Any]) -> Entity | None:
        try:
            return factory(self.context, item)
        except _ENTRY_ERRORS as err:
            _LOGGER.error("Cannot create %s %s: %s", factory.section, item.get("where"), err)
            return None

    def register(self, entity: Entity) -> list[str]:
        """Add an entity; returns the command topics that are new."""
        self.entities.append(entity)
        for key in entity.match_keys():
            self._by_address.setdefault((entity.who, key), []).append(entity)
        return self.add_topics(entity)

    def add_topics(self, entity: Entity) -> list[str]:
        """Route the command topics of an entity; returns the new ones.

        Called again when an entity re-announces itself, because what it
        listens on may have changed.
        """
        new = []
        for topic in entity.subscriptions():
            owner = self._by_topic.get(topic)
            if owner is entity:
                continue
            if owner is not None:
                _LOGGER.warning("%s and %s share the topic %s", owner.name, entity.name, topic)
            self._by_topic[topic] = entity
            new.append(topic)
        return new

    def duplicate_of(self, entity: Entity) -> Entity | None:
        """The registered entity that describes the same device, if any.

        The same section and *bus* address is a duplicate, and so is an entity
        of the same WHO on the same bus address - a switch and a light at "12"
        are one actuator.  Only entities that share an address on purpose (a
        BMSE sensor next to a light point) are allowed to coexist.

        The bus address, not ``where``: actuator 1 of zone 8 (``8#1``) and of
        zone 9 (``9#1``) are both written ``where: "1"``, and so are contact 1
        (``31``) and IR interface 1 (``41``) - two devices each.
        """
        keys = set(entity.match_keys())
        for existing in self.entities:
            if existing.section == entity.section and keys.intersection(existing.match_keys()):
                return existing
        if entity.exclusive_address:
            for key in keys:
                for existing in self._by_address.get((entity.who, key), ()):
                    if existing.exclusive_address:
                        return existing
        return None

    # -------------------------------------------------------------- lookups

    def route(self, message: OwnMessage) -> list[Entity]:
        """The entities a frame belongs to - full bus address first."""
        full = message.full_where
        targets = list(self._by_address.get((message.who, full), ()))
        if full != message.where:
            # A zone actuator "8#1" also tells the zone "8" what runs.
            targets += [entity for entity in self._by_address.get((message.who, message.where), ())
                        if entity not in targets]
        return targets

    def for_frame(self, frame: str) -> list[Entity]:
        """The entities a frame *we sent* is addressed to.

        Lets the sender book the gateway's answer where it belongs: an address
        that is refused again and again is silenced (Entity.note_refusal).
        ``parse`` never raises, an unknown shape simply matches nothing.
        """
        return self.route(parse(frame))

    def by_topic(self, topic: str) -> Entity | None:
        return self._by_topic.get(topic)

    @property
    def topics(self) -> list[str]:
        return sorted(self._by_topic)

    def device_entities(self) -> list[Entity]:
        """Everything that is a device of the installation (not the add-on)."""
        return [entity for entity in self.entities if entity.section]

    def ticking(self) -> list[Entity]:
        return [entity for entity in self.entities if entity.needs_tick]

    def known(self, section: str, where: str) -> bool:
        return any(entity.section == section and entity.where == where
                   for entity in self.entities)

    # ------------------------------------------------------- auto discovery

    def auto_create(self, classified: Classified) -> Entity | None:
        """Create an entity for an address that showed up on the bus."""
        factory = FACTORIES.get(classified.section)
        if factory is None:
            return None
        if len(self.discovered) >= self.max_auto_entities:
            if not self._limit_reported:
                _LOGGER.warning("%d devices discovered automatically - no more are added",
                                self.max_auto_entities)
                self._limit_reported = True
            return None
        item = dict(classified.extra, section=classified.section, where=classified.where)
        entity = self._create(factory, dict(item, discovered=True))
        if entity is None or self.duplicate_of(entity) is not None:
            return None
        self.register(entity)
        self.discovered.append(item)
        return entity

    def _memory_of(self, entity: Entity) -> dict[str, Any] | None:
        for item in self.discovered:
            if item.get("section") == entity.section and item.get("where") == entity.where:
                return item
        return None

    def is_remembered(self, entity: Entity) -> bool:
        """True while this entity lives in the discovery memory only."""
        return self._memory_of(entity) is not None

    def remember(self, entity: Entity) -> bool:
        """Store what a discovered entity learned about itself; True if stored."""
        item = self._memory_of(entity)
        if item is None:
            return False
        item.update(entity.to_config())
        return True

    def forget_discovered(self, items: list[dict[str, Any]] | None = None) -> None:
        """Drop the discovery memory - these devices are in the device file now.

        ``items`` limits it to the given entries (the very objects of
        :attr:`discovered`): a device that showed up while the file was being
        written is in neither place yet and has to stay remembered.
        """
        if items is None:
            self.discovered = []
            return
        gone = {id(item) for item in items}
        self.discovered = [item for item in self.discovered if id(item) not in gone]


def load_discovered(path: str) -> tuple[list[dict[str, Any]], bool]:
    """Read discovered.json: ``(items, complete)``.

    A missing file is an empty but complete memory - a fresh installation.  A
    broken one is incomplete: the bridge must then not remove the entities of
    devices it merely failed to read back.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return [], True
    except (OSError, ValueError) as err:
        _LOGGER.warning("Cannot read %s: %s", path, err)
        return [], False
    if not isinstance(data, list):
        _LOGGER.warning("%s does not contain a list - ignored", path)
        return [], False
    items = [item for item in data
             if isinstance(item, dict) and isinstance(item.get("where"), str) and item["where"]]
    if len(items) != len(data):
        _LOGGER.warning("%s: %d unreadable entries ignored", path, len(data) - len(items))
    return items, len(items) == len(data)


def save_discovered(path: str, items: list[dict[str, Any]]) -> bool:
    """Write discovered.json atomically; False when it could not be written."""
    try:
        atomic_write_text(path, json.dumps(items, indent=2, ensure_ascii=False))
    except OSError as err:
        _LOGGER.warning("Cannot store discovered devices in %s: %s", path, err)
        return False
    return True
