"""Checks a device list before it is written - with the device types themselves.

What the web interface saves becomes the device file, and the device file is
read again with the next start.  An entry the bridge cannot read then counts as
a problem: nothing is removed from Home Assistant any more, and saving from the
page is locked until somebody corrects the file by hand.  So the list is
checked while it is still on the page.

Every entry is handed to the very class that will read it at the next start,
and to a registry that looks for doubles the way the real one does.  What
passes here cannot fail there.  A few things a device type accepts without
complaint but that cannot be meant are checked on top: a contact that is make
and break at once, a colour temperature range that is upside down.

Pure: nothing in here touches the bus, the broker or a file.
"""

from __future__ import annotations

import logging
import math
import re
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from ..devices import base as devices_base
from ..devices.base import Context, Entity
from ..devices.light import (
    DEFAULT_MAX_KELVIN, DEFAULT_MIN_KELVIN, MAX_SENSIBLE_KELVIN, MIN_SENSIBLE_KELVIN,
)
from ..web.schema import SCHEMA
from .registry import FACTORIES, Registry

# An OpenWebNet address: digits, with "#" between its parts ("12", "23#4#01")
# or in front of a group ("#3").  Anything else cannot be part of a frame.
ADDRESS = re.compile(r"^#?[0-9]+(#[0-9]+)*$")
DIGITS = re.compile(r"^[0-9]+$")

# What a device type may raise on odd data - the same as in the registry.
_ENTRY_ERRORS = (ValueError, TypeError, AttributeError)
_EMPTY = ("", None, False, [], {})

DEFAULT_MIN_TEMP = 5.0
DEFAULT_MAX_TEMP = 35.0

# One finding: which row, which field of it ("" = the row as a whole), and
# what is wrong, in words for the page.
Problem = dict[str, Any]
Report = Callable[[str, str], None]


def _number(item: dict[str, Any], key: str) -> float | None:
    """The number in a field; None when it is empty, NaN when it is no number."""
    value = item.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return math.nan
    try:
        number = float(value)
    except (TypeError, ValueError):
        return math.nan
    return number if math.isfinite(number) else math.nan


def _not_negative(item: dict[str, Any], report: Report, *keys: str) -> None:
    for key in keys:
        number = _number(item, key)
        if number is None:
            continue
        if math.isnan(number):
            report(key, "Hier gehört eine Zahl hin.")
        elif number < 0:
            report(key, "Der Wert darf nicht negativ sein.")


def _check_light(item: dict[str, Any], report: Report) -> None:
    limits = {}
    for key, default in (("min_kelvin", DEFAULT_MIN_KELVIN), ("max_kelvin", DEFAULT_MAX_KELVIN)):
        number = _number(item, key)
        if number is None:
            limits[key] = float(default)
        elif math.isnan(number) or not MIN_SENSIBLE_KELVIN <= number <= MAX_SENSIBLE_KELVIN:
            report(key, f"Eine Weißtemperatur liegt zwischen {MIN_SENSIBLE_KELVIN} "
                        f"und {MAX_SENSIBLE_KELVIN} Kelvin.")
        else:
            limits[key] = number
    if len(limits) == 2 and limits["min_kelvin"] >= limits["max_kelvin"]:
        report("max_kelvin", f"Kalt ({limits['max_kelvin']:g} K) muss über Warm "
                             f"({limits['min_kelvin']:g} K) liegen.")


def _check_cover(item: dict[str, Any], report: Report) -> None:
    _not_negative(item, report, "travel_time", "tilt_time")


def _check_climate(item: dict[str, Any], report: Report) -> None:
    limits = {}
    for key, default in (("min_temp", DEFAULT_MIN_TEMP), ("max_temp", DEFAULT_MAX_TEMP)):
        number = _number(item, key)
        if number is None:
            limits[key] = default
        elif math.isnan(number):
            report(key, "Hier gehört eine Zahl hin.")
        else:
            limits[key] = number
    if len(limits) == 2 and limits["min_temp"] >= limits["max_temp"]:
        report("max_temp", f"max ({limits['max_temp']:g} °C) muss über min "
                           f"({limits['min_temp']:g} °C) liegen.")


def _check_contact(item: dict[str, Any], report: Report) -> None:
    if item.get("schliesser") and item.get("oeffner"):
        report("oeffner", "Schließer und Öffner zugleich geht nicht - der Kontakt "
                          "bliebe außen vor.")


def _check_motion(item: dict[str, Any], report: Report) -> None:
    _not_negative(item, report, "motion_poll", "off_delay", "lux_interval")


def _check_pump(item: dict[str, Any], report: Report) -> None:
    zone = item.get("zone")
    if zone not in (None, "") and not DIGITS.match(str(zone)):
        report("zone", "Die Zone ist eine Nummer, z. B. 0 oder 8.")


def _check_switch(item: dict[str, Any], report: Report) -> None:
    who = item.get("who")
    if who not in (None, "") and not DIGITS.match(str(who)):
        report("who", "WHO ist eine ganze Zahl, z. B. 1.")


