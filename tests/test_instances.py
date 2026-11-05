"""Running the add-on more than once - one instance per MyHome installation.

Two instances share one Home Assistant and one broker, so three things must not
collide: the MQTT client id (a broker drops the first client when a second logs
in with the same id), the prefix of every unique_id/object_id, and the discovery
node (every instance removes leftovers under its own node).

The first test class is the important one: with the default ``instance_id``
every id, topic and node has to stay exactly what version 1.22.0 published.
An installation runs on this; a changed unique_id means new entities and a history
that starts over.  BASELINE below was dumped from 1.22.0 before the rewrite.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest

from myhome_scs_mqtt.app import __version__
from myhome_scs_mqtt.app import settings as settings_module
from myhome_scs_mqtt.app.bridge.bridge import Bridge
from myhome_scs_mqtt.app.bridge.discovery import Discovery
from myhome_scs_mqtt.app.bridge.registry import Registry
from myhome_scs_mqtt.app.devices.base import BRIDGE_ID, Context
from myhome_scs_mqtt.app.devices.sensors import BridgeStatus
from myhome_scs_mqtt.app.mqtt import MqttClient, mqtt_client_id
from myhome_scs_mqtt.app.settings import (
    DEFAULT_BASE_TOPIC, DEFAULT_DEVICES_FILE, DEFAULT_INSTANCE_ID, GatewayConfig, MqttConfig,
    ScanConfig, Settings, SettingsError, instance_isolation_warning,
)

from .fake_broker import FakeBroker
from .helpers import wait_until

# One entry per device type that builds an id of its own - the five modules that
# used the hard-wired BRIDGE_ID (base, sensors, events, climate, lightmgmt).
DEVICES = {
    "lights": [{"where": "12", "name": "Kitchen", "dimmer": True}],
    "covers": [{"where": "21", "name": "Shutter"}],
    "climate": [{"where": "1", "name": "Zone"}],
    "energy": [{"where": "51", "name": "Meter", "energy_total": True}],
    "switches": [{"where": "3", "name": "Socket", "who": "9"}],
    "scenes": [{"where": "01", "name": "Scenes", "scenarios": [1]}],
    "binary_sensors": [
        {"where": "1", "name": "Bell", "schliesser": True},
        {"where": "2", "name": "IR", "bus_prefix": "4", "schliesser": True},
    ],
    "buttons": [{"where": "22", "name": "Keypad", "buttons": [1]}],
    "motion_sensors": [{"where": "1010", "name": "Hall"}],
    "pumps": [{"where": "3", "name": "Pump", "zone": "0"}],
}

# Dumped from version 1.22.0.  Everything in here is an id, a node or a topic -
# exactly what Home Assistant ties its entities and their history to.
BASELINE = """
ENTITY BridgeStatus name=MyHome SCS Bridge object_id=scs_bridge root=scs_bridge/sensor/192_0_2_10 file_name=MyHome SCS Bridge
CONFIG homeassistant/binary_sensor/scs_bridge/scs_bridge_link/config uid=scs_bridge_link name=Gateway link dev=scs_bridge via= availability_topic=scs_bridge/bridge/lwt,state_topic=scs_bridge/bridge/link
CONFIG homeassistant/sensor/scs_bridge/scs_bridge_scan/config uid=scs_bridge_scan name=Bus-Scan dev=scs_bridge via= availability_topic=scs_bridge/bridge/lwt,state_topic=scs_bridge/bridge/scan
CONFIG homeassistant/button/scs_bridge/scs_bridge_scan_button/config uid=scs_bridge_scan_button name=Bus scannen dev=scs_bridge via= availability_topic=scs_bridge/bridge/lwt,command_topic=scs_bridge/bridge/scan/set
CONFIG homeassistant/button/scs_bridge/scs_bridge_scan_deep_button/config uid=scs_bridge_scan_deep_button name=Bus gründlich scannen dev=scs_bridge via= availability_topic=scs_bridge/bridge/lwt,command_topic=scs_bridge/bridge/scan/set
CONFIG homeassistant/sensor/scs_bridge/scs_bridge_refused/config uid=scs_bridge_refused name=Abgelehnte Frames dev=scs_bridge via= availability_topic=scs_bridge/bridge/lwt,state_topic=scs_bridge/bridge/refused
CONFIG homeassistant/sensor/scs_bridge/scs_bridge_unanswered/config uid=scs_bridge_unanswered name=Unbeantwortete Frames dev=scs_bridge via= availability_topic=scs_bridge/bridge/lwt,state_topic=scs_bridge/bridge/unanswered
ENTITY Light name=Kitchen object_id=scs_bridge_1_12 root=scs_bridge/light/12 file_name=Kitchen
CONFIG homeassistant/light/scs_bridge/scs_bridge_1_12/config uid=scs_bridge_1_12 name=None dev=scs_bridge_1_12 via=scs_bridge command_topic=scs_bridge/light/12/set,state_topic=scs_bridge/light/12/state,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/light/12/reachable
ENTITY Cover name=Shutter object_id=scs_bridge_2_21 root=scs_bridge/cover/21 file_name=Shutter
CONFIG homeassistant/cover/scs_bridge/scs_bridge_2_21/config uid=scs_bridge_2_21 name=None dev=scs_bridge_2_21 via=scs_bridge command_topic=scs_bridge/cover/21/set,state_topic=scs_bridge/cover/21/state,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/cover/21/reachable
ENTITY Climate name=Zone object_id=scs_bridge_4_1 root=scs_bridge/climate/1 file_name=Zone
CONFIG homeassistant/climate/scs_bridge/scs_bridge_4_1/config uid=scs_bridge_4_1 name=None dev=scs_bridge_4_1 via=scs_bridge action_topic=scs_bridge/climate/1/action,current_temperature_topic=scs_bridge/climate/1/temperature,mode_command_topic=scs_bridge/climate/1/mode/set,mode_state_topic=scs_bridge/climate/1/mode,temperature_command_topic=scs_bridge/climate/1/setpoint/set,temperature_state_topic=scs_bridge/climate/1/setpoint,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/climate/1/reachable
CONFIG homeassistant/sensor/scs_bridge/scs_bridge_4_1_temperature/config uid=scs_bridge_4_1_temperature name=Temperatur dev=scs_bridge_4_1 via=scs_bridge state_topic=scs_bridge/climate/1/temperature,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/climate/1/reachable
ENTITY EnergyMeter name=Meter object_id=scs_bridge_18_51 root=scs_bridge/energy/51 file_name=Meter
CONFIG homeassistant/sensor/scs_bridge/scs_bridge_18_51_power/config uid=scs_bridge_18_51_power name=Power dev=scs_bridge_18_51 via=scs_bridge state_topic=scs_bridge/energy/51/power,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/energy/51/reachable
CONFIG homeassistant/sensor/scs_bridge/scs_bridge_18_51_energy/config uid=scs_bridge_18_51_energy name=Energy dev=scs_bridge_18_51 via=scs_bridge state_topic=scs_bridge/energy/51/energy,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/energy/51/reachable
ENTITY Switch name=Socket object_id=scs_bridge_9_3 root=scs_bridge/switch/3 file_name=Socket
CONFIG homeassistant/switch/scs_bridge/scs_bridge_9_3/config uid=scs_bridge_9_3 name=None dev=scs_bridge_9_3 via=scs_bridge command_topic=scs_bridge/switch/3/set,state_topic=scs_bridge/switch/3/state,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/switch/3/reachable
ENTITY Scenario name=Scenes object_id=scs_bridge_0_01 root=scs_bridge/scene/01 file_name=Scenes
CONFIG homeassistant/scene/scs_bridge/scs_bridge_0_01_scn1/config uid=scs_bridge_0_01_scn1 name=Scenario 1 dev=scs_bridge_0_01 via=scs_bridge command_topic=scs_bridge/scene/01/scenario/1/set,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/scene/01/reachable
ENTITY DryContact name=Bell object_id=scs_bridge_25_1 root=scs_bridge/contact/1 file_name=Bell
CONFIG homeassistant/binary_sensor/scs_bridge/scs_bridge_25_1/config uid=scs_bridge_25_1 name=None dev=scs_bridge_25_1 via=scs_bridge state_topic=scs_bridge/contact/1/state,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/contact/1/reachable
ENTITY DryContact name=IR object_id=scs_bridge_25_4_2 root=scs_bridge/contact/4_2 file_name=IR
CONFIG homeassistant/binary_sensor/scs_bridge/scs_bridge_25_4_2/config uid=scs_bridge_25_4_2 name=None dev=scs_bridge_25_4_2 via=scs_bridge state_topic=scs_bridge/contact/4_2/state,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/contact/4_2/reachable
ENTITY Keypad name=Keypad object_id=scs_bridge_25_22 root=scs_bridge/keypad/22 file_name=Keypad
CONFIG homeassistant/event/scs_bridge/scs_bridge_25_22_btn1/config uid=scs_bridge_25_22_btn1 name=Button 1 dev=scs_bridge_25_22 via=scs_bridge state_topic=scs_bridge/keypad/22/button/1,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/keypad/22/reachable
ENTITY BmseSensor name=Hall object_id=scs_bridge_1_1010_bmse root=scs_bridge/sensor/1010 file_name=Hall
CONFIG homeassistant/sensor/scs_bridge/scs_bridge_1_1010_bmse_lux/config uid=scs_bridge_1_1010_bmse_lux name=Brightness dev=scs_bridge_1_1010_bmse via=scs_bridge state_topic=scs_bridge/sensor/1010/illuminance,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/sensor/1010/reachable
CONFIG homeassistant/binary_sensor/scs_bridge/scs_bridge_1_1010_bmse_motion/config uid=scs_bridge_1_1010_bmse_motion name=Motion dev=scs_bridge_1_1010_bmse via=scs_bridge state_topic=scs_bridge/sensor/1010/motion,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/sensor/1010/reachable
ENTITY ThermoActuator name=Pump object_id=scs_bridge_4_0_3_pump root=scs_bridge/pump/3 file_name=Pump
CONFIG homeassistant/binary_sensor/scs_bridge/scs_bridge_4_0_3_pump/config uid=scs_bridge_4_0_3_pump name=None dev=scs_bridge_4_0_3_pump via=scs_bridge state_topic=scs_bridge/pump/3/state,availability=scs_bridge/bridge/lwt,availability=scs_bridge/bridge/availability,availability=scs_bridge/pump/3/reachable
"""

# The whole discovery message of one entity, again exactly as 1.22.0 wrote it.
# The line-by-line baseline above only covers ids and topics; this one proves
# that nothing else moved either.
BASELINE_LIGHT = {
    "availability": [
        {"payload_available": "online", "payload_not_available": "offline",
         "topic": "scs_bridge/bridge/lwt"},
        {"payload_available": "online", "payload_not_available": "offline",
         "topic": "scs_bridge/bridge/availability"},
        {"payload_available": "online", "payload_not_available": "offline",
         "topic": "scs_bridge/light/12/reachable"},
    ],
    "availability_mode": "all",
    "brightness": True,
    "command_topic": "scs_bridge/light/12/set",
    "device": {"identifiers": ["scs_bridge_1_12"], "manufacturer": "BTicino / Legrand",
               "model": "Dimmer", "name": "Kitchen", "via_device": "scs_bridge"},
    "name": None,
    "optimistic": False,
    "origin": {"name": "MyHome SCS Bridge", "sw_version": __version__},
    "schema": "json",
    "state_topic": "scs_bridge/light/12/state",
    "unique_id": "scs_bridge_1_12",
}


class StubMqtt:
    """Enough of MqttClient for Bridge.__init__ and Discovery."""

    def __init__(self, config=None, availability_topic="", on_command=None,
                 on_homeassistant_online=None):
        self.config = config
        self.published: list[tuple[str, str, bool, int]] = []
        self.connected = False
        self.subscriptions: list[str] = []

    def publish(self, topic: str, payload: str, retain: bool = False, qos: int = 0) -> None:
        self.published.append((topic, payload, retain, qos))

    def subscribe(self, topic: str) -> None:
        self.subscriptions.append(topic)


def context(instance_id: str = DEFAULT_INSTANCE_ID, base_topic: str = DEFAULT_BASE_TOPIC,
            name_prefix: str = "") -> Context:
    return Context(
        base_topic=base_topic,
        discovery_prefix="homeassistant",
        availability_topic=f"{base_topic}/bridge/availability",
        publish=lambda *_a, **_k: None,
        send=lambda _frame: None,
        bridge_id=instance_id,
        name_prefix=name_prefix,
        bridge_availability=f"{base_topic}/bridge/lwt",
    )


def instance(instance_id: str = DEFAULT_INSTANCE_ID, base_topic: str = DEFAULT_BASE_TOPIC,
             name_prefix: str = "", devices: dict | None = None):
    """One instance's registry and discovery, built like the bridge builds them."""
    registry = Registry(context(instance_id, base_topic, name_prefix))
    registry.register(BridgeStatus(registry.context,
                                   {"where": "192.0.2.10", "name": "MyHome SCS Bridge"}))
    registry.build(DEVICES if devices is None else devices, [])
    config = MqttConfig(host="127.0.0.1", base_topic=base_topic, instance_id=instance_id)
    publisher = StubMqtt(config)
    return registry, Discovery(publisher, registry, config), publisher


