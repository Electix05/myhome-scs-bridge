"""End to end: fake SCS gateway in, MQTT messages out - and back.

Everything runs on 127.0.0.1: the FakeGateway speaks OpenWebNet, FakeMqtt
records what the bridge publishes (FakeBroker for the shutdown test).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import tempfile
import unittest
from unittest import mock

import yaml

from myhome_scs_mqtt.app import __main__ as app_main
from myhome_scs_mqtt.app.bridge import background as background_module
from myhome_scs_mqtt.app.bridge import bridge as bridge_module
from myhome_scs_mqtt.app.bridge.bridge import Bridge, ScanTiming
from myhome_scs_mqtt.app.own.gateway import BusPacing
from myhome_scs_mqtt.app.settings import (
    BrakeConfig, GatewayConfig, MqttConfig, ScanConfig, Settings, SettingsError,
)

from .fake_broker import FakeBroker
from .fake_gateway import FakeGateway
from .helpers import wait_until

DEVICES = """
lights:
  - where: "12"
    name: Kitchen
    dimmer: true
covers:
  - where: "21"
    name: Living room shutter
    position_control: software
climate:
  - where: "2"
    name: Bathroom
energy:
  - where: "51"
    name: House meter
buttons:
  - where: "22"
    name: Hallway keypad
    buttons: [1, 2]
