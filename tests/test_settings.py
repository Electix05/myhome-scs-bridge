"""Add-on options, the Supervisor's MQTT service and the device file."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import yaml

from myhome_scs_mqtt.app import settings as settings_module
from myhome_scs_mqtt.app.own import gateway as gateway_module, session as session_module
from myhome_scs_mqtt.app.settings import (
    EXAMPLE_DEVICES, MqttConfig, Settings, SettingsError, apply_mqtt_service,
    atomic_write_text, fetch_supervisor_mqtt, load_devices, parse_devices, render_device_file,
    write_device_file,
)


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def path(self, *parts: str) -> str:
        return os.path.join(self.dir, *parts)

    def write(self, name: str, text: str) -> str:
        path = self.path(name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path


class TestOptions(TempDirTestCase):
    BASE = {
        "gateway_host": "192.0.2.10",
        "gateway_password": "12345",
        "mqtt_host": "192.0.2.20",
    }

    def load(self, fetch=None, **overrides) -> Settings:
        values = dict(self.BASE)
        values.update(overrides)
        values = {key: value for key, value in values.items() if value is not None}
        options = self.write("options.json", json.dumps(values))
        return Settings.load(options_file=options, fetch_mqtt_service=fetch or (lambda: None),
                             data_dir=self.dir)

    def test_everything_set_by_hand(self):
        asked = []
        settings = self.load(
            fetch=lambda: asked.append(True),
            gateway_auth="hmac", gateway_port=20001, mqtt_port=1884,
            mqtt_username="bridge", mqtt_password="12345",
            mqtt_base_topic="/haus/", scan_zones=5, scan_on_start=False,
            poll_interval=0, log_level="debug", auto_discover=False,
            devices_file="/config/anlage.yaml",
        )
        self.assertEqual(asked, [], "the Supervisor was asked although mqtt_host is set")
        self.assertEqual((settings.gateway.host, settings.gateway.port, settings.gateway.auth),
                         ("192.0.2.10", 20001, "hmac"))
        self.assertEqual(settings.gateway.password, "12345")
        self.assertEqual((settings.mqtt.host, settings.mqtt.port, settings.mqtt.username),
                         ("192.0.2.20", 1884, "bridge"))
        self.assertEqual(settings.mqtt.base_topic, "haus")
        self.assertEqual(settings.mqtt.discovery_prefix, "homeassistant")
        self.assertEqual((settings.scan.zones, settings.scan.on_start), (5, False))
        self.assertEqual((settings.poll_interval, settings.log_level), (0, "debug"))
        self.assertFalse(settings.auto_discover)
        self.assertEqual(settings.devices_file, "/config/anlage.yaml")

    def test_defaults(self):
        settings = self.load(gateway_password=None)
        self.assertEqual(settings.gateway.port, 20000)
        self.assertEqual(settings.gateway.password, "")
        self.assertEqual(settings.gateway.auth, "auto")
        self.assertEqual(settings.mqtt.port, 1883)
        # Not "myhome": that is the old add-on's, which runs side by side.
        self.assertEqual(settings.mqtt.base_topic, "scs_bridge")
        self.assertEqual((settings.scan.zones, settings.scan.meters, settings.scan.contacts),
                         (20, 8, 20))
        self.assertEqual(settings.poll_interval, 300)
        self.assertEqual(settings.devices_file, "/config/devices.yaml")
        self.assertTrue(settings.auto_discover)
        self.assertEqual((settings.brake.threshold, settings.brake.window), (3, 3.0))

    def test_the_brake_threshold_is_an_option(self):
        settings = self.load(bus_brake_threshold=5, bus_brake_window="4.5")
        self.assertEqual((settings.brake.threshold, settings.brake.window), (5, 4.5))

    def test_brake_defaults_match_the_gateway(self):
        self.assertEqual((settings_module.BRAKE_THRESHOLD, settings_module.BRAKE_WINDOW),
                         (gateway_module.BRAKE_THRESHOLD, gateway_module.BRAKE_WINDOW))

    def test_the_command_reserve_is_an_option(self):
        self.assertEqual(self.load().command_reserve, 1.5)
        self.assertEqual(self.load(command_reserve="2.5").command_reserve, 2.5)

    def test_dead_after_is_an_option(self):
        self.assertEqual(self.load().dead_after, 3)
        self.assertEqual(self.load(dead_after=0).dead_after, 0)      # switched off
        self.assertEqual(self.load(dead_after="5").dead_after, 5)

    def test_the_command_reserve_matches_the_gateway_and_leaves_room(self):
        self.assertEqual(settings_module.COMMAND_RESERVE, gateway_module.COMMAND_RESERVE)
        # A reserve of the whole bus would stop every refresh for good.
        self.assertLess(settings_module.COMMAND_RESERVE_RANGE[1],
                        gateway_module.MAX_FRAMES_PER_SECOND)

    def test_the_supervisor_fills_in_the_broker(self):
        service = {"host": "core-mosquitto", "port": 1883, "username": "addons",
                   "password": "12345", "ssl": False}
        settings = self.load(fetch=lambda: service, mqtt_host=None)
        self.assertEqual((settings.mqtt.host, settings.mqtt.username),
                         ("core-mosquitto", "addons"))

    def test_without_any_broker_the_message_says_what_to_do(self):
        with self.assertRaisesRegex(SettingsError, "Mosquitto"):
            self.load(mqtt_host=None)

    def test_the_gateway_host_is_required(self):
        with self.assertRaisesRegex(SettingsError, "gateway_host"):
            self.load(gateway_host="  ")

    def test_invalid_values_explain_themselves(self):
        cases = {
            "gateway_auth": "magic",
            "gateway_port": 0,
            "mqtt_port": "abc",
            "mqtt_base_topic": "my/#",
            "mqtt_discovery_prefix": "",
            "scan_zones": 100,
            "scan_meters": 4.5,
            "poll_interval": -1,
            "bus_brake_threshold": 0,
            "bus_brake_window": "nan",
            "command_reserve": 5,
            "dead_after": 99,
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                with self.assertRaisesRegex(SettingsError, key):
                    self.load(**{key: value})

    def test_unknown_log_level_falls_back_to_info(self):
        with self.assertLogs(settings_module._LOGGER, "WARNING"):
            self.assertEqual(self.load(log_level="loud").log_level, "info")

    def test_missing_options_file(self):
        with self.assertLogs(settings_module._LOGGER, "WARNING"):
            with self.assertRaises(SettingsError):
                Settings.load(options_file=self.path("nope.json"),
                              fetch_mqtt_service=lambda: None, data_dir=self.dir)

    def test_broken_options_file(self):
        for text in ("{not json", "[1, 2]"):
            with self.subTest(text=text):
                path = self.write("options.json", text)
                with self.assertRaises(SettingsError):
                    Settings.load(options_file=path, fetch_mqtt_service=lambda: None)

    def test_discovered_devices_live_in_the_data_dir(self):
        self.assertEqual(self.load().discovered_file, self.path("discovered.json"))

    def test_auth_modes_match_the_session(self):
        self.assertEqual(settings_module.AUTH_MODES, session_module.AUTH_MODES)


class _Opener:
    """Stands in for urllib.request.urlopen."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return io.BytesIO(outcome)


