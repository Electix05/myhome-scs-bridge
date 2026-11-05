"""Parsing and building of OpenWebNet frames.

An OpenWebNet frame always starts with ``*`` and ends with ``##``.  The
following forms exist:

===========================================  =============================
``*#*1##`` / ``*#*0##``                      ACK / NACK
``*WHO*WHAT*WHERE##``                        normal message (command/event)
``*#WHO*WHERE##``                            status request
``*#WHO*WHERE*DIM##``                        dimension request
``*#WHO*WHERE*DIM*V1*V2..##``                dimension response
``*#WHO*WHERE*#DIM*V1*V2..##``               dimension write
===========================================  =============================

``WHAT`` and ``WHERE`` may carry parameters separated by ``#``
(``WHAT#PAR1``, ``WHERE#4#01`` for a light point on a local bus).

Everything in here is pure: no I/O, no state, no logging.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Sequence

ACK: Final = "*#*1##"
NACK: Final = "*#*0##"

# Session types offered by the gateway.
SESSION_COMMAND: Final = "*99*0##"
SESSION_EVENT: Final = "*99*1##"

# The gateway asks for HMAC authentication with one of these.
HMAC_SHA1_CHALLENGE: Final = "*98*1##"
HMAC_SHA256_CHALLENGE: Final = "*98*2##"

# WHO values we care about.
WHO_SCENARIO: Final = "0"
WHO_LIGHTING: Final = "1"
WHO_AUTOMATION: Final = "2"
WHO_THERMO: Final = "4"
WHO_AUX: Final = "9"
WHO_GATEWAY: Final = "13"
WHO_CEN: Final = "15"
WHO_ENERGY: Final = "18"
WHO_CEN_PLUS: Final = "25"

# WHO=13 dimensions of the gateway itself.  Dimension 15 answers "200" on an
# F461 *and* on an MH201, so it is no model number; the firmware in dimension 16
# does tell them apart.
DIM_GATEWAY_TIME: Final = "0"
DIM_GATEWAY_TYPE: Final = "15"
DIM_GATEWAY_FIRMWARE: Final = "16"

KIND_ACK: Final = "ack"
KIND_NACK: Final = "nack"
KIND_NORMAL: Final = "normal"
KIND_STATUS_REQUEST: Final = "status_request"
KIND_DIMENSION_REQUEST: Final = "dim_request"
KIND_DIMENSION_RESPONSE: Final = "dim_response"
KIND_DIMENSION_WRITE: Final = "dim_write"
KIND_OTHER: Final = "other"

# Brightness levels in dimension 1 (and 4 on a DALI lamp that is off) are
# "percent plus 100": 175 means 75 %.
LEVEL_OFFSET: Final = 100

# WHO=1 dimensions of a DALI colour lamp, measured on 19.09.2026 (see
# berichte/17-messung-dali-farbe.md and berichte/18-messung-dali-schreiben.md):
# 12 carries hue, saturation and value, 14 the white temperature.
DIM_COLOR_HSV: Final = "12"
DIM_WHITE_TEMP: Final = "14"

# Hue is a degree (0..359), saturation and value are percent.
HUE_STEPS: Final = 360
PERCENT_MAX: Final = 100

# Dimension 14 is mired times one hundred, so kelvin = 100_000_000 / value.
# Confirmed on the installation (berichte/18-messung-dali-schreiben.md,
# section 3): 2000 K -> 50000, 4000 K -> 25000, 6200 K -> 16129.
WHITE_TEMP_FACTOR: Final = 100_000_000


@dataclass(frozen=True, slots=True)
class OwnMessage:
    """A single decoded OpenWebNet frame.

    Immutable on purpose: one frame is handed to every entity that listens on
    its address, and none of them may change it for the others.
    """

    raw: str
    kind: str
    who: str = ""
    what: str = ""
    what_params: tuple[str, ...] = ()
    where: str = ""
    where_params: tuple[str, ...] = ()
    dimension: str = ""
    values: tuple[str, ...] = ()

    @property
    def is_ack(self) -> bool:
        return self.kind == KIND_ACK

    @property
    def is_nack(self) -> bool:
        return self.kind == KIND_NACK

    @property
    def is_reply(self) -> bool:
        """True for the frames that terminate a command exchange."""
        return self.kind in (KIND_ACK, KIND_NACK)

    @property
    def bus(self) -> str:
        """Suffix of the address, e.g. ``#4#01`` or ``#1``, or an empty string."""
        return "#" + "#".join(self.where_params) if self.where_params else ""

    @property
    def full_where(self) -> str:
        """The address exactly as it travels on the bus: ``8#1``, ``41#4#01``."""
        return self.where + self.bus

    def __str__(self) -> str:
        return self.raw