"""

FAST_SCAN = ScanTiming(probe_pace=0.01, phase_pause=0.01, settle=0.3)
FAST_GATEWAY = {"reconnect_min": 0.05, "reconnect_max": 0.2}
# A free port on 127.0.0.1, open to the test client (in the add-on: ingress only).
LOCAL_WEB = {"port": 0, "host": "127.0.0.1", "allowed_peers": None}


class FakeMqtt:
    """Stands in for MqttClient: records everything, injects nothing by itself."""

    def __init__(self, config, availability_topic, on_command, on_homeassistant_online=None):
        self.config = config
        self.availability_topic = availability_topic
        self.on_command = on_command
        self.on_homeassistant_online = on_homeassistant_online
        self.published: list[tuple[str, str, bool]] = []
        self.subscribed: set[str] = set()
        self.connected = True
        self.stopped = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True

    def publish(self, topic, payload, retain=False, qos=0) -> None:
        self.published.append((topic, payload, retain))

    def subscribe(self, topic) -> None:
        self.subscribed.add(topic)

    @property
    def subscriptions(self) -> list[str]:
        return sorted(self.subscribed)

    def announce_online(self) -> None:
        self.publish(self.availability_topic, "online", retain=True, qos=1)

    def last(self, topic):
        for published_topic, payload, _retain in reversed(self.published):
            if published_topic == topic:
                return payload
        return None

    def discovery_for(self, object_id):
        """What a subscriber would see - discovery is retained, the last one counts."""
        for topic, payload, _retain in reversed(self.published):
            if topic.endswith(f"/{object_id}/config"):
                return json.loads(payload) if payload else None
        return None


class BridgeTestCase(unittest.IsolatedAsyncioTestCase):
    devices = DEVICES
    scan = ScanConfig(on_start=False)

    def gateway_options(self) -> dict:
        """What the bridge hands to the Gateway - a fresh set for every test."""
        return dict(FAST_GATEWAY)

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.devices_file = os.path.join(self.dir, "devices.yaml")
        if self.devices is not None:
            with open(self.devices_file, "w", encoding="utf-8") as handle:
                handle.write(self.devices)
        self.fake = FakeGateway(password="12345", mode="open")
        port = await self.fake.start()
        self.collect_calls = []
        self.leftovers: list[str] = []
        self.settings = Settings(
            gateway=GatewayConfig(host="127.0.0.1", port=port, password="12345"),
            mqtt=MqttConfig(host="broker", base_topic="myhome"),
            scan=self.scan,
            devices_file=self.devices_file,
            discovered_file=os.path.join(self.dir, "discovered.json"),
            auto_discover=True,
            poll_interval=0,
        )
        self._save_delay = mock.patch.object(bridge_module, "SAVE_DELAY", 0.05)
        # Undone even when the set-up fails below (then asyncTearDown is skipped).
        self.addCleanup(self._save_delay.stop)
        self._save_delay.start()
        self.bridge = Bridge(self.settings, mqtt_factory=FakeMqtt, collect=self.collect,
                             scan_timing=FAST_SCAN, gateway_options=self.gateway_options(),
                             web_options=LOCAL_WEB)
        self.mqtt: FakeMqtt = self.bridge.mqtt
        self.runner = asyncio.create_task(self.bridge.run())
        self.assertTrue(await wait_until(lambda: self.bridge.gateway.connected),
                        "gateway link did not come up")

    async def asyncTearDown(self) -> None:
        self.runner.cancel()
        await asyncio.gather(self.runner, return_exceptions=True)
        await self.bridge.stop()
        await self.fake.stop()
        self._tmp.cleanup()

    def collect(self, config, patterns, seconds):
        self.collect_calls.append(patterns)
        return list(self.leftovers)

    def discovered_file(self) -> list:
        with open(self.settings.discovered_file, encoding="utf-8") as handle:
            return json.load(handle)


class TestAnnouncements(BridgeTestCase):
    async def test_configured_devices_are_announced(self):
        light = self.mqtt.discovery_for("scs_bridge_1_12")
        self.assertEqual(light["schema"], "json")
        self.assertTrue(light["brightness"])
        self.assertEqual(light["command_topic"], "myhome/light/12/set")
        self.assertEqual(light["availability_mode"], "all")
        self.assertEqual([a["topic"] for a in light["availability"]],
                         ["myhome/bridge/lwt", "myhome/bridge/availability",
                          "myhome/light/12/reachable"])
        # The address is announced as reachable right away; without a payload
        # Home Assistant would leave the lamp unavailable for good.
        self.assertEqual(self.mqtt.last("myhome/light/12/reachable"), "online")
        climate = self.mqtt.discovery_for("scs_bridge_4_2")
        self.assertEqual(climate["current_temperature_topic"], "myhome/climate/2/temperature")
        self.assertEqual(self.mqtt.discovery_for("scs_bridge_18_51_power")["device_class"],
                         "power")
        self.assertEqual(self.mqtt.discovery_for("scs_bridge_25_22_btn1")["state_topic"],
                         "myhome/keypad/22/button/1")
        link = self.mqtt.discovery_for("scs_bridge_link")
        self.assertEqual(link["availability_topic"], "myhome/bridge/lwt")
        # Announced under a discovery node of its own, apart from the old add-on.
        self.assertIn(("homeassistant/light/scs_bridge/scs_bridge_1_12/config"),
                      [topic for topic, _payload, _retain in self.mqtt.published])

    async def test_command_topics_are_subscribed(self):
        for topic in ("myhome/light/12/set", "myhome/cover/21/position/set",
                      "myhome/climate/2/setpoint/set", "myhome/bridge/scan/set"):
            self.assertIn(topic, self.mqtt.subscribed)

    async def test_topics_read_like_a_directory(self):
        self.assertEqual(self.mqtt.last("myhome/light/12/name"), "Kitchen")
        self.assertEqual(self.mqtt.last("myhome/climate/2/name"), "Bathroom")
        directory = json.loads(self.mqtt.last("myhome/bridge/devices"))
        by_topic = {item["topic"]: item for item in directory}
        self.assertEqual(by_topic["myhome/light/12"],
                         {"type": "light", "where": "12", "name": "Kitchen",
                          "topic": "myhome/light/12"})
        self.assertEqual(len(directory), 5)

    async def test_leftovers_are_removed_at_start(self):
        self.assertEqual(self.collect_calls,
                         [["myhome/#", "homeassistant/+/scs_bridge/+/config"]])

    async def test_home_assistant_restart_announces_everything_again(self):
        self.mqtt.published.clear()
        self.mqtt.on_homeassistant_online()
        self.assertIsNotNone(self.mqtt.discovery_for("scs_bridge_1_12"))
        self.assertEqual(self.mqtt.last("myhome/bridge/lwt"), "online")
        self.assertEqual(self.mqtt.last("myhome/bridge/availability"), "online")
        self.assertIsNotNone(self.mqtt.last("myhome/climate/2/mode"))


class TestBusAndCommands(BridgeTestCase):
    async def test_bus_events_become_mqtt_states(self):
        await self.fake.emit("*1*1*12##")
        self.assertTrue(await wait_until(
            lambda: self.mqtt.last("myhome/light/12/state") == '{"state": "ON", "brightness": 255}'))
        await self.fake.emit("*#4*2*0*0198##")
        self.assertTrue(await wait_until(lambda: self.mqtt.last("myhome/climate/2/temperature") == "19.8"))
        await self.fake.emit("*#18*51*113*0000850##")
        self.assertTrue(await wait_until(lambda: self.mqtt.last("myhome/energy/51/power") == "850"))
        await self.fake.emit("*25*21#2*22##")
        self.assertTrue(await wait_until(
            lambda: self.mqtt.last("myhome/keypad/22/button/2") == '{"event_type": "press"}'))
        self.assertEqual(self.mqtt.last("myhome/keypad/last"), "22/2/press")
        self.assertEqual(self.bridge.status.last_frame, "*25*21#2*22##")

    async def test_mqtt_commands_reach_the_bus(self):
        self.mqtt.on_command("myhome/light/12/set", '{"state":"ON","brightness":128}')
        self.mqtt.on_command("myhome/cover/21/set", "CLOSE")
        self.mqtt.on_command("myhome/climate/2/setpoint/set", "21.5")
        self.mqtt.on_command("myhome/climate/2/mode/set", "off")
        for frame in ("*#1*12*#1*150*255##", "*2*2*21##", "*#4*#2*#14*0215*3##",
                      "*#4*2*#14*0215*3##", "*4*303*2##", "*4*303*#2##"):
            self.assertTrue(await wait_until(lambda f=frame: f in self.fake.received), frame)

    async def test_unknown_command_topics_are_ignored(self):
        self.mqtt.on_command("myhome/light/99/set", "ON")
        await asyncio.sleep(0.2)
        self.assertNotIn("*1*1*99##", self.fake.received)

    async def test_the_refresh_runs_when_the_link_comes_up(self):
        for frame in ("*#1*0##", "*#1*12##", "*#2*21##", "*#4*2*0##", "*#4*2*12##",
                      "*#18*51*113##"):
            self.assertTrue(await wait_until(lambda f=frame: f in self.fake.received, 15),
                            f"{frame} missing in {self.fake.received}")

    async def test_the_mh201_answers_on_the_event_session(self):
        self.fake.answers_on_event_session = True
        self.fake.responses["*#4*2*0##"] = ["*#4*2*0*0269##"]
        self.mqtt.on_command("myhome/climate/2/setpoint/set", "20")      # any traffic
        self.bridge.background.queue_frame("*#4*2*0##")
        self.assertTrue(await wait_until(lambda: self.mqtt.last("myhome/climate/2/temperature") == "26.9"))

    async def test_a_command_overtakes_the_refresh(self):
        for number in range(20):
            self.bridge.background.queue_frame(f"*#1*{30 + number}##", background=True)
        self.mqtt.on_command("myhome/cover/21/set", "OPEN")
        self.assertTrue(await wait_until(lambda: "*2*1*21##" in self.fake.received, 5))
        position = self.fake.received.index("*2*1*21##")
        before = [f for f in self.fake.received[:position] if f.startswith("*#1*3") or f.startswith("*#1*4")]
        self.assertLess(len(before), 3, self.fake.received)

    async def test_a_refused_probe_does_not_delay_a_command(self):
        self.fake.nack_frames.update({"*#4*2*11##", "*#4*2*60##"})
        self.bridge.background.queue_frame("*#4*2*11##", probe=True, background=True)
        self.bridge.background.queue_frame("*#4*2*60##", probe=True, background=True)
        self.assertTrue(await wait_until(lambda: "*#4*2*60##" in self.fake.received))
        loop = asyncio.get_running_loop()
        started = loop.time()
        self.mqtt.on_command("myhome/light/12/set", "OFF")
        self.assertTrue(await wait_until(lambda: "*1*0*12##" in self.fake.received, 3))
        self.assertLess(loop.time() - started, 1.0)

    async def test_refusals_reach_the_diagnostic_sensors(self):
        self.fake.nack_frames.add("*1*1*99##")
        with self.assertLogs("myhome_scs_mqtt.app.own.session", "WARNING"):
            self.bridge.background.queue_frame("*1*1*99##")
            self.assertTrue(await wait_until(lambda: self.bridge.gateway.refused == 1))
        self.bridge.status.publish_state()
        self.assertEqual(self.mqtt.last("myhome/bridge/refused"), "1")
        self.assertEqual(self.mqtt.last("myhome/bridge/unanswered"), "0")
        self.assertEqual(self.bridge.background.bus_load()["refused"], 1)

    async def test_a_broken_entity_does_not_stop_the_others(self):
        light = self.bridge.registry.by_topic("myhome/light/12/set")

        def explode(_message):
            raise RuntimeError("bug")

        light.on_own = explode
        with self.assertLogs(bridge_module._LOGGER, "ERROR"):
            await self.fake.emit("*1*1*12##")
            await asyncio.sleep(0.1)
        await self.fake.emit("*#4*2*0*0205##")
        self.assertTrue(await wait_until(lambda: self.mqtt.last("myhome/climate/2/temperature") == "20.5"))

    async def test_availability_follows_the_gateway_link(self):
        self.assertEqual(self.mqtt.last("myhome/bridge/availability"), "online")
        self.assertEqual(self.mqtt.last("myhome/bridge/link"), "connected")
        states = []
        self.bridge.status.set_connected = lambda state, real=self.bridge.status.set_connected: (
            states.append(state), real(state))
        self.fake.drop_all()
        self.assertTrue(await wait_until(lambda: states[:2] == [False, True], 5), states)
        self.assertIn(("myhome/bridge/availability", "offline", True), self.mqtt.published)
        self.assertEqual(self.mqtt.last("myhome/bridge/availability"), "online")


class TestSilencedAddresses(BridgeTestCase):
    """An address that is refused again and again is left alone.

    Measured on 24.09.2026 (berichte/22-messung-buslast.md): a single entry
    whose address does not exist slowed the whole installation down.
    """

    async def test_a_refused_address_is_silenced_and_named_on_the_page(self):
        light = self.bridge.registry.by_topic("myhome/light/12/set")
        self.fake.nack_frames.add("*1*1*12##")
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "WARNING"):
            for number in range(1, 4):
                self.mqtt.on_command("myhome/light/12/set", "ON")
                self.assertTrue(await wait_until(
                    lambda n=number: self.fake.received.count("*1*1*12##") == n, 5))
            self.assertTrue(await wait_until(lambda: light.silenced, 2))
        self.assertEqual(self.mqtt.last("myhome/light/12/reachable"), "offline")
        self.assertEqual(self.bridge.web_status()["silenced"], ["lights 12"])

    async def test_a_frame_from_the_bus_brings_it_back(self):
        light = self.bridge.registry.by_topic("myhome/light/12/set")
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "WARNING"):
            for _ in range(3):
                light.note_refusal()
        self.assertTrue(light.silenced)
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "INFO"):
            await self.fake.emit("*1*1*12##")
            self.assertTrue(await wait_until(lambda: not light.silenced, 5))
        self.assertEqual(self.mqtt.last("myhome/light/12/reachable"), "online")
        self.assertEqual(self.bridge.web_status()["silenced"], [])

    async def test_a_command_still_goes_out_to_a_silenced_address(self):
        # The user may try again at any time - only the refresh keeps quiet.
        light = self.bridge.registry.by_topic("myhome/light/12/set")
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "WARNING"):
            for _ in range(3):
                light.note_refusal()
        self.mqtt.on_command("myhome/light/12/set", "OFF")
        self.assertTrue(await wait_until(lambda: "*1*0*12##" in self.fake.received, 5))


class TestAutoDiscovery(BridgeTestCase):
    async def test_unknown_addresses_become_entities(self):
        await self.fake.emit("*2*2*33##")
        self.assertTrue(await wait_until(lambda: self.mqtt.discovery_for("scs_bridge_2_33")))
        self.assertEqual(self.mqtt.discovery_for("scs_bridge_2_33")["command_topic"],
                         "myhome/cover/33/set")
        self.assertIn("myhome/cover/33/set", self.mqtt.subscribed)
        self.assertEqual(self.mqtt.last("myhome/cover/33/state"), "closing")
        # ... and it can be controlled right away.
        self.mqtt.on_command("myhome/cover/33/set", "OPEN")
        self.assertTrue(await wait_until(lambda: "*2*1*33##" in self.fake.received))
        # An area address is no device.
        await self.fake.emit("*1*1*3##")
        await asyncio.sleep(0.1)
        self.assertIsNone(self.mqtt.discovery_for("scs_bridge_1_3"))
        # Remembered for the next start, and listed in the directory.
        self.assertTrue(await wait_until(lambda: os.path.exists(self.settings.discovered_file)))
        self.assertEqual(self.discovered_file(), [{"section": "covers", "where": "33"}])
        self.assertTrue(await wait_until(
            lambda: "myhome/cover/33" in self.mqtt.last("myhome/bridge/devices")))

    async def test_a_light_that_dims_is_remembered_as_dimmer(self):
        await self.fake.emit("*1*1*44##")
        self.assertTrue(await wait_until(lambda: self.mqtt.discovery_for("scs_bridge_1_44")))
        self.assertFalse(self.mqtt.discovery_for("scs_bridge_1_44")["brightness"])
        await self.fake.emit("*1*5*44##")
        self.assertTrue(await wait_until(
            lambda: self.mqtt.discovery_for("scs_bridge_1_44")["brightness"]))
        self.assertEqual(self.mqtt.last("myhome/light/44/state"), '{"state": "ON", "brightness": 128}')
        self.assertTrue(await wait_until(
            lambda: os.path.exists(self.settings.discovered_file)
            and self.discovered_file()[0].get("dimmer") is True))

    async def test_pending_discoveries_are_written_on_stop(self):
        with mock.patch.object(bridge_module, "SAVE_DELAY", 3600):
            await self.fake.emit("*2*2*34##")
            self.assertTrue(await wait_until(lambda: self.bridge.registry.known("covers", "34")))
            await self.bridge.stop()
        self.assertEqual(self.discovered_file(), [{"section": "covers", "where": "34"}])


class TestShutdownOfTheBridge(BridgeTestCase):
    async def test_stop_goes_offline_and_closes_the_sessions(self):
        await self.bridge.stop()
        self.assertEqual(self.mqtt.last("myhome/bridge/availability"), "offline")
        self.assertTrue(self.mqtt.stopped)
        self.assertTrue(await wait_until(lambda: self.fake.open_connections == 0))
        await self.bridge.stop()                  # twice is harmless

    async def test_nothing_new_starts_while_stopping(self):
        # MQTT and the bus still deliver while the gateway closes: a scan
        # button, a restart or a new device must not leave a task behind.
        real_stop = self.bridge.gateway.stop

        async def stop_with_late_requests() -> None:
            self.mqtt.on_command("myhome/bridge/scan/set", "PRESS")
            self.bridge.schedule_restart()
            self.bridge._schedule_save()
            await real_stop()

        self.bridge.gateway.stop = stop_with_late_requests
        await self.bridge.stop()
        self.assertFalse(self.bridge.scanning)
        self.assertIsNone(self.bridge._restart_task)
        leftovers = [task.get_name() for task in asyncio.all_tasks()
                     if task.get_name() in ("bridge-scan", "bridge-restart", "bridge-save")]
        self.assertEqual(leftovers, [])


class TestStartOrder(BridgeTestCase):
    """Review 11 suggested starting the gateway before MQTT - kept on purpose."""

    async def asyncSetUp(self) -> None:
        self.broker_up = asyncio.Event()
        original_start = FakeMqtt.start
        broker_up = self.broker_up

        async def slow_broker(fake):
            await broker_up.wait()                # the broker is still booting
            await original_start(fake)

        with mock.patch.object(FakeMqtt, "start", slow_broker):
            task = asyncio.create_task(super().asyncSetUp())
            await asyncio.sleep(0.5)
            self.logins_while_waiting = self.fake.logins if hasattr(self, "fake") else 0
            broker_up.set()
            await task

    async def test_the_link_waits_for_mqtt_and_ends_online(self):
        # With the gateway first, the "offline" that run() publishes after the
        # broker connected would overwrite the "online" of the link.
        self.assertEqual(self.logins_while_waiting, 0)
        self.assertEqual(self.mqtt.last("myhome/bridge/availability"), "online")
        published = [payload for topic, payload, _retain in self.mqtt.published
                     if topic == "myhome/bridge/availability"]
        self.assertEqual(published[-2:], ["offline", "online"])


class TestBrokenDeviceFile(BridgeTestCase):
    devices = "lights: [\n  - where: '12'\n"
    scan = ScanConfig(on_start=True)

    async def asyncSetUp(self) -> None:
        with self.assertLogs("myhome_scs_mqtt.app", "WARNING") as self.logs:
            await super().asyncSetUp()

    async def test_nothing_is_removed_and_nothing_is_scanned(self):
        # A typo in the file must not delete every entity in Home Assistant.
        self.assertEqual(self.collect_calls, [])
        await asyncio.sleep(0.3)
        self.assertFalse(self.bridge.scanning)
        self.assertTrue(any("device file" in r.getMessage() for r in self.logs.records))


class TestMistypedDeviceFile(BridgeTestCase):
    """The file is valid YAML, and still two devices in it cannot be read."""

    devices = """