ANSWER = json.dumps({"result": "ok", "data": {
    "host": "core-mosquitto", "port": 1883, "username": "addons",
    "password": "12345", "ssl": False, "protocol": "3.1.1"}}).encode()


class TestSupervisor(unittest.TestCase):
    def test_without_token_nothing_is_asked(self):
        opener = _Opener()
        self.assertIsNone(fetch_supervisor_mqtt(token="", opener=opener))
        self.assertEqual(opener.requests, [])

    def test_credentials_come_from_the_answer(self):
        opener = _Opener(ANSWER)
        data = fetch_supervisor_mqtt(token="abc", opener=opener, sleep=lambda _s: None)
        self.assertEqual(data["host"], "core-mosquitto")
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "http://supervisor/services/mqtt")
        self.assertEqual(request.get_header("Authorization"), "Bearer abc")
        self.assertGreater(timeout, 0)

        mqtt = MqttConfig()
        with self.assertLogs(settings_module._LOGGER, "INFO"):
            apply_mqtt_service(mqtt, data)
        self.assertEqual((mqtt.host, mqtt.port, mqtt.username, mqtt.password, mqtt.ssl),
                         ("core-mosquitto", 1883, "addons", "12345", False))

    def test_a_busy_supervisor_is_asked_again(self):
        sleeps = []
        failure = urllib.error.URLError("connection refused")
        opener = _Opener(failure, failure, ANSWER)
        data = fetch_supervisor_mqtt(token="abc", opener=opener, sleep=sleeps.append)
        self.assertEqual(data["host"], "core-mosquitto")
        self.assertEqual(sleeps, [settings_module.SUPERVISOR_RETRY_DELAY] * 2)

    def test_gives_up_after_the_last_attempt(self):
        sleeps = []
        opener = _Opener(*[OSError("down")] * 3)
        with self.assertLogs(settings_module._LOGGER, "WARNING"):
            self.assertIsNone(fetch_supervisor_mqtt(token="abc", attempts=3, opener=opener,
                                                    sleep=sleeps.append))
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(len(sleeps), 2)

    def test_an_http_error_is_final(self):
        error = urllib.error.HTTPError("http://supervisor/services/mqtt", 400,
                                       "Service not available", None, None)
        opener = _Opener(error)
        with self.assertLogs(settings_module._LOGGER, "WARNING"):
            self.assertIsNone(fetch_supervisor_mqtt(token="abc", opener=opener,
                                                    sleep=lambda _s: None))
        self.assertEqual(len(opener.requests), 1)

    def test_an_answer_without_service(self):
        opener = _Opener(json.dumps({"result": "error", "data": {}}).encode())
        with self.assertLogs(settings_module._LOGGER, "WARNING"):
            self.assertIsNone(fetch_supervisor_mqtt(token="abc", opener=opener))

    def test_an_odd_port_falls_back_to_the_default(self):
        mqtt = MqttConfig()
        with self.assertLogs(settings_module._LOGGER, "INFO"):
            apply_mqtt_service(mqtt, {"host": "broker", "port": "x"})
        self.assertEqual(mqtt.port, 1883)


