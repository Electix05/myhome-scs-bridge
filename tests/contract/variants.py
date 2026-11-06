"""Every kind of device the bridge can announce: the material of the compatibility checks.

check_discovery.py feeds these through the code of Home Assistant, run_e2e.py runs
them against a real Home Assistant.  Both use the bridge's own classes, so what is
checked is what the bridge really sends - not a copy that could drift away from it.

This folder is no Python package on purpose: the checks need Home Assistant or
Docker, so the normal test run must not pick them up.
"""

from __future__ import annotations

import os
import sys
from typing import Any

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_DIR)

from myhome_scs_mqtt.app.bridge.registry import FACTORIES, Registry  # noqa: E402
from myhome_scs_mqtt.app.devices.base import Context  # noqa: E402
from myhome_scs_mqtt.app.devices.sensors import BridgeStatus  # noqa: E402
from myhome_scs_mqtt.app.own import messages as msg  # noqa: E402

BASE_TOPIC = "scs_bridge"
DISCOVERY_PREFIX = "homeassistant"

DEVICES: dict[str, list[dict[str, Any]]] = {
    "lights": [
        {"where": "11", "name": "Relay"},
        {"where": "12", "name": "Dimmer", "dimmer": True},
        {"where": "13", "name": "Colour", "rgbw": True},
        {"where": "14", "name": "White", "tunable_white": True, "min_kelvin": 2000,
         "max_kelvin": 6200},
        {"where": "15", "name": "Colour and white", "rgbw": True, "tunable_white": True},
    ],
    "switches": [{"where": "16", "name": "Socket", "device_class": "outlet"}],
    "covers": [
        {"where": "21", "name": "Shutter"},
        {"where": "22", "name": "Shutter with position", "travel_time": 20},
        {"where": "23", "name": "Blind with slats", "travel_time": 20, "tilt_time": 2},
    ],
    "climate": [
        {"where": "1", "name": "Zone"},
        {"where": "2", "name": "Fan coil", "luefter": True, "kuehlen": True},
    ],
    "pumps": [{"where": "3", "name": "Pump", "zone": "0"}],
    "energy": [{"where": "51", "name": "Meter", "energy_total": True, "energy_today": True}],
    "binary_sensors": [{"where": "1", "name": "Contact", "schliesser": True}],
    "buttons": [{"where": "22", "name": "Keypad", "buttons": [1, 2]}],
    "motion_sensors": [{"where": "1010", "name": "Presence"}],
    "scenes": [{"where": "01", "name": "Scenes", "scenarios": [1]}],
}

# What an entity only announces once it has heard it on the bus: zone 2 has humidity.
TEACH_FRAMES = ("*#4*2*60*54##",)


def build_registry(gateway_host: str = "192.0.2.10",
                   teach_frames: tuple[str, ...] = TEACH_FRAMES) -> Registry:
    """The registry the bridge would build from DEVICES, after hearing ``teach_frames``."""
    context = Context(
        base_topic=BASE_TOPIC,
        discovery_prefix=DISCOVERY_PREFIX,
        availability_topic=f"{BASE_TOPIC}/bridge/availability",
        publish=lambda *_args, **_kwargs: None,
        send=lambda _frame: None,
        bridge_availability=f"{BASE_TOPIC}/bridge/lwt",
    )
    registry = Registry(context)
    registry.register(BridgeStatus(context, {"where": gateway_host,
                                             "name": "MyHome SCS Bridge"}))
    registry.build(DEVICES, [])
    for frame in teach_frames:
        message = msg.parse(frame)
        for entity in registry.route(message):
            entity.on_own(message)
    return registry


def announced(gateway_host: str = "192.0.2.10",
              teach_frames: tuple[str, ...] = TEACH_FRAMES) -> list[tuple[str, str, dict[str, Any]]]:
    """Every discovery message: (component, object id, payload)."""
    messages = []
    for entity in build_registry(gateway_host, teach_frames).entities:
        messages.extend(entity.discovery())
    return messages


def uncovered_sections() -> set[str]:
    """Device types of the bridge that DEVICES does not exercise."""
    return set(FACTORIES) - set(DEVICES)