def topics_of(payload: dict) -> str:
    """Every topic the payload names, as ``key=value`` pairs."""
    pairs = [f"{key}={value}" for key, value in sorted(payload.items())
             if key.endswith("_topic") and isinstance(value, str)]
    pairs += [f"availability={entry['topic']}" for entry in payload.get("availability", ())]
    return ",".join(pairs)


def identity_lines(registry: Registry, discovery: Discovery) -> list[str]:
    """Everything the instance id and the name prefix can move, as text.

    One line per entity and per announced Home Assistant entity.  Only ids,
    names and topics - a new field in a payload (an icon, a temperature range)
    must not make this comparison fail.
    """
    lines = []
    for entity in registry.entities:
        lines.append(f"ENTITY {type(entity).__name__} name={entity.name} "
                     f"object_id={entity.object_id} root={entity.topic_root} "
                     f"file_name={entity.to_config().get('name')}")
        for component, object_id, payload in entity.discovery():
            device = payload.get("device", {})
            lines.append(f"CONFIG {discovery.config_topic(component, object_id)} "
                         f"uid={payload.get('unique_id')} name={payload.get('name')} "
                         f"dev={'|'.join(device.get('identifiers', ()))} "
                         f"via={device.get('via_device', '')} {topics_of(payload)}")
    return lines


