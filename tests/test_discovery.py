"""MQTT discovery: announcements, the device directory and removing leftovers."""

from __future__ import annotations

import asyncio
import json
import unittest

from myhome_scs_mqtt.app.bridge import discovery as discovery_module
from myhome_scs_mqtt.app.bridge.discovery import Discovery
from myhome_scs_mqtt.app.bridge.registry import Registry
from myhome_scs_mqtt.app.devices.sensors import BridgeStatus
from myhome_scs_mqtt.app.mqtt import MqttClient
from myhome_scs_mqtt.app.settings import MqttConfig

from .fake_broker import FakeBroker
from .helpers import Recorder, wait_until

DEVICES = {
    "lights": [{"where": "12", "name": "Kitchen"}],
    "covers": [{"where": "21", "name": "Shutter"}],
    "binary_sensors": [{"where": "7", "name": "Undecided"}],
}


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool, int]] = []

    def publish(self, topic: str, payload: str, retain: bool = False, qos: int = 0) -> None:
        self.published.append((topic, payload, retain, qos))

    def last(self, topic: str) -> str | None:
        for published_topic, payload, _retain, _qos in reversed(self.published):
            if published_topic == topic:
                return payload
        return None


def make(collect=None, config: MqttConfig | None = None):
    registry = Registry(Recorder().context())
    registry.build(DEVICES, [])
    registry.register(BridgeStatus(registry.context, {"where": "192.0.2.10"}))
    publisher = FakePublisher()
    config = config or MqttConfig(host="127.0.0.1", base_topic="myhome")
    discovery = Discovery(publisher, registry, config,
                          collect=collect or (lambda *_args: []))
    return discovery, publisher, registry


class TestAnnouncements(unittest.TestCase):
    def test_every_entity_is_announced_retained(self):
        discovery, publisher, _registry = make()
        with self.assertLogs(discovery_module._LOGGER, "INFO") as logs:
            count = discovery.publish_all()
        # Where to look after changing the prefix or the base topic.
        summary = [r.getMessage() for r in logs.records if "Published" in r.getMessage()]
        self.assertIn("homeassistant/+/scs_bridge/...", summary[0])
        self.assertIn("topics under myhome/", summary[0])
        light = json.loads(publisher.last("homeassistant/light/scs_bridge/scs_bridge_1_12/config"))
        self.assertEqual(light["command_topic"], "myhome/light/12/set")
        configs = [(t, r, q) for t, _p, r, q in publisher.published if t.endswith("/config")]
        self.assertEqual(len(configs), count)
        self.assertTrue(all(retain and qos == 1 for _t, retain, qos in configs))
        self.assertIn("homeassistant/button/scs_bridge/scs_bridge_scan_button/config",
                      [t for t, _r, _q in configs])
        # Every announcement sits under the discovery node of this add-on.
        self.assertTrue(all(t.split("/")[2] == "scs_bridge" for t, _r, _q in configs))

    def test_names_stand_next_to_the_states(self):
        discovery, publisher, _registry = make()
        discovery.publish_all()
        self.assertEqual(publisher.last("myhome/light/12/name"), "Kitchen")
        self.assertEqual(publisher.last("myhome/cover/21/name"), "Shutter")
        self.assertFalse([t for t, *_rest in publisher.published if t.startswith("myhome/sensor/")],
                         "the add-on itself is no device of the installation")

    def test_an_undecided_contact_is_not_announced_and_reported_once(self):
        discovery, publisher, _registry = make()
        with self.assertLogs(discovery_module._LOGGER, "INFO") as logs:
            discovery.publish_all()
            discovery.publish_all()
        self.assertFalse([t for t, *_rest in publisher.published if "_25_7" in t])
        notes = [r for r in logs.records if "nicht zugeordnet" in r.getMessage()]
        self.assertEqual(len(notes), 1)

    def test_the_discovery_prefix_is_used(self):
        config = MqttConfig(host="127.0.0.1", base_topic="haus", discovery_prefix="ha")
        discovery, publisher, _registry = make(config=config)
        discovery.publish_entity(discovery.registry.entities[0])
        self.assertEqual(publisher.published[0][0], "ha/light/scs_bridge/scs_bridge_1_12/config")

    def test_the_directory_lists_every_device(self):
        discovery, publisher, _registry = make()
        discovery.publish_all()
        directory = json.loads(publisher.last("myhome/bridge/devices"))
        self.assertEqual(directory, [
            {"type": "contact", "where": "7", "name": "Undecided", "topic": "myhome/contact/7"},
            {"type": "cover", "where": "21", "name": "Shutter", "topic": "myhome/cover/21"},
            {"type": "light", "where": "12", "name": "Kitchen", "topic": "myhome/light/12"},
        ])