def _numbers_only(key: str, what: str) -> Callable[[dict[str, Any], Report], None]:
    def check(item: dict[str, Any], report: Report) -> None:
        values = item.get(key)
        if values in (None, ""):
            return
        if not isinstance(values, list) or not all(
                not isinstance(value, bool) and DIGITS.match(str(value)) for value in values):
            report(key, f"{what} sind Nummern, mit Komma getrennt: 1, 2, 3.")
    return check


_SECTION_CHECKS: dict[str, Callable[[dict[str, Any], Report], None]] = {
    "lights": _check_light,
    "covers": _check_cover,
    "climate": _check_climate,
    "binary_sensors": _check_contact,
    "motion_sensors": _check_motion,
    "pumps": _check_pump,
    "switches": _check_switch,
    "buttons": _numbers_only("buttons", "Tasten"),
    "scenes": _numbers_only("scenarios", "Szenarien"),
}


def _scratch_context() -> Context:
    """A context that goes nowhere: the entities built here never run."""
    return Context(base_topic="check", discovery_prefix="check",
                   availability_topic="check/availability",
                   publish=lambda *_args, **_kwargs: None, send=lambda _frame: None)


@contextmanager
def _quiet_device_types() -> Iterator[None]:
    """Keep what the device types log while being tried out of the add-on's log.

    The page asks while somebody is typing; a half-typed value would otherwise
    leave a warning in the log with every key press.  The check runs in one go
    on the event loop, so no real entity can log in between.
    """
    logger = logging.getLogger(devices_base.__name__.rsplit(".", 1)[0])
    handler = logging.NullHandler()
    propagate = logger.propagate
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield
    finally:
        logger.propagate = propagate
        logger.removeHandler(handler)


def is_blank(item: dict[str, Any]) -> bool:
    """A row somebody added and has not filled in - it is simply not saved."""
    return all(value in _EMPTY or (isinstance(value, str) and not value.strip())
               for value in item.values())


def check_devices(sections: object) -> list[Problem]:
    """Everything that is wrong with a device list, row by row.

    ``sections`` is what the page holds: ``{section: [entry, ...]}``, rows
    without an address included - the index of a finding is the position of
    the row on the page.  An empty list means the list can be saved.

    Raises:
        ValueError: when this is no device list at all.
    """
    if not isinstance(sections, dict):
        raise ValueError("Die Geräteliste muss ein Objekt sein.")
    problems: list[Problem] = []
    registry = Registry(_scratch_context())
    rows: dict[int, tuple[str, int]] = {}

    with _quiet_device_types():
        for section, items in sections.items():
            if not isinstance(items, list):
                raise ValueError(f"Abschnitt {section}: erwartet wird eine Liste.")
            factory = FACTORIES.get(section)
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(f"Abschnitt {section}, Eintrag {index + 1}: kein Objekt.")
                if is_blank(item):
                    continue
                found: list[Problem] = []

                def report(field: str, text: str, *, _section: str = section,
                           _index: int = index, _found: list[Problem] = found) -> None:
                    _found.append({"section": _section, "index": _index,
                                   "field": field, "text": text})

                _check_entry(section, item, report)
                if factory is None:
                    report("", f"Den Abschnitt '{section}' kennt das Add-on nicht.")
                elif not any(problem["field"] == "where" for problem in found):
                    # Built even when a field is wrong already: its address
                    # still has to be there for the rows below to be compared
                    # with.  Only a second remark about the same row is left out.
                    _try_out(factory, item, registry, rows, section, index, report,
                             said_already=bool(found))
                problems.extend(found)
    return problems


def _check_entry(section: str, item: dict[str, Any], report: Report) -> None:
    """What can be said about an entry without building it."""
    where = item.get("where")
    if not isinstance(where, str) or not where.strip():
        report("where", "Die Adresse fehlt - ohne sie wird die Zeile nicht gespeichert.")
    elif not ADDRESS.match(where.strip()):
        report("where", "Eine Adresse besteht aus Ziffern, bei Bedarf mit # dazwischen "
                        "(12, 0210, 23#4#01).")
    if "name" in item and not isinstance(item["name"], str):
        report("name", "Der Name ist kein Text.")
    check = _SECTION_CHECKS.get(section)
    if check is not None:
        check(item, report)


def _try_out(factory: type[Entity], item: dict[str, Any], registry: Registry,
             rows: dict[int, tuple[str, int]], section: str, index: int,
             report: Report, said_already: bool = False) -> None:
    """Build the entry the way the next start will, and look for its double."""
    try:
        entity = factory(registry.context, dict(item, where=str(item["where"]).strip()))
    except _ENTRY_ERRORS as err:
        if not said_already:
            report("", f"Der Eintrag lässt sich so nicht lesen ({err}).")
        return
    double = registry.duplicate_of(entity)
    if double is not None:
        other_section, other_index = rows[id(double)]
        place = f"Zeile {other_index + 1}"
        if other_section != section:
            label = SCHEMA.get(other_section, {}).get("label", other_section)
            place += f" unter „{label}“"
        report("where", f"Diese Adresse steht schon in {place} - auf dem Bus ist das "
                        "dasselbe Gerät.")
        return
    registry.register(entity)
    rows[id(entity)] = (section, index)