class TestTheDefaultChangesNothing(unittest.TestCase):
    """The option exists, but nothing may move as long as it is at its default."""

    def test_every_id_topic_and_node_is_what_1_22_0_published(self):
        registry, discovery, _publisher = instance()
        self.assertEqual(identity_lines(registry, discovery),
                         BASELINE.strip("\n").split("\n"))

    def test_a_whole_discovery_payload_is_unchanged(self):
        registry, _discovery, _publisher = instance()
        light = next(e for e in registry.entities if e.object_id == "scs_bridge_1_12")
        self.assertEqual(light.discovery()[0][2], BASELINE_LIGHT)

    def test_the_discovery_node_and_the_directory_stay_the_same(self):
        _registry, discovery, _publisher = instance()
        self.assertEqual(discovery.node, "scs_bridge")
        self.assertEqual(discovery.directory_topic, "scs_bridge/bridge/devices")

    def test_the_client_ids_stay_the_same(self):
        # Not a word of history hangs on these, but a changed client id would
        # leave a stale "offline" last will behind on every update.
        self.assertEqual(mqtt_client_id(DEFAULT_INSTANCE_ID, hostname="box"), "scs-bridge-box")
        self.assertEqual(mqtt_client_id(DEFAULT_INSTANCE_ID, "inventory", hostname="box", pid=7),
                         "scs-bridge-inventory-box-7")

    def test_the_two_places_that_know_the_default_agree(self):
        # devices/base.py is the default of Context.bridge_id, settings.py the
        # default of the option - the same value, in two layers that must not
        # import each other.
        self.assertEqual(BRIDGE_ID, DEFAULT_INSTANCE_ID)
        self.assertEqual(MqttConfig().instance_id, DEFAULT_INSTANCE_ID)
        self.assertEqual(Settings().instance_id, DEFAULT_INSTANCE_ID)
        self.assertEqual(Settings().name_prefix, "")


