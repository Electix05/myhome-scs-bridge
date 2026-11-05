"""WHO=1 - lighting: relays and dimmers.

Measured (WISSEN.md "DALI" and "Was das Gateway nicht kann"):

* A wall switch makes a dimmer report a rounded step, WHAT 2..10.  At a real
  40 % the bus says WHAT 6 - taken at face value Home Assistant would show 60 %.
* The exact level comes from dimension 1 as percent plus 100.  A DALI lamp that
  is off answers in dimension 4 instead, with the same encoding.
* Ordinary dimmers do not answer dimension 1 at all, so a dimmer at rest looks
  exactly like a relay until it reports a level for the first time.

Measured on 19.09.2026 (berichte/17-messung-dali-farbe.md and
berichte/18-messung-dali-schreiben.md), for DALI lamps that do colour:

* Dimension 12 is the colour: hue in degrees, saturation in percent, and a
  third value that was 100 in every recording.  It is *not* the brightness -
  that one stays with WHAT and dimension 1.
* Dimension 14 is the white temperature as mired times one hundred.  The lamp
  that was measured covers 2000 K to 6200 K and clamps by itself.
* A lamp that is off ignores a colour command and does not switch on from it.
  "On with a colour" therefore has to switch on first.
* Both dimensions are answered when asked, so the state survives a restart.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..own import messages as msg
from .base import Context, Discovery, Entity, device_class, is_number

_LOGGER = logging.getLogger(__name__)

WHAT_OFF = "0"
WHAT_ON = "1"
# Classic dimmers know ten steps: WHAT 2 = 20 % .. WHAT 10 = 100 %.
WHAT_STEP_MIN = 2
WHAT_STEP_MAX = 10
# A gateway forwards the command translation of a key press (MH201 only);
# it carries no state.
WHAT_COMMAND_TRANSLATION = "1000"

DIM_LEVEL = "1"
DIM_LEVEL_WHEN_OFF = "4"
DEFAULT_DIMMER_SPEED = "255"

# After a switch command the ballast fades for a moment; only once it has
# arrived does the exact level mean anything.
LEVEL_SETTLE = 2.0

# Home Assistant colour modes; it shows exactly one of them at a time.
COLOR_MODE_HS = "hs"
COLOR_MODE_TEMP = "color_temp"

# The value in dimension 12 stays at 100: in every recording it did, and the
# brightness of the lamp travels in WHAT or dimension 1.
COLOR_VALUE = 100

# Range of the lamp that was measured (berichte/18-messung-dali-schreiben.md).
DEFAULT_MIN_KELVIN = 2000
DEFAULT_MAX_KELVIN = 6200
# Outside this a number in the device file is a typo, not a lamp.
MIN_SENSIBLE_KELVIN = 1000
MAX_SENSIBLE_KELVIN = 20000

MODEL_DIMMER = "Dimmer"
MODEL_COLOR = "DALI colour light"
MODEL_WHITE = "DALI tunable white"


def pct_to_brightness(percent: int) -> int:
    """0..100 % -> Home Assistant brightness 0..255."""
    return max(0, min(255, round(percent * 255 / 100)))


def brightness_to_pct(brightness: int) -> int:
    """Home Assistant brightness -> 1..100 % (0 would switch the lamp off)."""
    return max(1, min(100, round(brightness * 100 / 255)))


def kelvin_option(value: object, default: int, name: str) -> int:
    """A colour temperature from the device file, or the default."""
    if value is None or value == "":
        return default
    number = msg.as_number(value)
    if number is None or not MIN_SENSIBLE_KELVIN <= number <= MAX_SENSIBLE_KELVIN:
        _LOGGER.warning("%s: %r is no colour temperature in kelvin - using %d K",
                        name, value, default)
        return default
    return round(number)


class Light(Entity):
    who = msg.WHO_LIGHTING
    component = "light"
    topic_group = "light"
    section = "lights"
    default_name = "Light"
    model = "Lighting actuator"

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context, config)
        # Colour and white temperature come from a DALI lamp, and such a lamp
        # dims in percent steps - so both imply the dimmer.
        self.rgbw = bool(config.get("rgbw"))
        self.tunable_white = bool(config.get("tunable_white"))
        self.dimmer = bool(config.get("dimmer") or config.get("dimmer100")
                           or self.rgbw or self.tunable_white)
        # One checkbox, and it means percent steps.  Older device files that
        # still carry the separate flag are read all the same.
        self.dimmer100 = self.dimmer
        self.dimmer_speed = str(config.get("dimmer_speed", DEFAULT_DIMMER_SPEED))
        self.min_kelvin = kelvin_option(config.get("min_kelvin"),
                                        DEFAULT_MIN_KELVIN, self.name)
        self.max_kelvin = kelvin_option(config.get("max_kelvin"),
                                        DEFAULT_MAX_KELVIN, self.name)
        if self.min_kelvin >= self.max_kelvin:
            _LOGGER.warning("%s: %d K to %d K is no colour temperature range - "
                            "using %d K to %d K", self.name, self.min_kelvin,
                            self.max_kelvin, DEFAULT_MIN_KELVIN, DEFAULT_MAX_KELVIN)
            self.min_kelvin, self.max_kelvin = DEFAULT_MIN_KELVIN, DEFAULT_MAX_KELVIN
        self.is_on = False
        self.brightness = 255
        # What the lamp last reported: hue and saturation, or the white
        # temperature in kelvin.  Home Assistant shows one mode at a time.
        self.hue: int | None = None
        self.saturation: int | None = None
        self.color_temp: int | None = None
        self.color_mode = ""
        self._asked_level = False
        self._level_pending = False
        self._update_model()

    def to_config(self) -> dict[str, Any]:
        config = super().to_config()
        config.pop("dimmer100", None)
        if self.dimmer:
            config["dimmer"] = True
        if self.rgbw:
            config["rgbw"] = True
        if self.tunable_white:
            config["tunable_white"] = True
        # Only what the entry already carries: a lamp that says nothing about
        # its range keeps saying nothing, the defaults do the rest.
        for key, kelvin in (("min_kelvin", self.min_kelvin),
                            ("max_kelvin", self.max_kelvin)):
            if config.get(key) is not None:
                config[key] = kelvin
        return config

    def _update_model(self) -> None:
        """What Home Assistant shows as the model of this lamp."""
        if self.rgbw:
            self.model = MODEL_COLOR
        elif self.tunable_white:
            self.model = MODEL_WHITE
        elif self.dimmer:
            self.model = MODEL_DIMMER

    # ------------------------------------------------------------- discovery

    def color_modes(self) -> list[str]:
        """The Home Assistant colour modes this lamp offers, if any."""
        modes = []
        if self.rgbw:
            modes.append(COLOR_MODE_HS)
        if self.tunable_white:
            modes.append(COLOR_MODE_TEMP)
        return modes

    def discovery(self) -> list[Discovery]:
        payload = self.base_payload()
        payload.update(
            {
                "schema": "json",
                "state_topic": self.state_topic,
                "command_topic": self.command_topic,
                "brightness": self.dimmer,
                "optimistic": False,
            }
        )
        modes = self.color_modes()
        if modes:
            payload["supported_color_modes"] = modes
        if self.tunable_white:
            # Kelvin instead of mireds, and the range of this lamp - all three
            # are documented options of the MQTT light integration
            # (color_temp_kelvin, min_kelvin, max_kelvin), see
            # https://www.home-assistant.io/integrations/light.mqtt/
            payload["color_temp_kelvin"] = True
            payload["min_kelvin"] = self.min_kelvin
            payload["max_kelvin"] = self.max_kelvin
        return [(self.component, self.object_id, payload)]

    def subscriptions(self) -> tuple[str, ...]:
        return (self.command_topic,)

    def probe_frames(self) -> tuple[str, ...]:
        """Colour questions whose refusal is an answer, not an overloaded bus.

        Asked until the lamp has answered once - afterwards it reports every
        change by itself, so a lamp at rest costs nothing.
        """
        frames = []
        if self.rgbw and self.hue is None:
            frames.append(msg.dimension_request(self.who, self.where, msg.DIM_COLOR_HSV))
        if self.tunable_white and self.color_temp is None:
            frames.append(msg.dimension_request(self.who, self.where, msg.DIM_WHITE_TEMP))
        return tuple(frames)

    def poll_frames(self) -> tuple[str, ...]:
        frames = [msg.status_request(self.who, self.where)]
        # Asked once at start-up: a percent dimmer answers with its exact
        # level, a relay stays silent and is then never asked again.
        if self.dimmer100 or not self._asked_level:
            frames.append(msg.dimension_request(self.who, self.where, DIM_LEVEL))
        self._asked_level = True
        # The colour questions ride along; the sender marks them as probes.
        frames.extend(self.probe_frames())
        return tuple(frames)

    def followup_frames(self) -> tuple[str, ...]:
        if self.dimmer:
            return ()
        return (msg.dimension_request(self.who, self.where, DIM_LEVEL),)

    # --------------------------------------------------------------- MQTT in

    def on_command(self, topic: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = {"state": payload.strip().upper()}
        state = str(data.get("state", "ON")).upper()

        if state == "OFF":
            self.send(msg.command(self.who, WHAT_OFF, self.where))
            return

        colors = [frame for frame in (self._color_frame(data), self._white_frame(data))
                  if frame]
        frames: list[str] = []
        brightness = data.get("brightness")
        if brightness is not None and self.dimmer:
            level = self._level_frame(brightness)
            if level is not None:
                frames.append(level)
            elif not colors:
                # A brightness we cannot read is dropped (and logged).  On its
                # own there is nothing left to send; next to a colour the
                # colour still goes out instead of the lamp doing nothing.
                return
        if colors and not frames and not self.is_on:
            # Measured: a lamp that is off ignores a colour command and does
            # not switch on from it.  A brightness frame switches it on by
            # itself, a colour frame needs the switch command in front of it.
            # The gap comes from the bus pacing (about 0.2 s), not from a
            # sleep that would hold up everything else.
            frames.append(msg.command(self.who, WHAT_ON, self.where))
        frames.extend(colors)
        if not frames:
            frames.append(msg.command(self.who, WHAT_ON, self.where))
        for frame in frames:
            self.send(frame)

    def _level_frame(self, brightness: object) -> str | None:
        """The frame that sets the brightness, or None when it is no number."""
        try:
            percent = brightness_to_pct(int(brightness))    # type: ignore[arg-type]
        except (TypeError, ValueError):
            _LOGGER.warning("%s: not a brightness: %r", self.name, brightness)
            return None
        if self.dimmer100:
            return msg.dimension_write(self.who, self.where, DIM_LEVEL,
                                       msg.encode_level(percent), self.dimmer_speed)
        what = max(WHAT_STEP_MIN, min(WHAT_STEP_MAX, round(percent / 10)))
        return msg.command(self.who, str(what), self.where)

    def _color_frame(self, data: dict[str, Any]) -> str:
        """``*#1*<where>*#12*H*S*V##`` from a Home Assistant colour."""
        color = data.get("color")
        if not self.rgbw or not isinstance(color, dict):
            return ""
        if "h" not in color or "s" not in color:
            return ""           # rgb or xy - we never offered those
        values = msg.encode_hsv(color.get("h"), color.get("s"), COLOR_VALUE)
        if values is None:
            _LOGGER.warning("%s: not a colour: %r", self.name, color)
            return ""
        return msg.dimension_write(self.who, self.where, msg.DIM_COLOR_HSV, *values)

    def _white_frame(self, data: dict[str, Any]) -> str:
        """``*#1*<where>*#14*<mired x 100>##`` from a colour temperature."""
        requested = data.get("color_temp")
        if not self.tunable_white or requested is None:
            return ""
        kelvin = msg.as_number(requested)
        if kelvin is None:
            _LOGGER.warning("%s: not a colour temperature: %r", self.name, requested)
            return ""
        # The lamp cuts its own range anyway (measured); staying inside the
        # configured one keeps pointless frames off the bus.
        kelvin = max(self.min_kelvin, min(self.max_kelvin, round(kelvin)))
        value = msg.encode_white(kelvin)
        if value is None:
            return ""
        return msg.dimension_write(self.who, self.where, msg.DIM_WHITE_TEMP, value)

    # ---------------------------------------------------------------- OWN in

    def on_own(self, message: msg.OwnMessage) -> None:
        if message.kind == msg.KIND_NORMAL:
            what = message.what
            if what == WHAT_OFF:
                self.is_on = False
            elif what == WHAT_ON:
                self.is_on = True
            elif is_number(what) and WHAT_STEP_MIN <= int(what) <= WHAT_STEP_MAX:
                self.is_on = True
                self.brightness = pct_to_brightness(int(what) * 10)
                self._upgrade_to_dimmer()
                if self.dimmer100:
                    # The step is rounded to ten percent, so it is only good
                    # enough until the exact level arrives.
                    self._request_level()
            else:
                return          # e.g. WHAT_COMMAND_TRANSLATION: no state in it
        elif (message.kind == msg.KIND_DIMENSION_RESPONSE
              and message.dimension in (DIM_LEVEL, DIM_LEVEL_WHEN_OFF)
              and message.values):
            percent = msg.decode_level(message.values[0])
            if percent is None:
                return
            self.is_on = percent > 0
            if percent:
                self.brightness = pct_to_brightness(percent)
            self._upgrade_to_dimmer(hundred=True)
        elif (message.kind == msg.KIND_DIMENSION_RESPONSE
              and message.dimension == msg.DIM_COLOR_HSV):
            color = msg.decode_hsv(message.values)
            if color is None:
                return
            # The third value is not the brightness - that one keeps coming
            # from WHAT and dimension 1.
            self.hue, self.saturation = color[0], color[1]
            self.color_mode = COLOR_MODE_HS
            self._upgrade_to_color(rgbw=True)
        elif (message.kind == msg.KIND_DIMENSION_RESPONSE
              and message.dimension == msg.DIM_WHITE_TEMP
              and message.values):
            kelvin = msg.decode_white(message.values[0])
            if kelvin is None:
                return
            # The lamp reports what it really stands at, even beyond its own
            # range; Home Assistant only accepts what we announced.
            self.color_temp = max(self.min_kelvin, min(self.max_kelvin, kelvin))
            self.color_mode = COLOR_MODE_TEMP
            self._upgrade_to_color(tunable_white=True)
        else:
            return
        self.publish_state()

    def _request_level(self) -> None:
        """Ask for the exact brightness once the fade has finished."""
        if self._level_pending:
            return
        self._level_pending = True

        def ask() -> None:
            self._level_pending = False
            self.send(msg.dimension_request(self.who, self.where, DIM_LEVEL))

        if self.ctx.call_later(LEVEL_SETTLE, ask) is None:
            self._level_pending = False     # no scheduler: do not block for ever

    def _upgrade_to_dimmer(self, hundred: bool = False) -> None:
        """A relay never reports a brightness - so this one is a dimmer.

        Dimmers that sit at 0 % or 100 % are indistinguishable from a relay on
        the bus, which is why they can only be recognised once they report a
        level for the first time.
        """
        if self.dimmer and (self.dimmer100 or not hundred):
            return
        self.dimmer = True
        self.dimmer100 = self.dimmer100 or hundred
        self._update_model()
        _LOGGER.info("%s reports a brightness - exposing it as a dimmer", self.name)
        self.ctx.rediscover(self)

    def _upgrade_to_color(self, rgbw: bool = False, tunable_white: bool = False) -> None:
        """A lamp that reports a colour can do colour - so show it that way.

        Same idea as :meth:`_upgrade_to_dimmer`: until dimension 12 or 14
        arrives for the first time, a colour lamp looks like a plain dimmer.
        """
        if not ((rgbw and not self.rgbw) or (tunable_white and not self.tunable_white)):
            return
        self.rgbw = self.rgbw or rgbw
        self.tunable_white = self.tunable_white or tunable_white
        # A DALI lamp that does colour dims in percent steps as well.
        self.dimmer = self.dimmer100 = True
        self._update_model()
        _LOGGER.info("%s reports %s - exposing it that way", self.name,
                     "a colour" if rgbw else "a white temperature")
        self.ctx.rediscover(self)

    def publish_state(self) -> None:
        state: dict[str, Any] = {"state": "ON" if self.is_on else "OFF"}
        if self.dimmer:
            state["brightness"] = self.brightness
        # Home Assistant takes exactly one colour mode at a time, together with
        # the value that belongs to it.
        if self.rgbw and self.color_mode == COLOR_MODE_HS and self.hue is not None:
            state["color_mode"] = COLOR_MODE_HS
            state["color"] = {"h": self.hue, "s": self.saturation}
        elif (self.tunable_white and self.color_mode == COLOR_MODE_TEMP
              and self.color_temp is not None):
            state["color_mode"] = COLOR_MODE_TEMP
            state["color_temp"] = self.color_temp
        self.publish(self.state_topic, json.dumps(state))