class TestDeviceFile(TempDirTestCase):
    def test_first_start_creates_a_readable_example(self):
        path = self.path("config", "devices.yaml")
        with self.assertLogs(settings_module._LOGGER, "INFO"):
            result = load_devices(path)
        self.assertTrue(os.path.exists(path))
        self.assertTrue(result.complete)
        self.assertEqual(result.sections, {})
        # The example itself reads without a single problem.
        again = load_devices(path)
        self.assertTrue(again.complete, again.problems)
        self.assertEqual(again.sections, {})
        self.assertIsInstance(parse_devices(EXAMPLE_DEVICES), dict)

    def test_addresses_stay_exactly_as_written(self):
        path = self.write("devices.yaml", """
lights:
  - where: 04
    name: Office
  - where: 0210
  - where: 12
covers:
  - where: 21
    travel_time: 25
    tilt_time: 1.4
pumps:
  - where: 3
    zone: 0
climate:
  - where: "2"
    min_temp: 16.5
""")
        result = load_devices(path)
        self.assertTrue(result.complete, result.problems)
        self.assertEqual([item["where"] for item in result.sections["lights"]],
                         ["04", "0210", "12"])
        cover = result.sections["covers"][0]
        self.assertEqual(cover["where"], "21")
        # Everything else keeps its type.
        self.assertEqual((cover["travel_time"], cover["tilt_time"]), (25, 1.4))
        self.assertEqual(result.sections["pumps"][0]["zone"], "0")
        self.assertEqual(result.sections["climate"][0]["min_temp"], 16.5)

    def test_why_plain_yaml_is_not_enough(self):
        # Documents the trap the loader avoids: octal 0210 is 136.
        self.assertEqual(yaml.safe_load("where: 0210")["where"], 136)
        self.assertEqual(yaml.safe_load("where: 04")["where"], 4)

    def test_fields_the_code_does_not_know_survive(self):
        path = self.write("devices.yaml", """
scenes:
  - where: "01"
    scenarios: [1, 2]
    scenario_names:
      1: Alles aus
    custom_note: installed 2019
""")
        item = load_devices(path).sections["scenes"][0]
        self.assertEqual(item["custom_note"], "installed 2019")
        self.assertEqual(item["scenario_names"], {1: "Alles aus"})

    def test_broken_yaml_is_reported_instead_of_raised(self):
        path = self.write("devices.yaml", "lights: [\n  - where: '12'\n")
        with self.assertLogs(settings_module._LOGGER, "ERROR"):
            result = load_devices(path)
        self.assertFalse(result.complete)
        self.assertEqual(result.sections, {})
        self.assertTrue(result.problems)

    def test_the_file_must_be_a_mapping(self):
        path = self.write("devices.yaml", "- where: '12'\n")
        with self.assertLogs(settings_module._LOGGER, "ERROR"):
            self.assertFalse(load_devices(path).complete)

    def test_a_section_that_is_not_a_list(self):
        path = self.write("devices.yaml",
                          "lights:\n  where: '12'\ncovers:\n  - where: '21'\n")
        with self.assertLogs(settings_module._LOGGER, "ERROR"):
            result = load_devices(path)
        self.assertFalse(result.complete)
        self.assertNotIn("lights", result.sections)
        self.assertEqual(result.sections["covers"], [{"where": "21"}])

    def test_entries_without_address_are_dropped_and_reported(self):
        path = self.write("devices.yaml",
                          "lights:\n  - name: Half done\n  - where: '12'\n  - just text\n")
        with self.assertLogs(settings_module._LOGGER, "ERROR"):
            result = load_devices(path)
        self.assertEqual(result.sections["lights"], [{"where": "12"}])
        self.assertFalse(result.complete)
        self.assertEqual(len(result.problems), 2)

    def test_empty_file_and_empty_sections_are_fine(self):
        for text in ("", "lights:\ncovers:\n", "# only a comment\n"):
            with self.subTest(text=text):
                result = load_devices(self.write("devices.yaml", text))
                self.assertTrue(result.complete)
                self.assertEqual(result.sections, {})

    def test_no_path_means_no_devices(self):
        self.assertTrue(load_devices("").complete)

    def test_unreadable_files(self):
        os.mkdir(self.path("folder.yaml"))
        with self.assertLogs(settings_module._LOGGER, "ERROR"):
            self.assertFalse(load_devices(self.path("folder.yaml")).complete)
        with open(self.path("binary.yaml"), "wb") as handle:
            handle.write(b"\xff\xfe\x00garbage")
        with self.assertLogs(settings_module._LOGGER, "ERROR"):
            self.assertFalse(load_devices(self.path("binary.yaml")).complete)

    def test_an_address_that_is_not_text_is_a_problem(self):
        # Neither crashes the loader; both mark the file as incomplete.
        for text in ("lights:\n  - where: !!map {a: 1}\n", "lights:\n  - where: on\n"):
            with self.subTest(text=text):
                path = self.write("devices.yaml", text)
                with self.assertLogs(settings_module._LOGGER, "ERROR"):
                    result = load_devices(path)
                self.assertFalse(result.complete)
                self.assertEqual(result.sections["lights"], [])