ligths:
  - where: "12"
    name: Kitchen
covers:
  - where: "21"
    name: Shutter
    travel_time: 25s
  - where: "22"
    name: Second shutter
"""
    scan = ScanConfig(on_start=True)

    async def asyncSetUp(self) -> None:
        with self.assertLogs("myhome_scs_mqtt.app", "WARNING") as self.logs:
            await super().asyncSetUp()

    async def test_nothing_is_removed_while_a_device_could_not_be_read(self):
        # Kitchen and Shutter exist, the bridge just cannot read them.  Taking
        # their announcements away would delete them in Home Assistant.
        self.assertEqual(self.collect_calls, [])
        self.assertFalse(self.bridge.inventory_complete)
        self.assertTrue(any("nothing is removed" in r.getMessage() for r in self.logs.records))
        status = self.bridge.web_status()
        problems = status["problems"]
        self.assertEqual(len(problems), 2)
        self.assertIn("ligths", problems[0])
        self.assertIn("covers 21", problems[1])
        # The page does not know these two - a save from it would delete them.
        self.assertTrue(status["saving_blocked"])
        # What could be read works as usual.
        self.assertIsNotNone(self.mqtt.discovery_for("scs_bridge_2_22"))
        # Nothing waits for a confirmation either: there was no clean-up at all.
        self.assertIsNone(self.bridge.web_status()["purge_held"])
        self.assertIsNone(await self.bridge.web_confirm_purge())
        self.assertEqual(self.collect_calls, [])


class TestHeldCleanUp(BridgeTestCase):
    """The broker knows eight devices, this start knows none of them."""

    devices = None                                # devices_file points nowhere
    scan = ScanConfig(on_start=True)

    def collect(self, config, patterns, seconds):
        self.collect_calls.append(patterns)
        topics = []
        for where in range(50, 58):
            topics += [f"homeassistant/light/scs_bridge/scs_bridge_1_{where}/config",
                       f"myhome/light/{where}/state", f"myhome/light/{where}/name"]
        return topics

    async def asyncSetUp(self) -> None:
        with self.assertLogs("myhome_scs_mqtt.app", "WARNING") as self.logs:
            await super().asyncSetUp()

    def removed(self) -> list[str]:
        return [topic for topic, payload, _retain in self.mqtt.published if payload == ""]

    async def test_nothing_is_removed_and_nothing_is_scanned_until_confirmed(self):
        self.assertEqual(self.removed(), [])
        held = self.bridge.web_status()["purge_held"]
        self.assertEqual((held["devices"], held["entities"]), (8, 8))
        self.assertEqual(held["examples"][0], "light/50")
        self.assertTrue(any("8 devices" in r.getMessage() for r in self.logs.records))
        # An empty list is no fresh installation here - no scan on top of it.
        await asyncio.sleep(0.3)
        self.assertFalse(self.bridge.scanning)
        self.assertEqual(self.bridge.status.scan_state, "bereit")

    async def test_the_confirmation_removes_them(self):
        with self.assertLogs("myhome_scs_mqtt.app", "WARNING"):
            self.assertEqual(await self.bridge.web_confirm_purge(), 24)
        self.assertEqual(len(self.removed()), 24)
        self.assertIsNone(self.bridge.web_status()["purge_held"])

    async def test_a_device_that_came_back_meanwhile_stays(self):
        await self.fake.emit("*1*1*50##")                     # lamp 50 is there after all
        self.assertTrue(await wait_until(lambda: self.bridge.registry.known("lights", "50")))
        with self.assertLogs("myhome_scs_mqtt.app", "WARNING"):
            self.assertEqual(await self.bridge.web_confirm_purge(), 21)
        self.assertNotIn("homeassistant/light/scs_bridge/scs_bridge_1_50/config", self.removed())


class TestRevision(BridgeTestCase):
    """The number the page compares its copy of the device list with."""

    async def test_it_moves_with_every_device_found_and_every_thing_learned(self):
        first = self.bridge.web_status()["revision"]
        await self.fake.emit("*1*1*44##")                     # a new lamp
        self.assertTrue(await wait_until(lambda: self.bridge.registry.known("lights", "44")))
        second = self.bridge.web_status()["revision"]
        self.assertNotEqual(second, first)
        await self.fake.emit("*1*5*44##")                     # ... that turns out to dim
        self.assertTrue(await wait_until(
            lambda: self.mqtt.discovery_for("scs_bridge_1_44")["brightness"]))
        self.assertNotEqual(self.bridge.web_status()["revision"], second)
        # A frame of a known device changes nothing in the list.
        third = self.bridge.web_status()["revision"]
        await self.fake.emit("*1*0*12##")
        await asyncio.sleep(0.1)
        self.assertEqual(self.bridge.web_status()["revision"], third)

    async def test_two_starts_do_not_share_a_number(self):
        other = Bridge(self.settings, mqtt_factory=FakeMqtt, collect=self.collect,
                       web_options=LOCAL_WEB)
        self.assertNotEqual(other.revision, self.bridge.revision)


class TestScan(BridgeTestCase):
    devices = None                                # a fresh installation
    scan = ScanConfig(on_start=True, zones=3, meters=1, contacts=3)

    def gateway_options(self) -> dict:
        # At the real five frames a second every single test in here would wait
        # ten seconds for its scan.  What the pacing does to a scan is measured
        # in test_gateway.py and test_background.py; here the scan logic counts.
        return {**FAST_GATEWAY,
                "pacing": BusPacing(rate=100.0, size=100.0, reserve=10.0)}

    async def asyncSetUp(self) -> None:
        # What the simulated installation answers - set before the link is up.
        responses = {
            "*#1*0##": ["*1*1*12##", "*1*5*13##"],
            "*#2*0##": ["*2*0*21##"],
            "*#2*21*10##": ["*#2*21*10*10*15*001*0##"],   # real F461 format
            "*#4*2*0##": ["*#4*2*0*0215##"],
            "*#18*51*113##": ["*#18*51*113*0000420##"],
            "*#25*33##": ["*25*32*33##"],
        }
        original = FakeGateway.__init__

        def with_responses(fake, *args, **kwargs):
            original(fake, *args, **kwargs)
            fake.responses = dict(responses)

        with mock.patch.object(FakeGateway, "__init__", with_responses):
            await super().asyncSetUp()

    async def wait_for_scan(self) -> bool:
        return await wait_until(lambda: self.bridge.status.scan_state.startswith("fertig"), 30)

    async def test_an_empty_installation_scans_itself(self):
        self.assertTrue(await self.wait_for_scan(), self.bridge.status.scan_state)
        found = {(e.section, e.where) for e in self.bridge.registry.device_entities()}
        self.assertEqual(found, {("lights", "12"), ("lights", "13"), ("covers", "21"),
                                 ("climate", "2"), ("energy", "51"), ("binary_sensors", "3")})
        self.assertIn("fertig: 6 neu", self.bridge.status.scan_state)

    async def test_the_scan_says_how_far_it_has_come(self):
        seen: list[tuple[int, int]] = []
        while not self.bridge.status.scan_state.startswith("fertig"):
            progress = self.bridge.web_status()["scan_progress"]
            if progress is not None:
                seen.append((progress["done"], progress["total"]))
            await asyncio.sleep(0.01)
        self.assertTrue(seen, "no progress was reported while the scan ran")
        # It only ever moves forward and never claims more than there is.
        self.assertEqual([done for done, _total in seen], sorted(done for done, _total in seen))
        self.assertTrue(all(0 <= done <= total for done, total in seen))
        self.assertGreater(seen[-1][0], 0)
        # Nothing to show once it is over.
        self.assertIsNone(self.bridge.web_status()["scan_progress"])

    async def test_the_scan_never_switches_anything(self):
        self.assertTrue(await self.wait_for_scan())
        self.assertEqual([f for f in self.fake.received if not f.startswith("*#")], [])

    async def test_a_reporting_shutter_gets_position_control(self):
        self.assertTrue(await self.wait_for_scan())
        cover = self.mqtt.discovery_for("scs_bridge_2_21")
        self.assertEqual(cover["position_topic"], "myhome/cover/21/position")
        with open(os.path.join(self.dir, "scan_result.yaml"), encoding="utf-8") as handle:
            report = yaml.safe_load(handle)
        shutter = report["covers"][0]
        self.assertEqual(shutter["where"], "21")
        self.assertTrue(shutter["position_feedback"])
        self.assertNotIn("advanced", shutter)
        self.assertEqual([item["where"] for item in report["lights"]], ["12", "13"])
        self.assertTrue(report["lights"][1]["dimmer"])

    async def test_the_scan_button_starts_a_new_run(self):
        self.assertTrue(await self.wait_for_scan())
        before = len(self.fake.received)
        self.mqtt.on_command("myhome/bridge/scan/set", "PRESS")
        self.assertTrue(await wait_until(lambda: self.bridge.status.scan_state.startswith("läuft")))
        self.assertTrue(await self.wait_for_scan())
        self.assertGreater(len(self.fake.received), before)

    async def test_a_lost_link_aborts_the_scan(self):
        self.assertTrue(await self.wait_for_scan())
        await self.bridge.gateway.stop()
        await self.bridge.scan_bus()
        self.assertEqual(self.bridge.status.scan_state, "kein Gateway verbunden")

    async def test_the_scan_empties_the_queued_requests_afterwards(self):
        # Measured 24.09.2026: after a scan 40 stale requests ran on for
        # minutes and nothing could be switched meanwhile.
        self.assertTrue(await self.wait_for_scan())
        self.bridge.trigger_scan()
        # The last phase of the scan: from here it is a few tenths of a second
        # to the end, so what is queued now is still queued when it finishes -
        # exactly the situation on the installation (40 in the queue).
        self.assertTrue(await wait_until(
            lambda: "Auswertung" in self.bridge.status.scan_state, 30),
            self.bridge.status.scan_state)
        for number in range(100, 300):
            self.bridge.background.outbox.put(f"*#1*{number}##", background=True)
        self.assertGreater(self.bridge.background.outbox.qsize(True), 150)
        with self.assertLogs(background_module._LOGGER, "INFO") as logs:
            self.assertTrue(await self.wait_for_scan())
        self.assertEqual(self.bridge.background.outbox.qsize(True), 0)
        self.assertTrue(any("dropped" in r.getMessage() for r in logs.records),
                        [r.getMessage() for r in logs.records])

    async def test_the_refresh_starts_again_a_while_after_the_scan(self):
        with mock.patch.object(bridge_module, "POST_SCAN_REFRESH", 0.05):
            self.assertTrue(await self.wait_for_scan())
            self.assertTrue(await wait_until(
                lambda: self.bridge.background.refreshing
                or any(f.startswith("*#1*12") for f in self.fake.received), 5))

    async def test_a_second_scan_replaces_the_pending_refresh_timer(self):
        # Otherwise the quiet period would count from the first scan's end, and
        # the second scan would get a shorter one than intended.
        self.assertTrue(await self.wait_for_scan())
        first_timer = self.bridge._post_scan_task
        self.assertIsNotNone(first_timer)
        self.bridge.trigger_scan()
        self.assertTrue(await wait_until(
            lambda: "Auswertung" in self.bridge.status.scan_state, 30))
        self.assertTrue(await self.wait_for_scan())
        self.assertIsNot(self.bridge._post_scan_task, first_timer,
                         "the second scan kept the first scan's stale timer")
        self.assertTrue(first_timer.cancelled())

    async def test_the_page_says_that_switching_is_slow_while_scanning(self):
        # The initial scan of asyncSetUp is still running here.
        self.assertTrue(self.bridge.web_status()["scanning"])
        self.assertTrue(await self.wait_for_scan())
        self.assertFalse(self.bridge.web_status()["scanning"])


class TestBrakeOption(unittest.IsolatedAsyncioTestCase):
    async def test_the_brake_threshold_from_the_options_reaches_the_gateway(self):
        settings = Settings(gateway=GatewayConfig(host="127.0.0.1"),
                            mqtt=MqttConfig(host="broker"),
                            brake=BrakeConfig(threshold=7, window=9.5))
        bridge = Bridge(settings, mqtt_factory=FakeMqtt, collect=lambda *_args: [],
                        web_options=LOCAL_WEB)
        pacing = bridge.gateway._pacing
        self.assertEqual((pacing.threshold, pacing.window), (7, 9.5))


@unittest.skipUnless(os.name == "posix", "needs POSIX signals")
class TestSigterm(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        root = logging.getLogger()
        self._root_state = (root.level, list(root.handlers))
        self._tmp = tempfile.TemporaryDirectory()
        self.broker = FakeBroker()
        broker_port = await self.broker.start()
        self.fake = FakeGateway(mode="none")
        gateway_port = await self.fake.start()
        self.settings = Settings(
            gateway=GatewayConfig(host="127.0.0.1", port=gateway_port),
            mqtt=MqttConfig(host="127.0.0.1", port=broker_port, base_topic="myhome"),
            scan=ScanConfig(on_start=False),
            devices_file=os.path.join(self._tmp.name, "devices.yaml"),
            discovered_file=os.path.join(self._tmp.name, "discovered.json"),
            poll_interval=0,
        )

    async def asyncTearDown(self) -> None:
        await self.fake.stop()
        await self.broker.stop()
        self._tmp.cleanup()
        root = logging.getLogger()
        level, handlers = self._root_state
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
        root.setLevel(level)

    def make_bridge(self, settings: Settings) -> Bridge:
        return Bridge(settings, gateway_options=FAST_GATEWAY, scan_timing=FAST_SCAN,
                      web_options=LOCAL_WEB)

    async def test_sigterm_shuts_down_cleanly(self):
        running = asyncio.create_task(app_main.main(lambda: self.settings, self.make_bridge))
        self.assertTrue(await wait_until(
            lambda: self.broker.retained.get("myhome/bridge/availability") == "online", 15))
        self.assertTrue(await wait_until(
            lambda: signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, None)),
            "the signal handler is not installed - not sending SIGTERM")
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertEqual(await asyncio.wait_for(running, 15), 0)
        self.assertEqual(self.broker.retained.get("myhome/bridge/availability"), "offline")
        self.assertEqual(self.broker.retained.get("myhome/bridge/lwt"), "offline")
        self.assertTrue(await wait_until(lambda: self.fake.open_connections == 0))
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        leftovers = [t for t in asyncio.all_tasks() if t.get_name().startswith(("bridge", "own-"))]
        self.assertEqual(leftovers, [], "tasks were not cancelled")

    async def test_invalid_options_end_with_exit_code_1(self):
        def broken() -> Settings:
            raise SettingsError("No gateway host configured")

        with self.assertLogs("app", "ERROR"):
            self.assertEqual(await app_main.main(broken, self.make_bridge), 1)

    def test_log_lines_carry_milliseconds(self):
        # Five frames a second are 0.2 s apart - seconds alone cannot show that.
        formatter = logging.Formatter(app_main.LOG_FORMAT, app_main.LOG_DATE_FORMAT)
        record = logging.LogRecord("app.own.session", 5, __file__, 1, "[command] >>> %s",
                                   ("*1*1*12##",), None)
        record.created = 1_000_000.25
        record.msecs = 250.0
        line = formatter.format(record)
        self.assertRegex(line, r"^\d\d:\d\d:\d\d\.250 ")
        self.assertTrue(line.endswith("app.own.session: [command] >>> *1*1*12##"))


if __name__ == "__main__":
    unittest.main()