class TestTwoInstances(unittest.TestCase):
    def test_nothing_is_shared(self):
        first_registry, first, _p1 = instance()
        second_registry, second, _p2 = instance("haus2", "haus2")

        def ids_and_topics(registry, discovery):
            found = {discovery.directory_topic, discovery.node}
            for entity in registry.entities:
                found.add(entity.object_id)
                found.add(entity.topic_root)
                for component, object_id, payload in entity.discovery():
                    found.add(discovery.config_topic(component, object_id))
                    found.add(payload["unique_id"])
                    found.update(payload.get("device", {}).get("identifiers", ()))
            return found

        mine = ids_and_topics(first_registry, first)
        theirs = ids_and_topics(second_registry, second)
        self.assertEqual(len(mine), len(theirs))
        self.assertEqual(mine & theirs, set())

    def test_neither_instance_sees_the_other_as_a_leftover(self):
        _registry, first, _p1 = instance()
        _registry2, second, _p2 = instance("haus2", "haus2")
        theirs = ["homeassistant/light/haus2/haus2_1_12/config",
                  "homeassistant/binary_sensor/haus2/haus2_25_399/config",
                  "haus2/light/12/state", "haus2/contact/399/state", "haus2/bridge/link"]
        mine = ["homeassistant/light/scs_bridge/scs_bridge_1_12/config",
                "scs_bridge/light/12/state", "scs_bridge/bridge/link"]
        self.assertEqual(first.stale_topics(theirs), [])
        self.assertEqual(second.stale_topics(mine), [])

    def test_the_client_ids_differ(self):
        self.assertNotEqual(mqtt_client_id("scs_bridge", hostname="box"),
                            mqtt_client_id("haus2", hostname="box"))
        self.assertNotEqual(mqtt_client_id("scs_bridge", "inventory", hostname="box", pid=1),
                            mqtt_client_id("haus2", "inventory", hostname="box", pid=1))