class TestWritingTheDeviceFile(TempDirTestCase):
    def test_what_the_web_interface_writes_reads_back_unchanged(self):
        sections = {
            "covers": [{"where": "0210", "name": "Rollo", "tilt_time": 1.4}],
            "custom": [{"where": "1", "note": "unknown section"}],
            "lights": [{"where": "04", "name": "Küche", "dimmer": True,
                        "custom_note": "installed 2019"},
                       {"where": "12#4#01"}],
            "scenes": [],
        }
        text = render_device_file(sections, order=["lights", "covers", "scenes"])
        self.assertTrue(text.startswith("#"))
        result = load_devices(self.write("devices.yaml", text))
        self.assertTrue(result.complete, result.problems)
        self.assertEqual(list(result.sections), ["lights", "covers", "custom"])
        self.assertEqual([item["where"] for item in result.sections["lights"]], ["04", "12#4#01"])
        self.assertEqual(result.sections["lights"][0]["custom_note"], "installed 2019")
        self.assertEqual(result.sections["covers"][0],
                         {"where": "0210", "name": "Rollo", "tilt_time": 1.4})
        # Quoted in the file itself, so even plain YAML keeps them as text.
        self.assertEqual(yaml.safe_load(text)["lights"][0]["where"], "04")

    def test_nothing_left_is_an_empty_but_readable_file(self):
        result = load_devices(self.write("devices.yaml", render_device_file({})))
        self.assertTrue(result.complete)
        self.assertEqual(result.sections, {})


