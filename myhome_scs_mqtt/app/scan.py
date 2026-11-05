"""Active bus scan: ask the SCS bus what is out there.

Passive discovery only learns a device once somebody presses a switch.  A scan
instead walks the bus and makes every device answer, the same way MyHOME_Suite
does during its diagnostics run.  Everything here is read-only: status and
dimension *requests*, never a command that switches something.

The plan is pure data so it can be checked without touching a gateway; the
bridge executes it and lets the normal discovery path create the entities from
the replies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple

from .own import messages as msg

# Addresses that never describe a single device: 0 is "general" (also written
# 00 or 000) and "#n" a group.
GENERAL = re.compile(r"^(0+|#\d+)$")

# Section names of devices.yaml - the same as Entity.section of the device
# types (a test keeps both in step).
SECTION_LIGHTS = "lights"
SECTION_SWITCHES = "switches"
SECTION_COVERS = "covers"
SECTION_CLIMATE = "climate"
SECTION_ENERGY = "energy"
SECTION_CONTACTS = "binary_sensors"
SECTION_BUTTONS = "buttons"
SECTION_SCENES = "scenes"
SECTION_MOTION = "motion_sensors"
SECTION_PUMPS = "pumps"

WHAT_LIGHT_OFF, WHAT_LIGHT_ON, WHAT_LIGHT_MAX_STEP = 0, 1, 10
COVER_WHATS = ("0", "1", "2")
DIM_LIGHT_LEVEL = ("1", "4")
DIM_SHUTTER_STATUS = "10"
# Measured temperature, setpoint in force (a fan coil reports 12 first when it
# is switched on) and programmed setpoint - any of them proves a zone.
DIM_ZONE_REPORTS = ("0", "12", "14")
DIM_METER_POWER = "113"
CONTACT_PREFIX = "3"
CEN_PLUS_KEYPAD_PREFIX = "2"


@dataclass
class ScanOptions:
    zones: int = 20        # heating zones 1..N
    meters: int = 8        # energy meters 5N
    contacts: int = 20     # dry contacts 3N
    deep: bool = False     # sweep every A/PL address instead of areas only


@dataclass
class ScanPhase:
    label: str
    frames: list[str] = field(default_factory=list)


class Classified(NamedTuple):
    """What a frame says about the device that sent it."""

    section: str
    where: str
    extra: dict[str, Any]


def build_plan(options: ScanOptions) -> list[ScanPhase]:
    """The list of probes a scan sends, grouped into reportable phases."""
    phases: list[ScanPhase] = [
        # WHO=13: type and firmware of the gateway itself.
        ScanPhase("Gateway", [
            msg.dimension_request(msg.WHO_GATEWAY, "", msg.DIM_GATEWAY_FIRMWARE),
            msg.dimension_request(msg.WHO_GATEWAY, "", msg.DIM_GATEWAY_TYPE),
        ]),
    ]

    # The general request makes every actuator answer at once; the per-area
    # requests catch the gateways that ignore the general one.
    for label, who in (("Licht", msg.WHO_LIGHTING), ("Rollläden", msg.WHO_AUTOMATION)):
        frames = [msg.status_request(who, "0")]
        frames += [msg.status_request(who, str(area)) for area in range(1, 10)]
        phases.append(ScanPhase(label, frames))

    zones = max(0, min(options.zones, 99))
    if zones:
        phases.append(ScanPhase("Heizzonen", [
            msg.dimension_request(msg.WHO_THERMO, str(zone), "0")
            for zone in range(1, zones + 1)
        ]))
    if options.meters > 0:
        phases.append(ScanPhase("Energiezähler", [
            msg.dimension_request(msg.WHO_ENERGY, f"5{meter}", DIM_METER_POWER)
            for meter in range(1, options.meters + 1)
        ]))
    if options.contacts > 0:
        phases.append(ScanPhase("Kontakte", [
            msg.status_request(msg.WHO_CEN_PLUS, f"{CONTACT_PREFIX}{contact}")
            for contact in range(1, options.contacts + 1)
        ]))
    if options.deep:
        # Every light point and shutter address, including the four digit form
        # used when the area or the light point number exceeds 9.
        deep: list[str] = []
        for area in range(0, 11):
            for point in range(1, 16):
                where = f"{area}{point}" if area < 10 and point < 10 else f"{area:02d}{point:02d}"
                deep.append(msg.status_request(msg.WHO_LIGHTING, where))
                deep.append(msg.status_request(msg.WHO_AUTOMATION, where))
        phases.append(ScanPhase("Feinsuche", deep))
    return phases


def plan_size(phases: list[ScanPhase]) -> int:
    return sum(len(phase.frames) for phase in phases)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def _number(text: str) -> int | None:
    return int(text) if text.isascii() and text.isdigit() else None


def classify(message: msg.OwnMessage) -> Classified | None:
    """Decide what an unsolicited frame says about the device that sent it.

    Returns None for anything that must not become an entity: group and area
    addresses, light management, unsupported WHOs.
    """
    where = message.full_where
    if not where or GENERAL.match(where):
        return None
    who = message.who
    extra: dict[str, Any] = {}

    if who == msg.WHO_LIGHTING:
        if message.kind == msg.KIND_NORMAL:
            what = _number(message.what)
            if what is None or what > WHAT_LIGHT_MAX_STEP:
                return None      # e.g. WHAT 34: light management, not a lamp
            if what > WHAT_LIGHT_ON:
                # Only note what was seen: an absent flag reads better in the
                # generated file than "dimmer: false" everywhere.
                extra["dimmer"] = True
        elif message.kind == msg.KIND_DIMENSION_RESPONSE:
            if message.dimension in DIM_LIGHT_LEVEL:
                # A DALI lamp answers in dimension 4 while it is off, in 1 once lit.
                extra["dimmer"] = True
            elif message.dimension == msg.DIM_COLOR_HSV:
                # Colour and white temperature come from a DALI lamp, which
                # always dims as well (berichte/18-messung-dali-schreiben.md).
                extra["dimmer"] = True
                extra["rgbw"] = True
            elif message.dimension == msg.DIM_WHITE_TEMP:
                extra["dimmer"] = True
                extra["tunable_white"] = True
            else:
                return None
        else:
            return None
        if len(where) < 2:
            return None          # "1".."9" address a whole area, not a light point
        return Classified(SECTION_LIGHTS, where, extra)

    if who == msg.WHO_AUTOMATION:
        known = ((message.kind == msg.KIND_NORMAL and message.what in COVER_WHATS)
                 or (message.kind == msg.KIND_DIMENSION_RESPONSE
                     and message.dimension == DIM_SHUTTER_STATUS))
        # Answering dimension 10 proves the actuator reports a position, not
        # that it accepts one - "advanced" stays out of the generated file.
        if not known or len(where) < 2:
            return None
        return Classified(SECTION_COVERS, where, extra)

    if who == msg.WHO_THERMO:
        if (message.kind == msg.KIND_DIMENSION_RESPONSE
                and message.dimension in DIM_ZONE_REPORTS):
            zone = where.lstrip("#")
            if _number(zone) is None or _number(zone) == 0:
                return None
            return Classified(SECTION_CLIMATE, zone, extra)
        return None

    if who == msg.WHO_ENERGY:
        if message.kind == msg.KIND_DIMENSION_RESPONSE:
            return Classified(SECTION_ENERGY, where, extra)
        return None

    if who == msg.WHO_CEN_PLUS:
        if message.kind != msg.KIND_NORMAL:
            # A bare request - the scan's own "*#25*312##" or a second client's -
            # proves nothing; taking it for a device made phantom contacts.
            return None
        if where.startswith(CONTACT_PREFIX) and len(where) > 1:
            # The bus says 399, the contact is number 99.
            return Classified(SECTION_CONTACTS, where[1:], extra)
        if where.startswith(CEN_PLUS_KEYPAD_PREFIX):
            return Classified(SECTION_BUTTONS, where, {"kind": "cen_plus"})
        return None

    if who == msg.WHO_CEN:
        if message.kind != msg.KIND_NORMAL:
            return None
        return Classified(SECTION_BUTTONS, where, {"kind": "cen"})

    return None


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

SECTION_ORDER = [
    SECTION_LIGHTS, SECTION_SWITCHES, SECTION_COVERS, SECTION_CLIMATE, SECTION_PUMPS,
    SECTION_ENERGY, SECTION_CONTACTS, SECTION_MOTION, SECTION_BUTTONS, SECTION_SCENES,
]

SECTION_TITLES = {
    SECTION_LIGHTS: "Licht und Dimmer",
    SECTION_SWITCHES: "Schaltaktoren",
    SECTION_COVERS: "Rollläden",
    SECTION_CLIMATE: "Heizzonen",
    SECTION_PUMPS: "Pumpen und Heizungsaktoren",
    SECTION_ENERGY: "Energiezähler",
    SECTION_CONTACTS: "Kontakte",
    SECTION_MOTION: "Melder (BMSE)",
    SECTION_BUTTONS: "Taster (CEN / CEN+)",
    SECTION_SCENES: "Szenarien",
}

_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _render_key(key: object) -> str:
    if isinstance(key, bool):
        return _render_value(key)
    if isinstance(key, int):
        return str(key)
    text = str(key)
    return text if _PLAIN_KEY.match(text) else json.dumps(text, ensure_ascii=False)


def _render_value(value: object) -> str:
    """One YAML value in flow style; strings always quoted ("05" stays text)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_render_key(key)}: {_render_value(item)}"
                               for key, item in value.items()) + "}"
    # A JSON string is a valid YAML double-quoted scalar - quotes, backslashes
    # and line breaks included.
    return json.dumps(str(value), ensure_ascii=False)


def render_devices_yaml(sections: dict[str, list[dict[str, Any]]], found: int = 0,
                        generated: datetime | None = None) -> str:
    """Turn the known devices into a ready to use devices.yaml."""
    stamp = (generated or datetime.now()).strftime("%d.%m.%Y %H:%M")
    lines = [
        f"# Ergebnis des Bus-Scans vom {stamp}",
        f"# {found} Geräte gefunden.",
        "#",
        "# Namen anpassen und den Inhalt nach devices.yaml übernehmen - Einträge",
        "# dort haben Vorrang vor der automatischen Erkennung.",
        "",
    ]
    order = SECTION_ORDER + sorted(name for name in sections if name not in SECTION_ORDER)
    for section in order:
        items = sections.get(section)
        if not items:
            continue
        lines.append(f"# {SECTION_TITLES.get(section, section)}")
        lines.append(f"{_render_key(section)}:")
        for item in items:
            keys = ["where", "name"] + [key for key in item if key not in ("where", "name")]
            first = True
            for key in keys:
                if key not in item or item[key] is None:
                    continue
                prefix = "  - " if first else "    "
                lines.append(f"{prefix}{_render_key(key)}: {_render_value(item[key])}")
                first = False
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
