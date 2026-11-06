"""Does Home Assistant still understand what the bridge announces?

Runs inside the Home Assistant image, against its real code:

    docker run --rm --entrypoint python3 -v "$PWD:/src:ro" -w /src \
        ghcr.io/home-assistant/home-assistant:beta tests/contract/check_discovery.py

The important check is the one about lost keys.  Home Assistant drops unknown
discovery keys without a word (extra=REMOVE_EXTRA): after a rename the entity is
still there, it just lost a feature.  Neither a log line nor "it still starts" would
show that.  Comparing what goes in with what comes out does.
"""

from __future__ import annotations

import importlib
import json
import unittest
from typing import Any

import variants
from myhome_scs_mqtt.app.bridge.discovery import Discovery
from myhome_scs_mqtt.app.devices import climate as climate_device
from myhome_scs_mqtt.app.devices import light as light_device
from myhome_scs_mqtt.app.devices.base import DEVICE_CLASSES
from myhome_scs_mqtt.app.settings import MqttConfig

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.components.cover import CoverDeviceClass
from homeassistant.components.event import EventDeviceClass
from homeassistant.components.light import ColorMode
from homeassistant.components.mqtt.const import SUPPORTED_COMPONENTS
from homeassistant.components.mqtt.discovery import TOPIC_MATCHER
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.const import __version__ as HA_VERSION

# Options that have to stay covered: if a device type stops announcing one of them,
# these checks would look at less without anybody noticing.
MUST_BE_COVERED = {
    "light": {"schema", "brightness", "supported_color_modes", "color_temp_kelvin", "min_kelvin",
              "max_kelvin", "state_topic", "command_topic"},
    "cover": {"position_topic", "set_position_topic", "tilt_command_topic", "tilt_status_topic",
              "tilt_opened_value", "tilt_closed_value", "payload_stop", "state_opening"},
    "climate": {"modes", "mode_state_topic", "temperature_command_topic", "action_topic",
                "fan_modes", "fan_mode_command_topic", "precision", "temp_step"},
    "sensor": {"device_class", "unit_of_measurement", "state_class", "entity_category"},
    "binary_sensor": {"device_class", "payload_on", "payload_off"},
    "event": {"event_types", "device_class"},
    "button": {"payload_press", "entity_category"},
    "scene": {"payload_on", "command_topic"},
    "switch": {"device_class", "state_on", "state_off"},
}

# Values Home Assistant rewrites into its own spelling, with the same meaning.
NORMALISED = {("temperature_unit", "C"): "°C"}


def schema_of(component: str) -> Any:
    module = importlib.import_module(f"homeassistant.components.mqtt.{component}")
    return module.DISCOVERY_SCHEMA


def validate(component: str, payload: dict[str, Any]) -> dict[str, Any]:
    """What Home Assistant makes of a message, as its discovery would."""
    # Through JSON, because that is how it arrives; "platform" is what discovery adds.
    return schema_of(component)({**json.loads(json.dumps(payload)), "platform": "mqtt"})


def lost_keys(sent: dict[str, Any], kept: dict[str, Any], path: str = "") -> list[str]:
    """The keys of ``sent`` that did not survive, including nested ones."""
    lost = []
    for key, value in sent.items():
        if key not in kept:
            lost.append(path + key)
        elif isinstance(value, dict) and isinstance(kept[key], dict):
            lost.extend(lost_keys(value, kept[key], f"{path}{key}."))
        elif isinstance(value, list) and isinstance(kept[key], list):
            for number, (item, kept_item) in enumerate(zip(value, kept[key], strict=False)):
                if isinstance(item, dict) and isinstance(kept_item, dict):
                    lost.extend(lost_keys(item, kept_item, f"{path}{key}[{number}]."))
    return lost


def changed_values(sent: dict[str, Any], kept: dict[str, Any]) -> list[str]:
    """Plain values Home Assistant changed: a value it now reads differently."""
    changed = []
    for key, value in sent.items():
        if key not in kept or isinstance(value, dict):
            continue
        other = kept[key]
        if isinstance(value, list):
            # Lists may come back as sets or tuples; only the content matters here.
            same = (all(isinstance(item, (str, int, float)) for item in value)
                    and sorted(map(str, value)) == sorted(map(str, other)))
            if not same and all(isinstance(item, (str, int, float)) for item in value):
                changed.append(f"{key}: {value!r} became {other!r}")
        elif value != other and NORMALISED.get((key, value)) != other:
            changed.append(f"{key}: {value!r} became {other!r}")
    return changed


