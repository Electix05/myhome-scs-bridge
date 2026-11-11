"""What a device is doing right now, in a few words for the web interface.

The page shows it next to every row: whoever stands in front of a cabinet with
forty addresses wants to see which of them just went on.  Nothing in here
changes an entity - it only reads what the device types keep anyway, so the
protocol code stays as it is.
"""

from __future__ import annotations

from ..devices.base import Entity
from ..devices.climate import Climate, ThermoActuator
from ..devices.cover import Cover
from ..devices.events import DryContact
from ..devices.light import Light, Switch
from ..devices.lightmgmt import BmseSensor
from ..devices.sensors import EnergyMeter

# Sections whose devices can be switched by hand from the page to find out
# which lamp an address belongs to.  Lights and plain actuators only: nothing
# that moves (shutters) or regulates (heating) is switched "just to see".
TESTABLE = (Light.section, Switch.section)

SILENCED = "stillgelegt"
_COVER_STATES = {"open": "offen", "closed": "geschlossen", "opening": "öffnet",
                 "closing": "schließt"}


def device_key(entity: Entity) -> str:
    """``section|address`` as the page knows the row of this entity."""
    return f"{entity.section}|{entity.to_config().get('where', entity.where)}"


def _reported(entity: Entity) -> bool:
    """Contacts and heating actuators note themselves whether a state arrived:
    a frame that carried none (a request passing by) is no report."""
    return bool(getattr(entity, "_known", entity.heard))


def state_text(entity: Entity) -> str:
    """The state of a device in a few words; "" when nothing is known yet."""
    if entity.silenced:
        return SILENCED
    if isinstance(entity, Light):
        if not entity.heard:
            return ""
        if not entity.is_on:
            return "aus"
        return f"an · {round(entity.brightness * 100 / 255)} %" if entity.dimmer else "an"
    if isinstance(entity, Switch):
        if not entity.heard:
            return ""
        return "an" if entity.is_on else "aus"
    if isinstance(entity, Cover):
        if not entity.heard:
            return ""
        text = _COVER_STATES.get(entity.state, entity.state)
        if entity.reports_position:
            text += f" · {int(round(entity.position))} %"
        return text
    if isinstance(entity, Climate):
        if entity.current_temperature is None:
            return ""
        text = f"{entity.current_temperature:.1f} °C"
        if entity.heard:
            text += " · aus" if entity.mode == "off" else f" · Soll {entity.setpoint:.1f} °C"
        return text
    if isinstance(entity, EnergyMeter):
        return "" if entity.power is None else f"{entity.power} W"
    if isinstance(entity, DryContact):
        if not entity.configured:
            return "nicht zugeordnet"
        if not _reported(entity):
            return ""
        return "ausgelöst" if entity.is_on else "in Ruhe"
    if isinstance(entity, BmseSensor):
        parts = []
        if entity.lux is not None:
            parts.append(f"{entity.lux} lx")
        if entity.with_motion and entity.heard:
            parts.append("Bewegung" if entity.motion else "frei")
        return " · ".join(parts)
    if isinstance(entity, ThermoActuator):
        if not _reported(entity):
            return ""
        return "läuft" if entity.is_on else "steht"
    return ""
