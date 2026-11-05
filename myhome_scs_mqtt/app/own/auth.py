"""Authentication schemes used by OpenWebNet gateways.

Three cases can happen after the client announces the session type:

* ``*#*1##``          - the gateway accepts us right away (IP filtering only).
* ``*#<nonce>##``     - legacy "OPEN password": a numeric password is folded
  into the nonce with the well known bit-shuffling algorithm.
* ``*98*1##`` /
  ``*98*2##``         - HMAC authentication (SHA-1 / SHA-256), used by
  MyHOMEServer1, F455 and newer F454 firmwares.

Both real-world cases are confirmed on hardware (WISSEN.md "Anmeldung"): an
F461 asked for HMAC SHA-256, an MH201 sent the classic nonce.  The algorithms
below are taken over unchanged from the add-on that was measured there.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Callable

# Constants of the HMAC handshake, fixed by the protocol.
_HMAC_A = "736F70653E"
_HMAC_B = "636F70653E"

HMAC_ALGORITHMS = ("sha1", "sha256")


# --------------------------------------------------------------------------
# Legacy OPEN password
# --------------------------------------------------------------------------

def open_password(password: str, nonce: str) -> str:
    """Answer for the ``*#<nonce>##`` challenge of an OPEN password gateway.

    The classic scheme computes with the password as a number, so only digits
    are allowed.  HMAC gateways accept alphanumeric passwords instead.

    Surrounding whitespace is stripped; signs, underscore digit grouping and
    non-ASCII digits (e.g. full-width forms) are rejected on purpose, even
    though ``int()`` would silently accept some of them - do not "simplify"
    this back to ``int(password)``.

    Raises:
        ValueError: if ``password`` is not a plain ASCII digit string.
    """
    text = password.strip()
    if not (text.isascii() and text.isdigit()):
        raise ValueError("The OPEN password of an SCS gateway must be numeric")
    secret = int(text)

    start = True
    num1 = 0
    num2 = 0
    mask = 0xFFFFFFFF

    for char in nonce:
        if char != "0":
            if start:
                num2 = secret
            start = False
        if char == "1":
            num1 = (num2 & 0xFFFFFF80) >> 7
            num2 = num2 << 25
        elif char == "2":
            num1 = (num2 & 0xFFFFFFF0) >> 4
            num2 = num2 << 28
        elif char == "3":
            num1 = (num2 & 0xFFFFFFF8) >> 3
            num2 = num2 << 29
        elif char == "4":
            num1 = num2 << 1
            num2 = num2 >> 31
        elif char == "5":
            num1 = num2 << 5
            num2 = num2 >> 27
        elif char == "6":
            num1 = num2 << 12
            num2 = num2 >> 20
        elif char == "7":
            num1 = (num2 & 0x0000FF00) | ((num2 & 0x000000FF) << 24) \
                | ((num2 & 0x00FF0000) >> 16)
            num2 = (num2 & 0xFF000000) >> 8
        elif char == "8":
            num1 = ((num2 & 0x0000FFFF) << 16) | (num2 >> 24)
            num2 = (num2 & 0x00FF0000) >> 8
        elif char == "9":
            num1 = ~num2
        else:
            num1 = num2

        num1 &= mask
        num2 &= mask
        if char not in ("0", "9"):
            num1 |= num2
        num2 = num1

    return str(num1 & mask)


# --------------------------------------------------------------------------
# HMAC authentication
# --------------------------------------------------------------------------

def hex_to_digits(value: str) -> str:
    """'a3' -> '1003'  (every hex nibble becomes a two digit decimal group).

    This encoding was the part of the handshake most likely to go wrong;
    measured on an F461, it is right.

    Raises:
        ValueError: if ``value`` contains a character that is not a hex digit.
    """
    return "".join(f"{int(char, 16):02d}" for char in value)


def digits_to_hex(value: str) -> str:
    """Inverse of :func:`hex_to_digits`: '1003' -> 'a3'.

    Raises ValueError for anything that is not a sequence of groups 00..15.
    """
    if len(value) % 2:
        raise ValueError(f"digit string of odd length: {value}")
    if not (value.isascii() and value.isdigit()):
        raise ValueError(f"not a digit string: {value}")
    nibbles = []
    for index in range(0, len(value), 2):
        number = int(value[index:index + 2])
        if number > 15:
            raise ValueError(f"group {number} is not a hex digit")
        nibbles.append(f"{number:x}")
    return "".join(nibbles)


class HmacAuth:
    """State of one HMAC handshake.

    ``sha1`` is selected by ``*98*1##`` and ``sha256`` by ``*98*2##``.
    ``random_bytes`` exists for tests that need a reproducible client nonce.
    """

    def __init__(
        self,
        password: str,
        algorithm: str = "sha256",
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        if algorithm not in HMAC_ALGORITHMS:
            raise ValueError(f"unsupported HMAC algorithm: {algorithm}")
        self.algorithm = algorithm
        self.password = password
        self._random_bytes = random_bytes
        self.ra = ""
        self.rb = ""
        self.kab = ""

    def _hash(self, data: str) -> str:
        return hashlib.new(self.algorithm, data.encode("utf-8")).hexdigest()

    def client_response(self, ra_digits: str) -> str:
        """Build the ``*#<Rb>*<digest>##`` answer to the gateway nonce.

        Raises:
            ValueError: if ``ra_digits`` is not a valid digit encoded nonce.
        """
        self.ra = digits_to_hex(ra_digits)
        self.rb = self._hash(self._random_bytes(32).hex())
        self.kab = self._hash(self.password)
        digest = self._hash(self.ra + self.rb + _HMAC_A + _HMAC_B + self.kab)
        return f"*#{hex_to_digits(self.rb)}*{hex_to_digits(digest)}##"

    def verify_server(self, frame: str) -> bool:
        """Check the ``*#<digest>##`` the gateway sends back to prove itself."""
        if not self.rb:
            return False                       # client_response() never ran
        payload = frame.strip("*#")
        try:
            server_digest = digits_to_hex(payload)
        except ValueError:
            return False
        expected = self._hash(self.ra + self.rb + self.kab)
        return hmac.compare_digest(server_digest, expected)