class TestTwoInstancesOnOneBroker(unittest.IsolatedAsyncioTestCase):
    """Against the fake broker: the part that bit in practice."""

    async def asyncSetUp(self) -> None:
        self.broker = FakeBroker()
        self.port = await self.broker.start()

    async def asyncTearDown(self) -> None:
        await self.broker.stop()

    async def client(self, instance_id: str, base_topic: str) -> MqttClient:
        config = MqttConfig(host="127.0.0.1", port=self.port, base_topic=base_topic,
                            instance_id=instance_id)
        client = MqttClient(config, f"{base_topic}/bridge/lwt", on_command=lambda *_a: None)
        await asyncio.wait_for(client.start(), 10)
        return client

    async def test_both_instances_log_in_with_a_client_id_of_their_own(self):
        first = await self.client("scs_bridge", "scs_bridge")
        second = await self.client("haus2", "haus2")
        try:
            self.assertTrue(await wait_until(lambda: len(self.broker.connects) >= 2))
            ids = [info.client_id for info in self.broker.connects]
            self.assertEqual(len(set(ids)), len(ids), ids)
            self.assertTrue(any(name.startswith("scs-bridge-") for name in ids), ids)
            self.assertTrue(any(name.startswith("haus2-") for name in ids), ids)
        finally:
            await first.stop()
            await second.stop()

    async def test_purging_one_instance_leaves_the_other_untouched(self):
        client = await self.client("scs_bridge", "scs_bridge")
        theirs = {
            "homeassistant/light/haus2/haus2_1_12/config": "{}",
            "homeassistant/binary_sensor/haus2/haus2_25_399/config": "{}",
            "haus2/light/12/state": "ON",
            "haus2/contact/399/state": "OFF",
            "haus2/bridge/devices": "[]",
        }
        mine_stale = {
            "homeassistant/binary_sensor/scs_bridge/scs_bridge_25_399/config": "{}",
            "scs_bridge/contact/399/state": "ON",
        }
        try:
            self.broker.retained.update(theirs)
            self.broker.retained.update(mine_stale)
            self.broker.retained["scs_bridge/light/12/state"] = "ON"
            registry, _discovery, _publisher = instance()
            discovery = Discovery(client, registry, client.config)
            with self.assertLogs("myhome_scs_mqtt.app.bridge.discovery", "INFO"):
                self.assertEqual(await discovery.purge_stale(), len(mine_stale))
            self.assertTrue(await wait_until(
                lambda: "scs_bridge/contact/399/state" not in self.broker.retained))
            for topic in mine_stale:
                self.assertNotIn(topic, self.broker.retained)
            for topic in theirs:
                self.assertIn(topic, self.broker.retained, "a topic of the other instance is gone")
            self.assertIn("scs_bridge/light/12/state", self.broker.retained)
        finally:
            await client.stop()