def _split_params(token: str) -> tuple[str, tuple[str, ...]]:
    """Split ``WHAT#p1#p2`` into ``("WHAT", ("p1", "p2"))``.

    A leading ``#`` belongs to the value itself (group addresses such as
    ``#3`` in lighting, or ``#1`` for a thermo zone command), so it is kept.
    """
    if not token:
        return "", ()
    lead = ""
    body = token
    if body.startswith("#"):
        lead = "#"
        body = body[1:]
    parts = body.split("#")
    return lead + parts[0], tuple(parts[1:])


def parse(raw: str) -> OwnMessage:
    """Decode a single frame. Never raises - unknown shapes become KIND_OTHER."""
    if raw == ACK:
        return OwnMessage(raw=raw, kind=KIND_ACK)
    if raw == NACK:
        return OwnMessage(raw=raw, kind=KIND_NACK)
    if not raw.startswith("*") or not raw.endswith("##") or len(raw) < 4:
        return OwnMessage(raw=raw, kind=KIND_OTHER)

    body = raw[1:-2]

    if body.startswith("#"):
        tokens = body[1:].split("*")
        who = tokens[0]
        if len(tokens) < 2:
            # A bare "*#<value>##": the nonce or a digest of the handshake.
            return OwnMessage(raw=raw, kind=KIND_OTHER, who=who)
        where, where_params = _split_params(tokens[1])

        if len(tokens) == 2:
            return OwnMessage(raw=raw, kind=KIND_STATUS_REQUEST, who=who,
                              where=where, where_params=where_params)

        third = tokens[2]
        if third.startswith("#"):
            return OwnMessage(raw=raw, kind=KIND_DIMENSION_WRITE, who=who,
                              where=where, where_params=where_params,
                              dimension=third[1:], values=tuple(tokens[3:]))
        if len(tokens) == 3:
            return OwnMessage(raw=raw, kind=KIND_DIMENSION_REQUEST, who=who,
                              where=where, where_params=where_params,
                              dimension=third)
        return OwnMessage(raw=raw, kind=KIND_DIMENSION_RESPONSE, who=who,
                          where=where, where_params=where_params,
                          dimension=third, values=tuple(tokens[3:]))

    tokens = body.split("*")
    if len(tokens) < 3:
        return OwnMessage(raw=raw, kind=KIND_OTHER, who=tokens[0])
    what, what_params = _split_params(tokens[1])
    where, where_params = _split_params(tokens[2])
    return OwnMessage(raw=raw, kind=KIND_NORMAL, who=tokens[0], what=what,
                      what_params=what_params, where=where,
                      where_params=where_params)


def split_frames(buffer: str) -> tuple[list[str], str]:
    """Cut a receive buffer into complete frames plus the remaining tail.

    Anything in front of the ``*`` that starts a frame is noise and dropped.
    """
    frames: list[str] = []
    while True:
        end = buffer.find("##")
        if end == -1:
            break
        frame = buffer[: end + 2]
        buffer = buffer[end + 2:]
        start = frame.find("*")
        if start != -1:
            frames.append(frame[start:])
    return frames, buffer


# --------------------------------------------------------------------------
# Frame builders
# --------------------------------------------------------------------------

def command(who: str, what: str, where: str) -> str:
    """``*WHO*WHAT*WHERE##``"""
    return f"*{who}*{what}*{where}##"


def status_request(who: str, where: str) -> str:
    """``*#WHO*WHERE##``"""
    return f"*#{who}*{where}##"


def dimension_request(who: str, where: str, dimension: str) -> str:
    """``*#WHO*WHERE*DIM##``"""
    return f"*#{who}*{where}*{dimension}##"


def dimension_write(who: str, where: str, dimension: str, *values: str) -> str:
    """``*#WHO*WHERE*#DIM*V1*V2##``"""
    joined = "*".join(values)
    return f"*#{who}*{where}*#{dimension}*{joined}##"