class TestBackupOfTheDeviceFile(TempDirTestCase):
    """The page writes the whole file anew - what it replaces must not be lost."""

    BY_HAND = "# Erdgeschoss\nlights:\n  - where: '12'   # über der Spüle\n    name: Küche\n"

    def read(self, name: str) -> str:
        with open(self.path(name), encoding="utf-8") as handle:
            return handle.read()

    def save(self, name: str) -> None:
        write_device_file(self.path("devices.yaml"), {"lights": [{"where": "12", "name": name}]})

    def test_the_replaced_version_stays_next_to_the_file(self):
        self.write("devices.yaml", self.BY_HAND)
        self.save("Küche")
        self.assertNotIn("über der Spüle", self.read("devices.yaml"))
        self.assertEqual(self.read("devices.yaml.bak"), self.BY_HAND)
        self.assertEqual(self.read("devices.yaml.manual.bak"), self.BY_HAND)

    def test_the_version_written_by_hand_survives_a_second_save(self):
        # ".bak" alone would hold the first save by then - and the comments
        # somebody wrote into the file would be gone for good.
        self.write("devices.yaml", self.BY_HAND)
        self.save("Küche")
        first_save = self.read("devices.yaml")
        self.save("Küche neu")
        self.assertEqual(self.read("devices.yaml.bak"), first_save)
        self.assertEqual(self.read("devices.yaml.manual.bak"), self.BY_HAND)

    def test_nothing_to_keep_means_no_copy(self):
        self.save("Küche")                                  # there was no file yet
        self.assertEqual(os.listdir(self.dir), ["devices.yaml"])
        self.save("Küche")                                  # the same again
        self.assertEqual(os.listdir(self.dir), ["devices.yaml"])

    def test_the_example_file_is_not_somebodys_handwriting(self):
        self.write("devices.yaml", EXAMPLE_DEVICES)
        self.save("Küche")
        self.assertEqual(sorted(os.listdir(self.dir)), ["devices.yaml", "devices.yaml.bak"])

    def test_a_copy_that_fails_does_not_stop_the_save(self):
        self.write("devices.yaml", self.BY_HAND)
        real_write = settings_module.atomic_write_text

        def no_room_for_copies(path: str, text: str) -> None:
            if path.endswith(".bak"):
                raise OSError("disk full")
            real_write(path, text)

        with mock.patch.object(settings_module, "atomic_write_text", no_room_for_copies):
            with self.assertLogs(settings_module._LOGGER, "WARNING") as logs:
                self.save("Küche")
        self.assertIn("name: Küche", self.read("devices.yaml"))
        self.assertEqual(len(logs.records), 2)

    def test_a_second_installation_keeps_its_own_copies(self):
        # Every instance has a device file of its own, and so has its backup.
        self.write("devices_haus2.yaml", self.BY_HAND)
        write_device_file(self.path("devices_haus2.yaml"), {"lights": [{"where": "12"}]})
        self.assertEqual(sorted(os.listdir(self.dir)),
                         ["devices_haus2.yaml", "devices_haus2.yaml.bak",
                          "devices_haus2.yaml.manual.bak"])


class TestAtomicWrite(TempDirTestCase):
    def test_creates_directories_and_replaces_the_file(self):
        path = self.path("deep", "dir", "discovered.json")
        atomic_write_text(path, "first")
        atomic_write_text(path, "second")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "second")
        self.assertEqual(os.listdir(self.path("deep", "dir")), ["discovered.json"])

    def test_a_failed_write_keeps_the_old_file(self):
        path = self.write("devices.yaml", "old")
        with mock.patch.object(settings_module.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic_write_text(path, "new")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "old")
        self.assertEqual(os.listdir(self.dir), ["devices.yaml"])


if __name__ == "__main__":
    unittest.main()