class TestDiscoveryMessages(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        print(f"Home Assistant {HA_VERSION}", flush=True)
        cls.messages = variants.announced()

    def test_every_device_type_of_the_bridge_is_exercised(self):
        self.assertEqual(variants.uncovered_sections(), set())

    def test_the_options_that_matter_are_exercised(self):
        seen: dict[str, set[str]] = {}
        for component, _object_id, payload in self.messages:
            seen.setdefault(component, set()).update(payload)
        for component, keys in MUST_BE_COVERED.items():
            with self.subTest(component=component):
                self.assertEqual(keys - seen.get(component, set()), set())
        self.assertEqual(set(seen) - set(MUST_BE_COVERED), set(),
                         "a platform without an entry in MUST_BE_COVERED")

    def test_every_platform_is_still_supported(self):
        for component in {component for component, _object_id, _payload in self.messages}:
            with self.subTest(component=component):
                self.assertIn(component, SUPPORTED_COMPONENTS)

    def test_home_assistant_accepts_every_message(self):
        for component, object_id, payload in self.messages:
            with self.subTest(component=component, object_id=object_id):
                validate(component, payload)

    def test_no_key_gets_lost(self):
        """The silent failure: accepted, and yet without the feature."""
        for component, object_id, payload in self.messages:
            with self.subTest(component=component, object_id=object_id):
                self.assertEqual(lost_keys(payload, validate(component, payload)), [])

    def test_no_value_gets_changed(self):
        for component, object_id, payload in self.messages:
            with self.subTest(component=component, object_id=object_id):
                self.assertEqual(changed_values(payload, validate(component, payload)), [])

    def test_the_discovery_topic_is_still_understood(self):
        discovery = Discovery(publisher=None, registry=None,
                              config=MqttConfig(host="127.0.0.1", instance_id="scs_bridge"))
        for component, object_id, _payload in self.messages:
            topic = discovery.config_topic(component, object_id)
            with self.subTest(topic=topic):
                self.assertTrue(topic.startswith(variants.DISCOVERY_PREFIX + "/"))
                match = TOPIC_MATCHER.match(topic[len(variants.DISCOVERY_PREFIX) + 1:])
                self.assertIsNotNone(match)
                self.assertEqual(match.group("component"), component)
                self.assertEqual(match.group("object_id"), object_id)


class TestVocabulary(unittest.TestCase):
    """Values that stand in the bridge's code and only mean something to Home Assistant."""

    def test_the_device_classes_we_let_through_still_exist(self):
        for component, known in (("binary_sensor", BinarySensorDeviceClass),
                                 ("cover", CoverDeviceClass),
                                 ("switch", SwitchDeviceClass)):
            with self.subTest(component=component):
                self.assertEqual(DEVICE_CLASSES[component] - {item.value for item in known}, set())

    def test_the_colour_modes_still_exist(self):
        modes = {item.value for item in ColorMode}
        self.assertIn(light_device.COLOR_MODE_HS, modes)
        self.assertIn(light_device.COLOR_MODE_TEMP, modes)

    def test_the_climate_modes_and_actions_still_exist(self):
        modes = {item.value for item in HVACMode}
        for mode in (climate_device.MODE_OFF, climate_device.MODE_HEAT, climate_device.MODE_COOL,
                     climate_device.MODE_AUTO):
            with self.subTest(mode=mode):
                self.assertIn(mode, modes)
        actions = {item.value for item in HVACAction}
        for action in (climate_device.ACTION_OFF, climate_device.ACTION_IDLE,
                       climate_device.ACTION_HEATING, climate_device.ACTION_COOLING):
            with self.subTest(action=action):
                self.assertIn(action, actions)

    def test_the_button_event_class_still_exists(self):
        self.assertIn("button", {item.value for item in EventDeviceClass})


if __name__ == "__main__":
    unittest.main(verbosity=1)