# --------------------------------------------------------------------------
# Value helpers
# --------------------------------------------------------------------------

def encode_temperature(celsius: float) -> str:
    """WHO=4 temperature: sign digit plus three digits in 0.1 C.

    21.5 -> '0215', -3.0 -> '1030'.  Values beyond 99.9 are clamped.
    """
    value = int(round(abs(celsius) * 10))
    value = max(0, min(999, value))
    sign = "1" if celsius < 0 else "0"
    return f"{sign}{value:03d}"


def decode_temperature(raw: str) -> float | None:
    """'0243' -> 24.3. Returns None when the value is not a temperature."""
    digits = raw.strip()
    # isascii() first: "²".isdigit() is True, but int() cannot read it.
    if not (digits.isascii() and digits.isdigit()) or len(digits) < 4:
        return None
    sign = -1 if digits[0] == "1" else 1
    return sign * int(digits[1:4]) / 10.0


def encode_level(percent: int) -> str:
    """Brightness for dimension 1: 75 % -> '175'.  Clamped to 0..100 %."""
    return str(LEVEL_OFFSET + max(0, min(100, int(percent))))


def decode_level(raw: str) -> int | None:
    """Dimension 1/4 value -> percent: '175' -> 75.  None outside 100..200."""
    text = raw.strip()
    if not (text.isascii() and text.isdigit()):
        return None
    value = int(text)
    if not LEVEL_OFFSET <= value <= LEVEL_OFFSET + 100:
        return None
    return value - LEVEL_OFFSET


_INFINITY: Final = float("inf")


def as_number(value: object) -> float | None:
    """A finite number from anything the outside world hands in, else None.

    Home Assistant sends JSON, the device file is written by hand - both can
    carry text, ``null`` or nonsense where a number belongs.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number != number or number in (_INFINITY, -_INFINITY):   # NaN, inf
        return None
    return number


def encode_hsv(hue: object, saturation: object,
               value: object = PERCENT_MAX) -> tuple[str, str, str] | None:
    """Colour for dimension 12: ``*#1*<where>*#12*H*S*V##``.

    Hue is a degree (360 is 0 again), saturation and value are percent.  The
    value stayed at 100 in every recording we have - the brightness of the lamp
    travels in WHAT or dimension 1, not in here
    (berichte/17-messung-dali-farbe.md).  Returns None for anything that is no
    number; a colour is never worth an exception.
    """
    hue_number = as_number(hue)
    saturation_number = as_number(saturation)
    value_number = as_number(value)
    if hue_number is None or saturation_number is None or value_number is None:
        return None
    degrees = round(hue_number) % HUE_STEPS
    percent = max(0, min(PERCENT_MAX, round(saturation_number)))
    level = max(0, min(PERCENT_MAX, round(value_number)))
    return str(degrees), str(percent), str(level)


def decode_hsv(values: Sequence[str]) -> tuple[int, int, int] | None:
    """Dimension 12 values -> ``(hue, saturation, value)``.

    ``('243', '96', '100')`` -> ``(243, 96, 100)``.  None when the frame does
    not carry three numbers in range.
    """
    if len(values) < 3:
        return None
    numbers: list[int] = []
    for raw in values[:3]:
        text = raw.strip()
        # isascii() first: "²".isdigit() is True, but int() cannot read it.
        if not (text.isascii() and text.isdigit()):
            return None
        numbers.append(int(text))
    hue, saturation, value = numbers
    if hue >= HUE_STEPS or saturation > PERCENT_MAX or value > PERCENT_MAX:
        return None
    return hue, saturation, value


def encode_white(kelvin: object) -> str | None:
    """Colour temperature for dimension 14: mired times one hundred.

    2000 K -> '50000', 6200 K -> '16129' (berichte/18-messung-dali-schreiben.md).
    None for zero or nonsense.
    """
    number = as_number(kelvin)
    if number is None or number < 1:
        return None
    value = round(WHITE_TEMP_FACTOR / number)
    if value < 1:
        return None
    return str(value)


def decode_white(raw: str) -> int | None:
    """Dimension 14 value -> colour temperature in kelvin: '37040' -> 2700."""
    text = raw.strip()
    if not (text.isascii() and text.isdigit()):
        return None
    value = int(text)
    if value < 1:
        return None
    return round(WHITE_TEMP_FACTOR / value)