LEFTOVERS = [
    "homeassistant/binary_sensor/scs_bridge/scs_bridge_25_399/config",   # contact removed
    "myhome/contact/399/state",
    "myhome/contact/399/name",
    "myhome/light/12/state",                                             # still exists
    "homeassistant/light/scs_bridge/scs_bridge_1_12/config",
    "homeassistant/sensor/tasmota_kitchen/config",                        # somebody else's
    "homeassistant/light/myhome_scs_bridge_1_77/config",                 # the old add-on's
    "homeassistant/light/other_node/scs_bridge_1_77/config",             # another node
    "myhome/bridge/link",
    "myhome/bridge/last_frame",                                          # retired topic
    "myhome/who1/12/state",                                              # old topic layout
]


class TestStaleTopics(unittest.TestCase):
    def test_only_leftovers_of_this_add_on_are_stale(self):
        discovery, _publisher, _registry = make()
        self.assertEqual(sorted(discovery.stale_topics(LEFTOVERS)), sorted([
            "homeassistant/binary_sensor/scs_bridge/scs_bridge_25_399/config",
            "myhome/contact/399/state",
            "myhome/contact/399/name",
            "myhome/bridge/last_frame",
            "myhome/who1/12/state",
        ]))

    def test_the_old_add_on_keeps_its_announcements(self):
        # Both add-ons may run side by side: "scs_bridge" is part of the old
        # ids ("myhome_scs_bridge_..."), so a substring test would remove them.
        discovery, _publisher, _registry = make()
        old = ["homeassistant/light/myhome_scs_bridge_1_12/config",
               "homeassistant/binary_sensor/myhome_scs_bridge_link/config"]
        self.assertEqual(discovery.stale_topics(old), [])

    def test_a_contact_that_is_no_longer_announced_loses_its_config(self):
        discovery, _publisher, _registry = make()
        stale = discovery.stale_topics(
            ["homeassistant/binary_sensor/scs_bridge/scs_bridge_25_7/config",
             "myhome/contact/7/name"])
        self.assertEqual(stale, ["homeassistant/binary_sensor/scs_bridge/scs_bridge_25_7/config"])


class TestPurge(unittest.IsolatedAsyncioTestCase):
    async def test_purge_publishes_empty_retained_messages(self):
        discovery, publisher, _registry = make(collect=lambda *_args: LEFTOVERS)
        with self.assertLogs(discovery_module._LOGGER, "INFO"):
            removed = await discovery.purge_stale()
        self.assertEqual(removed, 5)
        cleared = [t for t, payload, retain, qos in publisher.published
                   if payload == "" and retain and qos == 1]
        self.assertEqual(len(cleared), 5)
        self.assertNotIn("homeassistant/sensor/tasmota_kitchen/config", cleared)

    async def test_nothing_is_removed_when_the_broker_cannot_be_asked(self):
        discovery, publisher, _registry = make(collect=lambda *_args: None)
        with self.assertLogs(discovery_module._LOGGER, "WARNING"):
            self.assertIsNone(await discovery.purge_stale())
        self.assertEqual(publisher.published, [])

    async def test_against_a_broker_with_foreign_retained_topics(self):
        broker = FakeBroker()
        port = await broker.start()
        config = MqttConfig(host="127.0.0.1", port=port, base_topic="myhome")
        client = MqttClient(config, "myhome/bridge/lwt", on_command=lambda *_a: None,
                            client_id="purge-test")
        try:
            broker.retained.update({
                "homeassistant/binary_sensor/scs_bridge/scs_bridge_25_399/config": "{}",
                "myhome/contact/399/state": "ON",
                "myhome/light/12/state": '{"state": "ON"}',
                "homeassistant/light/scs_bridge/scs_bridge_1_12/config": "{}",
                "homeassistant/sensor/tasmota_kitchen/config": "{}",
                "homeassistant/light/myhome_scs_bridge_1_12/config": "{}",    # old add-on
                "sdomotica/2/04/state": "open",
            })
            await asyncio.wait_for(client.start(), 10)
            registry = Registry(Recorder().context())
            registry.build(DEVICES, [])
            discovery = Discovery(client, registry, config)
            with self.assertLogs(discovery_module._LOGGER, "INFO"):
                self.assertEqual(await discovery.purge_stale(), 2)
            self.assertTrue(await wait_until(
                lambda: "myhome/contact/399/state" not in broker.retained))
            self.assertNotIn("homeassistant/binary_sensor/scs_bridge/scs_bridge_25_399/config",
                             broker.retained)
            for kept in ("myhome/light/12/state",
                         "homeassistant/light/scs_bridge/scs_bridge_1_12/config",
                         "homeassistant/sensor/tasmota_kitchen/config",
                         "homeassistant/light/myhome_scs_bridge_1_12/config",
                         "sdomotica/2/04/state"):
                self.assertIn(kept, broker.retained)
        finally:
            await client.stop()
            await broker.stop()


if __name__ == "__main__":
    unittest.main()