class Switch(Entity):
    """Same protocol as a relay light, shown as a switch in Home Assistant."""

    who = msg.WHO_LIGHTING
    component = "switch"
    topic_group = "switch"
    section = "switches"
    default_name = "Switch"
    model = "Actuator"

    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context, config)
        self.is_on = False
        self.device_class = device_class(
            self.component, config.get("device_class"), config.get("name", "")
        )

    def to_config(self) -> dict[str, Any]:
        config = super().to_config()
        config["who"] = int(self.who) if is_number(self.who) else self.who
        return config

    def discovery(self) -> list[Discovery]:
        payload = self.base_payload()
        payload.update(
            {
                "state_topic": self.state_topic,
                "command_topic": self.command_topic,
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
            }
        )
        if self.device_class:
            payload["device_class"] = self.device_class
        return [(self.component, self.object_id, payload)]

    def subscriptions(self) -> tuple[str, ...]:
        return (self.command_topic,)

    def poll_frames(self) -> tuple[str, ...]:
        return (msg.status_request(self.who, self.where),)

    def on_command(self, topic: str, payload: str) -> None:
        what = WHAT_ON if payload.strip().upper() in ("ON", "1", "TRUE") else WHAT_OFF
        self.send(msg.command(self.who, what, self.where))

    def on_own(self, message: msg.OwnMessage) -> None:
        if message.kind != msg.KIND_NORMAL:
            return
        if message.what == WHAT_OFF:
            self.is_on = False
        elif message.what == WHAT_ON or (
                is_number(message.what)
                and WHAT_STEP_MIN <= int(message.what) <= WHAT_STEP_MAX):
            self.is_on = True
        else:
            return
        self.publish_state()

    def publish_state(self) -> None:
        self.publish(self.state_topic, "ON" if self.is_on else "OFF")
