"""WHO=4 - thermoregulation: heating zones, fan coils and their actuators.

Measured on real hardware (WISSEN.md "Heizung (WHO 4)" and "Fancoil-Zonen"):

* Temperatures are four digits: sign plus tenths of a degree (0243 = 24.3 °C).
* Dimension 12 is the setpoint in force, 14 the programmed one - they differ
  on a fan coil zone (29.0 against 21.0 °C).  12 wins.
* Zone commands go out in both address forms, ``#<zone>`` for a zone at a
  central unit and ``<zone>`` for a standalone thermostat.  Both are
  acknowledged, only the right one acts, and from outside nobody can tell which
  kind of zone it is - the commands are idempotent, so both are sent.
* The fan speed is written *without* ``#`` in front of the zone
  (``*#4*8*#11*1##`` ACK, ``*#4*#8*#11*1##`` NACK).
* Fan coil actuator codes: 5 = off, 6/7/8 = speed 1/2/3.  A plain valve keeps
  reporting 0 and 1.  The zone actuator (``<zone>#1``, dimension 20) reports a
  stop at once, the valve state (dimension 19) only minutes later.
* The circulation pump hangs on the central unit (``0#3``), not on the zone.
* The MH201 does not hand out the humidity (dimension 60), the F461 does.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from ..own import messages as msg
from .base import Context, Discovery, Entity, device_class, is_number, slug

_LOGGER = logging.getLogger(__name__)

DIM_MEASURED_TEMPERATURE = "0"
DIM_FAN = "11"
DIM_SETPOINT_ACTIVE = "12"
DIM_SETPOINT_PROGRAMMED = "14"
DIM_VALVES = "19"
DIM_ACTUATOR = "20"
DIM_HUMIDITY = "60"
# The "zone" of an actuator at the central unit: the pump is 0#3 (WISSEN.md).
CENTRAL_UNIT = "0"

# Fan coil actuators: 5 = off, 6/7/8 = speed 1/2/3.  Simple valves use 0 and 1.
FAN_STAGES = {"5": "aus", "6": "1", "7": "2", "8": "3"}
ACTIVE_CODES = frozenset({"1", "2", "6", "7", "8"})
# Dimension 11 is what the thermostat was told to do, not what runs.
FAN_SETTINGS = {"0": "auto", "1": "1", "2": "2", "3": "3"}
FAN_MODES = ["auto", "1", "2", "3"]
FAN_VALUES = {"auto": "0", "1": "1", "2": "2", "3": "3"}

WHAT_COOLING = "0"
WHAT_HEATING = "1"
WHAT_OFF = "303"
WHAT_ANTIFREEZE = "302"
WHAT_AUTO = "311"
OFF_CODES = ("303", "300")
PROTECTION_CODES = ("102", "202", "302")
# Mode 3 of a setpoint write means "manual setpoint until further notice".
SETPOINT_MODE_MANUAL = "3"

MODE_OFF = "off"
MODE_HEAT = "heat"
MODE_COOL = "cool"
MODE_AUTO = "auto"

ACTION_OFF = "off"
ACTION_IDLE = "idle"
ACTION_HEATING = "heating"
ACTION_COOLING = "cooling"

EXTRA_FAN = "fan"
EXTRA_HUMIDITY = "humidity"


class Climate(Entity):
    who = msg.WHO_THERMO
    component = "climate"
    topic_group = "climate"
    section = "climate"
    default_name = "Zone"
    model = "Thermo zone"

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context, config)
        self.zone = self.where.lstrip("#")
        # Two checkboxes instead of a list: heating, cooling or both.
        if "heizen" in config or "kuehlen" in config:
            modes = ([MODE_HEAT] if config.get("heizen", True) else []) + \
                    ([MODE_COOL] if config.get("kuehlen") else [])
        else:
            modes = [str(mode) for mode in (config.get("modes") or [MODE_HEAT])]
        self.can_heat = MODE_HEAT in modes
        self.can_cool = MODE_COOL in modes
        self.modes = [MODE_OFF] + modes + [MODE_AUTO]
        # A fan is detected, but can be set by hand; None means "detect".
        luefter = config.get("luefter")
        self.has_fan = None if luefter is None else bool(luefter)
        self.min_temp = float(config.get("min_temp", 5))
        self.max_temp = float(config.get("max_temp", 35))
        self.step = float(config.get("temp_step", 0.5))
        self.current_temperature: float | None = None
        self.humidity: int | None = None
        self._setpoint_from_active = False
        self.fan_setting: str | None = None     # what the thermostat is set to
        self.fan_stage: str | None = None       # what the actuator actually runs
        self._extras_seen: set[str] = set()
        self.setpoint = float(config.get("default_setpoint", 20))
        self.mode = MODE_HEAT if MODE_HEAT in self.modes else self.modes[-1]
        self.season = MODE_HEAT if MODE_HEAT in modes else MODE_COOL
        self.action = ACTION_IDLE

    def to_config(self) -> dict[str, Any]:
        config = super().to_config()
        config["where"] = self.zone
        config.pop("modes", None)
        config["heizen"] = self.can_heat
        config["kuehlen"] = self.can_cool
        if self.has_fan is not None:
            config["luefter"] = self.has_fan
        elif EXTRA_FAN in self._extras_seen:
            config["luefter"] = True
        else:
            # Left empty on purpose: "detect it yourself".  Writing False here
            # would switch the detection off for good on the next save.
            config.pop("luefter", None)
        return config

    # ------------------------------------------------------------- discovery

    @property
    def mode_topic(self) -> str:
        return self.topic_root + "/mode"

    @property
    def setpoint_topic(self) -> str:
        return self.topic_root + "/setpoint"

    @property
    def temperature_topic(self) -> str:
        return self.topic_root + "/temperature"

    @property
    def action_topic(self) -> str:
        return self.topic_root + "/action"

    @property
    def humidity_topic(self) -> str:
        return self.topic_root + "/humidity"

    @property
    def fan_topic(self) -> str:
        return self.topic_root + "/fan"

    @property
    def fan_mode_topic(self) -> str:
        return self.topic_root + "/fan_mode"

    def match_keys(self) -> tuple[str, ...]:
        return (self.zone, "#" + self.zone)

    @property
    def fan_available(self) -> bool:
        """The fan comes from the configuration or from what the zone reports."""
        if self.has_fan is not None:
            return self.has_fan
        return EXTRA_FAN in self._extras_seen

    def discovery(self) -> list[Discovery]:
        payload = self.base_payload()
        payload.update(
            {
                "modes": self.modes,
                "mode_state_topic": self.mode_topic,
                "mode_command_topic": self.mode_topic + "/set",
                "temperature_state_topic": self.setpoint_topic,
                "temperature_command_topic": self.setpoint_topic + "/set",
                "current_temperature_topic": self.temperature_topic,
                "action_topic": self.action_topic,
                "temperature_unit": "C",
                "min_temp": self.min_temp,
                "max_temp": self.max_temp,
                "temp_step": self.step,
                "precision": 0.1,
                "optimistic": False,
            }
        )
        # A fan control only where there is a fan - otherwise Home Assistant
        # shows a switch at every zone that reaches into nothing.
        if self.fan_available:
            payload.update(
                {
                    "fan_modes": FAN_MODES,
                    "fan_mode_state_topic": self.fan_mode_topic,
                    "fan_mode_command_topic": self.fan_mode_topic + "/set",
                }
            )
        entities: list[Discovery] = [(self.component, self.object_id, payload)]

        # Always: the measured temperature as an entity of its own.  A
        # thermostat carries it only as an attribute, graphs need a sensor.
        sensor = self.base_payload("temperature", name="Temperatur")
        sensor.update({"state_topic": self.temperature_topic, "device_class": "temperature",
                       "unit_of_measurement": "°C", "state_class": "measurement"})
        entities.append(("sensor", sensor["unique_id"], sensor))

        # Only zones that really answer get these - an entity that stays
        # empty is worse than none.
        if EXTRA_HUMIDITY in self._extras_seen:
            humidity = self.base_payload("humidity", name="Luftfeuchtigkeit")
            humidity.update({"state_topic": self.humidity_topic, "device_class": "humidity",
                             "unit_of_measurement": "%", "state_class": "measurement"})
            entities.append(("sensor", humidity["unique_id"], humidity))

        if self.fan_available:
            fan = self.base_payload("fan", name="Lüfterstufe")
            fan.update({"state_topic": self.fan_topic, "icon": "mdi:fan"})
            entities.append(("sensor", fan["unique_id"], fan))
        return entities

    def subscriptions(self) -> tuple[str, ...]:
        return (self.mode_topic + "/set", self.setpoint_topic + "/set",
                self.fan_mode_topic + "/set")

    def probe_frames(self) -> tuple[str, ...]:
        """Questions whose refusal is an answer: does the zone have this?"""
        frames = []
        if EXTRA_FAN not in self._extras_seen:
            frames.append(msg.dimension_request(self.who, self.zone, DIM_FAN))
        if EXTRA_HUMIDITY not in self._extras_seen:
            frames.append(msg.dimension_request(self.who, self.zone, DIM_HUMIDITY))
        return tuple(frames)

    def poll_frames(self) -> tuple[str, ...]:
        frames = [
            msg.status_request(self.who, self.zone),
            msg.dimension_request(self.who, self.zone, DIM_MEASURED_TEMPERATURE),
            msg.dimension_request(self.who, self.zone, DIM_SETPOINT_ACTIVE),
            msg.dimension_request(self.who, self.zone, DIM_VALVES),
        ]
        # Asked in every round until the zone has answered once: two frames
        # every five minutes are cheap, a sensor hidden until the next restart
        # because one answer got lost is not.
        frames.extend(self.probe_frames())
        if EXTRA_FAN in self._extras_seen:
            frames.append(msg.dimension_request(self.who, self.zone, DIM_FAN))
        if EXTRA_HUMIDITY in self._extras_seen:
            frames.append(msg.dimension_request(self.who, self.zone, DIM_HUMIDITY))
        return tuple(frames)

    # --------------------------------------------------------------- MQTT in

    def on_command(self, topic: str, payload: str) -> None:
        payload = payload.strip()
        if topic == self.fan_mode_topic + "/set":
            self._set_fan(payload.lower())
        elif topic == self.mode_topic + "/set":
            self._set_mode(payload.lower())
        elif topic == self.setpoint_topic + "/set":
            try:
                temperature = float(payload)
            except ValueError:
                temperature = math.nan
            if not math.isfinite(temperature):
                _LOGGER.warning("%s: not a temperature: %s", self.name, payload)
                return
            self._set_setpoint(temperature)

    def _zone_command(self, what: str) -> None:
        """Send a zone command in both address forms (see module docstring)."""
        self.send(msg.command(self.who, what, self.zone))
        self.send(msg.command(self.who, what, "#" + self.zone))

    def _set_mode(self, mode: str) -> None:
        if mode == MODE_OFF:
            self._zone_command(WHAT_OFF)
        elif mode == MODE_AUTO:
            self._zone_command(WHAT_AUTO)
        elif mode in (MODE_HEAT, MODE_COOL):
            self.season = mode
            self.mode = mode
            # The gateway acknowledges the season change without the zone
            # following - probably the central unit decides.  The command goes
            # out anyway, then the setpoint is written, which does work and
            # publishes the new state as well.
            self._zone_command(WHAT_HEATING if mode == MODE_HEAT else WHAT_COOLING)
            self._set_setpoint(self.setpoint)
            return
        else:
            _LOGGER.warning("%s: unsupported mode %s", self.name, mode)
            return
        self.mode = mode
        self.publish_state()

    def _set_fan(self, mode: str) -> None:
        """Set the fan speed - without "#" in front of the zone (measured)."""
        value = FAN_VALUES.get(mode)
        if value is None:
            _LOGGER.warning("%s: unbekannte Lüfterstufe %s", self.name, mode)
            return
        self.fan_setting = mode
        self.send(msg.dimension_write(self.who, self.zone, DIM_FAN, value))
        self.publish_state()

    def _set_setpoint(self, temperature: float) -> None:
        temperature = max(self.min_temp, min(self.max_temp, temperature))
        self.setpoint = temperature
        for where in ("#" + self.zone, self.zone):
            self.send(msg.dimension_write(self.who, where, DIM_SETPOINT_PROGRAMMED,
                                          msg.encode_temperature(temperature),
                                          SETPOINT_MODE_MANUAL))
        if self.mode == MODE_OFF:
            self.mode = self.season
        self.publish_state()

    # ---------------------------------------------------------------- OWN in

    def on_own(self, message: msg.OwnMessage) -> None:
        if message.kind == msg.KIND_NORMAL:
            self._decode_mode(message.what)
        elif message.kind == msg.KIND_DIMENSION_RESPONSE and message.values:
            if not self._apply_dimension(message.dimension, message.values):
                return
        else:
            return
        self.publish_state()

    def _apply_dimension(self, dimension: str, values: tuple[str, ...]) -> bool:
        """Take one dimension response; False when it says nothing to us."""
        first = values[0]
        if dimension == DIM_MEASURED_TEMPERATURE:
            value = msg.decode_temperature(first)
            if value is None:
                return False
            self.current_temperature = value
        elif dimension == DIM_SETPOINT_ACTIVE:
            value = msg.decode_temperature(first)
            if value is None:
                return False
            self.setpoint = value
            self._setpoint_from_active = True
        elif dimension == DIM_SETPOINT_PROGRAMMED:
            # Only as long as the zone has not told us the one in force.
            value = msg.decode_temperature(first)
            if value is None or self._setpoint_from_active:
                return False
            self.setpoint = value
        elif dimension == DIM_HUMIDITY:
            if not is_number(first):
                return False
            self.humidity = int(first)
            self._note_extra(EXTRA_HUMIDITY)
        elif dimension == DIM_FAN:
            self.fan_setting = FAN_SETTINGS.get(first, first)
            self._note_extra(EXTRA_FAN)
        elif dimension == DIM_VALVES:
            self._decode_valves(values)
        elif dimension == DIM_ACTUATOR:
            # The actuator reports a stop at once, the valve minutes later -
            # so the action follows the actuator.  A fan coil says 5 for "off".
            self.fan_stage = FAN_STAGES.get(first, self.fan_stage)
            self._set_action(first in ACTIVE_CODES)
        else:
            return False
        return True

    def _decode_mode(self, what: str) -> None:
        """Map the zone WHAT onto a Home Assistant HVAC mode.

        Three digit modes encode ``<source><season>``: the first digit is 1 for
        manual, 2 for the weekly program and 3 for a scenario, the last digit is
        0 for heating and 1 for cooling.  302/303 switch the zone off.
        """
        if what in OFF_CODES:
            self.mode = MODE_OFF
            self.action = ACTION_OFF
            return
        if what in PROTECTION_CODES:          # anti freeze / thermal protection
            self.mode = MODE_OFF
            self.action = ACTION_IDLE
            return
        if what == WHAT_COOLING:
            self.season = MODE_COOL
            self.mode = MODE_COOL if MODE_COOL in self.modes else self.mode
            return
        if what == WHAT_HEATING:
            self.season = MODE_HEAT
            self.mode = MODE_HEAT if MODE_HEAT in self.modes else self.mode
            return
        if len(what) == 3 and what[0] in "123" and what[2] in "01":
            self.season = MODE_HEAT if what[2] == "0" else MODE_COOL
            if what[0] == "2":
                self.mode = MODE_AUTO
            elif self.season in self.modes:
                self.mode = self.season

    def _note_extra(self, name: str) -> None:
        """A zone that answers an extra dimension gets the entity for it."""
        if name in self._extras_seen:
            return
        self._extras_seen.add(name)
        _LOGGER.info("%s liefert %s", self.name,
                     "Luftfeuchtigkeit" if name == EXTRA_HUMIDITY else "eine Lüfterstufe")
        self.ctx.rediscover(self)

    def _decode_valves(self, values: tuple[str, ...]) -> None:
        conditioning = values[0] if values else "0"
        heating = values[1] if len(values) > 1 else "0"
        stage = FAN_STAGES.get(heating) or FAN_STAGES.get(conditioning)
        if stage and stage != "aus":
            self.fan_stage = stage
            self._note_extra(EXTRA_FAN)
        elif stage == "aus":
            self.fan_stage = "aus"
        if heating in ACTIVE_CODES:
            self.action = ACTION_HEATING
        elif conditioning in ACTIVE_CODES:
            self.action = ACTION_COOLING
        elif self.mode == MODE_OFF:
            self.action = ACTION_OFF
        else:
            self.action = ACTION_IDLE

    def _set_action(self, active: bool) -> None:
        if not active:
            self.action = ACTION_OFF if self.mode == MODE_OFF else ACTION_IDLE
        else:
            self.action = ACTION_COOLING if self.season == MODE_COOL else ACTION_HEATING

    def publish_state(self) -> None:
        if self.humidity is not None:
            self.publish(self.humidity_topic, str(self.humidity))
        if self.fan_setting is not None:
            # Only the setting - auto, 1, 2 or 3, the same as on the thermostat.
            # Whether the fan runs right now shows in the action of the zone.
            self.publish(self.fan_topic, self.fan_setting)
        if self.fan_available:
            self.publish(self.fan_mode_topic, self.fan_setting or "auto")
        self.publish(self.mode_topic, self.mode)
        self.publish(self.setpoint_topic, f"{self.setpoint:.1f}")
        self.publish(self.action_topic, self.action)
        if self.current_temperature is not None:
            self.publish(self.temperature_topic, f"{self.current_temperature:.1f}")


class ThermoActuator(Entity):
    """An actuator of the heating - usually the circulation pump.

    On the bus it is ``<zone>#<number>``; the pump hangs on the central unit and
    therefore on ``0#3``.  As with contacts, the number is configured, not the
    bus address.
    """

    who = msg.WHO_THERMO
    component = "binary_sensor"
    topic_group = "pump"
    section = "pumps"
    default_name = "Pumpe"
    model = "Heizungsaktor"

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context, config)
        self.actuator = self.where
        self.zone = str(config.get("zone", CENTRAL_UNIT)).strip() or CENTRAL_UNIT
        self.bus_where = f"{self.zone}#{self.actuator}"
        self.object_id = f"{context.bridge_id}_{self.who}_{slug(self.bus_where)}_pump"
        if self.zone != CENTRAL_UNIT:
            # Actuator 1 of zone 8 and actuator 1 of zone 9 are two devices:
            # the topic needs the zone.  The pump at the central unit keeps
            # its plain number.
            self.key = slug(self.bus_where)
        self.device_class = device_class(
            self.component, config.get("device_class", "running"), config.get("name", ""))
        self.is_on = False
        self._known = False

    def to_config(self) -> dict[str, Any]:
        config = super().to_config()
        config["zone"] = self.zone
        return config

    def match_keys(self) -> tuple[str, ...]:
        return (self.bus_where,)

    def discovery(self) -> list[Discovery]:
        payload = self.base_payload()
        payload.update({"state_topic": self.state_topic, "payload_on": "ON",
                        "payload_off": "OFF", "icon": "mdi:pump"})
        if self.device_class:
            payload["device_class"] = self.device_class
        return [(self.component, self.object_id, payload)]

    def poll_frames(self) -> tuple[str, ...]:
        return (msg.dimension_request(self.who, self.bus_where, DIM_ACTUATOR),)

    def on_own(self, message: msg.OwnMessage) -> None:
        if (message.kind != msg.KIND_DIMENSION_RESPONSE
                or message.dimension != DIM_ACTUATOR or not message.values):
            return
        # The same codes as at the zone: 1 runs, 0 stands, and a fan coil
        # actuator says 5 for off and 6/7/8 for its speeds.
        self.is_on = message.values[0] in ACTIVE_CODES
        self._known = True
        self.publish_state()

    def publish_state(self) -> None:
        # Nothing before the first report: an assumed state would be a guess.
        if self._known:
            self.publish(self.state_topic, "ON" if self.is_on else "OFF")
