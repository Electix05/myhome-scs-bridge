"""The paho-mqtt glue against a real (if tiny) MQTT broker on 127.0.0.1."""

from __future__ import annotations

import asyncio
import socket
import unittest

from myhome_scs_mqtt.app import mqtt as mqtt_module
from myhome_scs_mqtt.app.mqtt import MqttClient, collect_retained
from myhome_scs_mqtt.app.settings import MqttConfig

from .fake_broker import FakeBroker
from .helpers import wait_until

LWT = "myhome/bridge/lwt"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MqttTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.broker = FakeBroker()
        self.port = await self.broker.start()
        self.commands: list[tuple[str, str]] = []
        self.ha_online: list[bool] = []
        self.clients: list[MqttClient] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.stop()
        await self.broker.stop()

    def make(self, port: int | None = None, **config) -> MqttClient:
        settings = MqttConfig(host="127.0.0.1", port=port or self.port, base_topic="myhome",
                              **config)
        client = MqttClient(
            settings, LWT,
            on_command=lambda topic, payload: self.commands.append((topic, payload)),
            on_homeassistant_online=lambda: self.ha_online.append(True),
            client_id="test-%d" % len(self.clients),
        )
        self.clients.append(client)
        return client

    async def started(self, **config) -> MqttClient:
        client = self.make(**config)
        await asyncio.wait_for(client.start(), 10)
        return client