class TestTheInstanceIdOption(unittest.TestCase):
    BASE = {"gateway_host": "192.0.2.10", "mqtt_host": "192.0.2.20"}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def load(self, **overrides) -> Settings:
        path = os.path.join(self._tmp.name, "options.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(dict(self.BASE, **overrides), handle)
        return Settings.load(options_file=path, fetch_mqtt_service=lambda: None,
                             data_dir=self._tmp.name)

    def test_a_proper_second_instance_loads(self):
        settings = self.load(instance_id="haus2", mqtt_base_topic="haus2",
                             devices_file="/config/devices_haus2.yaml", name_prefix="Haus 2")
        self.assertEqual(settings.instance_id, "haus2")
        self.assertEqual(settings.mqtt.instance_id, "haus2")
        self.assertEqual(settings.name_prefix, "Haus 2")

    def test_an_empty_id_falls_back_to_the_default(self):
        self.assertEqual(self.load(instance_id="").instance_id, DEFAULT_INSTANCE_ID)
        self.assertEqual(self.load().instance_id, DEFAULT_INSTANCE_ID)

    def test_nonsense_is_refused_with_a_message_that_says_what_is_allowed(self):
        for value in ("Haus 2", "haus 2", "haus-2", "haus.2", "haus/2", "haus#2", "HAUS2",
                      "a" * 33):
            with self.subTest(value=value):
                with self.assertRaises(SettingsError) as caught:
                    self.load(instance_id=value)
                self.assertIn("instance_id", str(caught.exception))

    def test_the_id_of_the_old_add_on_is_refused(self):
        # The old add-on removes every discovery topic containing this text.
        for value in ("myhome_scs_bridge", "myhome_scs_bridge_2"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SettingsError, "myhome_scs_bridge"):
                    self.load(instance_id=value)

    def test_a_half_converted_second_instance_is_warned_about_once(self):
        with self.assertLogs(settings_module._LOGGER, "WARNING") as logs:
            self.load(instance_id="haus2")
        warnings = [record.getMessage() for record in logs.records
                    if "half-converted" in record.getMessage()]
        self.assertEqual(len(warnings), 1)
        self.assertIn("instance_id", warnings[0])

    def test_a_proper_instance_is_not_warned_about(self):
        for options in ({}, {"instance_id": "haus2", "mqtt_base_topic": "haus2",
                             "devices_file": "/config/devices_haus2.yaml"}):
            with self.subTest(options=options):
                settings = self.load(**options)
                self.assertEqual(
                    instance_isolation_warning(settings.instance_id, settings.mqtt.base_topic,
                                               settings.devices_file), "")

    def test_every_mixture_is_spotted(self):
        other = {"instance_id": "haus2", "mqtt_base_topic": "haus2",
                 "devices_file": "/config/devices_haus2.yaml"}
        defaults = {"instance_id": DEFAULT_INSTANCE_ID, "mqtt_base_topic": DEFAULT_BASE_TOPIC,
                    "devices_file": DEFAULT_DEVICES_FILE}
        for key in other:
            for one, rest in ((other, defaults), (defaults, other)):
                values = dict(rest)
                values[key] = one[key]
                with self.subTest(changed=key, values=values):
                    warning = instance_isolation_warning(values["instance_id"],
                                                         values["mqtt_base_topic"],
                                                         values["devices_file"])
                    self.assertIn("half-converted", warning)
                    self.assertIn(key, warning)


class TestNamePrefix(unittest.TestCase):
    def test_an_empty_prefix_leaves_every_name_alone(self):
        registry, _discovery, _publisher = instance(name_prefix="")
        self.assertEqual([entity.name for entity in registry.entities],
                         [entity.base_name for entity in registry.entities])

    def test_the_prefix_stands_in_front_of_every_name(self):
        registry, _discovery, _publisher = instance("haus2", "haus2", name_prefix="Haus 2")
        light = next(e for e in registry.entities if e.where == "12")
        self.assertEqual(light.name, "Haus 2 Kitchen")
        self.assertEqual(light.discovery()[0][2]["device"]["name"], "Haus 2 Kitchen")
        # The add-on's own device too, so two instances are told apart in HA.
        status = registry.entities[0]
        self.assertEqual(status.name, "Haus 2 MyHome SCS Bridge")
        # A name the user did not give: the prefix goes in front of that as well.
        plain, _d, _p = instance(name_prefix="Haus 2", devices={"lights": [{"where": "77"}]})
        self.assertEqual(plain.entities[-1].name, "Haus 2 Light 77")

    def test_the_device_file_keeps_the_name_without_the_prefix(self):
        # to_config() writes the device file; a stored prefix would grow on
        # every save ("Haus 2 Haus 2 Kitchen").
        registry, _discovery, _publisher = instance(name_prefix="Haus 2")
        light = next(e for e in registry.entities if e.where == "12")
        self.assertEqual(light.to_config()["name"], "Kitchen")
        again = light.__class__(registry.context, light.to_config())
        self.assertEqual(again.name, "Haus 2 Kitchen")
        self.assertEqual(again.to_config()["name"], "Kitchen")


class TestTheBridgeWiresItUp(unittest.TestCase):
    def settings(self, **overrides) -> Settings:
        return Settings(
            gateway=GatewayConfig(host="192.0.2.10"),
            mqtt=MqttConfig(host="192.0.2.20", base_topic=overrides.pop("base_topic", "haus2"),
                            instance_id=overrides.pop("instance_id", "haus2")),
            scan=ScanConfig(on_start=False),
            poll_interval=0,
            **overrides,
        )

    def test_the_option_reaches_the_entities_and_the_discovery(self):
        bridge = Bridge(self.settings(name_prefix="Haus 2"), mqtt_factory=StubMqtt,
                        collect=lambda *_args: [])
        self.assertEqual(bridge.context.bridge_id, "haus2")
        self.assertEqual(bridge.context.name_prefix, "Haus 2")
        self.assertEqual(bridge.discovery.node, "haus2")
        self.assertEqual(bridge.status.object_id, "haus2")
        self.assertEqual(bridge.status.name, "Haus 2 MyHome SCS Bridge")
        self.assertEqual(bridge.availability_topic, "haus2/bridge/availability")

    def test_the_default_instance_is_wired_exactly_as_before(self):
        bridge = Bridge(self.settings(instance_id=DEFAULT_INSTANCE_ID,
                                      base_topic=DEFAULT_BASE_TOPIC),
                        mqtt_factory=StubMqtt, collect=lambda *_args: [])
        self.assertEqual(bridge.context.bridge_id, "scs_bridge")
        self.assertEqual(bridge.context.name_prefix, "")
        self.assertEqual(bridge.status.object_id, "scs_bridge")
        self.assertEqual(bridge.availability_topic, "scs_bridge/bridge/availability")

    def test_the_page_says_which_instance_it_belongs_to(self):
        """Two browser tabs look alike otherwise - and a frame typed by hand
        would go to whichever installation that tab happens to be."""
        def label(**overrides) -> str:
            bridge = Bridge(self.settings(**overrides), mqtt_factory=StubMqtt,
                            collect=lambda *_args: [])
            self.assertEqual(bridge.web_status()["instance"], bridge.instance_label())
            return bridge.instance_label()

        self.assertEqual(label(name_prefix="Haus 2"), "Haus 2")
        self.assertEqual(label(), "haus2")
        # A single default instance: the status line stays exactly as it was.
        self.assertEqual(label(instance_id=DEFAULT_INSTANCE_ID,
                               base_topic=DEFAULT_BASE_TOPIC), "")


if __name__ == "__main__":
    unittest.main()