class TestConnection(MqttTestCase):
    async def test_announces_itself_online_and_registers_a_last_will(self):
        client = await self.started(username="bridge", password="12345")
        self.assertTrue(client.connected)
        self.assertTrue(await wait_until(lambda: self.broker.last(LWT) == "online"))
        self.assertEqual(self.broker.retained[LWT], "online")
        connect = self.broker.connects[-1]
        self.assertEqual(connect.username, "bridge")
        self.assertEqual(connect.will, (LWT, "offline", True))

    async def test_the_last_will_fires_when_the_link_dies(self):
        await self.started()
        self.assertTrue(await wait_until(lambda: self.broker.last(LWT) == "online"))
        self.broker.drop_clients()
        self.assertTrue(await wait_until(lambda: LWT in self.broker.retained
                                         and self.broker.count(LWT, "offline") >= 1))

    async def test_offline_is_published_on_stop(self):
        client = await self.started()
        await client.stop()
        self.assertTrue(await wait_until(lambda: self.broker.last(LWT) == "offline"))
        self.assertEqual(self.broker.retained[LWT], "offline")
        await client.stop()                       # twice is harmless

    async def test_stop_without_start_is_harmless(self):
        await self.make().stop()

    async def test_reconnects_and_subscribes_again(self):
        client = await self.started()
        client.subscribe("myhome/light/12/set")
        self.assertTrue(await wait_until(lambda: "myhome/light/12/set" in self.broker.subscriptions))
        connects = len(self.broker.connects)

        self.broker.drop_clients()
        self.assertTrue(await wait_until(lambda: len(self.broker.connects) > connects, 10))
        self.assertTrue(await wait_until(lambda: client.connected))
        self.assertTrue(await wait_until(lambda: "myhome/light/12/set" in self.broker.subscriptions))
        await self.broker.publish("myhome/light/12/set", "ON")
        self.assertTrue(await wait_until(lambda: self.commands))

    async def test_waits_for_a_broker_that_starts_later(self):
        # After a power cut the broker may come up after this add-on.
        await self.broker.stop()
        port = _free_port()
        client = self.make(port=port)
        with self.assertLogs(mqtt_module._LOGGER, "WARNING") as logs:
            starting = asyncio.create_task(client.start(first_warning=0.3, warning_interval=0.3))
            await asyncio.sleep(0.8)
            self.assertFalse(starting.done())
            self.broker = FakeBroker()
            await self.broker.start(port)
            await asyncio.wait_for(starting, 10)
        self.assertTrue(any("Still no connection" in r.getMessage() for r in logs.records))
        self.assertTrue(client.connected)

    async def test_refused_credentials_are_logged_once(self):
        self.broker.refuse_connect = True
        client = self.make(username="bridge", password="wrong")
        with self.assertLogs(mqtt_module._LOGGER, "DEBUG") as logs:
            starting = asyncio.create_task(client.start(first_warning=30))
            self.assertTrue(await wait_until(lambda: len(self.broker.connects) >= 3, 10))
            starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
        errors = [r for r in logs.records if r.levelname == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("refused", errors[0].getMessage())


class TestPublishing(MqttTestCase):
    async def test_publish_reaches_the_broker(self):
        client = await self.started()
        client.publish("myhome/light/12/state", '{"state": "ON"}', retain=True)
        self.assertTrue(await wait_until(
            lambda: self.broker.last("myhome/light/12/state") == '{"state": "ON"}'))
        self.assertIn(("myhome/light/12/state", '{"state": "ON"}', True), self.broker.messages)

    async def test_an_unchanged_state_is_not_sent_again(self):
        client = await self.started()
        for payload in ("closed", "closed", "opening", "opening", "closed"):
            client.publish("myhome/cover/21/state", payload, retain=True)
        self.assertTrue(await wait_until(lambda: self.broker.count("myhome/cover/21/state") == 3))
        await asyncio.sleep(0.2)
        self.assertEqual([p for t, p, _r in self.broker.messages if t == "myhome/cover/21/state"],
                         ["closed", "opening", "closed"])

    async def test_a_state_that_could_not_be_sent_goes_out_later(self):
        # paho drops a QoS 0 publish without a connection instead of queueing
        # it.  Remembering it as sent would keep the broker on the old state.
        client = self.make()
        client.publish("myhome/cover/21/state", "opening", retain=True)
        await asyncio.wait_for(client.start(), 10)
        client.publish("myhome/cover/21/state", "opening", retain=True)
        self.assertTrue(await wait_until(
            lambda: self.broker.last("myhome/cover/21/state") == "opening"))

    async def test_events_are_never_suppressed(self):
        # The payload of an event is constant on purpose - every press counts.
        client = await self.started()
        for _ in range(3):
            client.publish("myhome/keypad/22/button/1/press", "press", retain=False)
        self.assertTrue(await wait_until(
            lambda: self.broker.count("myhome/keypad/22/button/1/press") == 3))

    async def test_home_assistant_restart_sends_everything_again(self):
        client = await self.started()

        def republish():
            self.ha_online.append(True)
            client.publish("myhome/light/12/name", "Kitchen", retain=True)

        client._on_ha_online = republish
        client.publish("myhome/light/12/name", "Kitchen", retain=True)
        self.assertTrue(await wait_until(lambda: self.broker.count("myhome/light/12/name") == 1))

        await self.broker.publish("homeassistant/status", "online")
        self.assertTrue(await wait_until(lambda: self.ha_online))
        self.assertTrue(await wait_until(lambda: self.broker.count("myhome/light/12/name") == 2))
        self.assertEqual(self.commands, [])

    async def test_the_status_topic_follows_the_discovery_prefix(self):
        client = await self.started(discovery_prefix="ha")
        self.assertEqual(client.status_topic, "ha/status")
        self.assertTrue(await wait_until(lambda: "ha/status" in self.broker.subscriptions))
        await self.broker.publish("homeassistant/status", "online")
        await self.broker.publish("ha/status", "offline")
        await asyncio.sleep(0.3)
        self.assertEqual(self.ha_online, [])
        await self.broker.publish("ha/status", "online")
        self.assertTrue(await wait_until(lambda: self.ha_online))


class TestCommands(MqttTestCase):
    async def test_subscriptions_deliver_commands(self):
        client = self.make()
        client.subscribe("myhome/light/12/set")        # before the connection
        await asyncio.wait_for(client.start(), 10)
        client.subscribe("myhome/cover/21/set")        # after it
        self.assertTrue(await wait_until(
            lambda: {"myhome/light/12/set", "myhome/cover/21/set"} <= set(self.broker.subscriptions)))
        await self.broker.publish("myhome/light/12/set", "ON")
        await self.broker.publish("myhome/cover/21/set", "CLOSE")
        self.assertTrue(await wait_until(lambda: len(self.commands) == 2))
        self.assertEqual(self.commands, [("myhome/light/12/set", "ON"),
                                         ("myhome/cover/21/set", "CLOSE")])
        self.assertEqual(client.subscriptions, ["myhome/cover/21/set", "myhome/light/12/set"])

    async def test_a_retained_command_is_not_replayed(self):
        # Somebody published "ON" with retain: the broker hands it out on every
        # subscribe, and obeying it would switch the light after each restart.
        self.broker.retained["myhome/light/12/set"] = "ON"
        client = self.make()
        client.subscribe("myhome/light/12/set")
        with self.assertLogs(mqtt_module._LOGGER, "WARNING"):
            await asyncio.wait_for(client.start(), 10)
            await asyncio.sleep(0.3)
        self.assertEqual(self.commands, [])
        # A live command on the same topic is obeyed.
        await self.broker.publish("myhome/light/12/set", "OFF")
        self.assertTrue(await wait_until(lambda: self.commands == [("myhome/light/12/set", "OFF")]))


class TestInventory(unittest.IsolatedAsyncioTestCase):
    """Taking stock of what the broker still holds."""

    async def asyncSetUp(self) -> None:
        self.broker = FakeBroker()
        self.port = await self.broker.start()
        self.broker.retained.update({
            "myhome/light/12/state": '{"state": "ON"}',
            "myhome/contact/399/state": "ON",
            "homeassistant/binary_sensor/myhome_scs_bridge_25_399/config": "{}",
            "sdomotica/2/04/state": "open",
        })

    async def asyncTearDown(self) -> None:
        await self.broker.stop()

    async def test_finds_retained_topics_under_the_patterns_only(self):
        found = await asyncio.to_thread(
            collect_retained, MqttConfig(host="127.0.0.1", port=self.port),
            ["myhome/#", "homeassistant/+/+/config"], 3.0)
        self.assertEqual(found, [
            "homeassistant/binary_sensor/myhome_scs_bridge_25_399/config",
            "myhome/contact/399/state",
            "myhome/light/12/state",
        ])

    async def test_an_empty_broker_is_an_empty_list(self):
        self.broker.retained.clear()
        found = await asyncio.to_thread(
            collect_retained, MqttConfig(host="127.0.0.1", port=self.port), ["myhome/#"], 3.0)
        self.assertEqual(found, [])

    async def test_no_broker_is_not_an_empty_list(self):
        await self.broker.stop()
        with self.assertLogs(mqtt_module._LOGGER, "WARNING"):
            found = await asyncio.to_thread(
                collect_retained, MqttConfig(host="127.0.0.1", port=_free_port()),
                ["myhome/#"], 1.0)
        self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()
