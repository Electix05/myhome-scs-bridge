"""The add-on package: manifest, image, translations - checked before it ships.

A mistake here does not show in any other test, only on the device: an option
the code never reads, a version that differs between the manifest and the
changelog, an image that compiles on the target.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import unittest

import yaml

from myhome_scs_mqtt.app import __version__, settings as settings_module
from myhome_scs_mqtt.app.web import server as server_module

ADDON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "myhome_scs_mqtt")
# Defaults of the Supervisor: the app linter refuses them spelled out in config.yaml.
DEFAULT_ROLE = "default"
DEFAULT_INGRESS_PORT = 8099


def read(name: str) -> str:
    with open(os.path.join(ADDON_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def read_project(name: str) -> str:
    """Files one level above the add-on folder, e.g. the project's README.md."""
    with open(os.path.join(os.path.dirname(ADDON_DIR), name), encoding="utf-8") as handle:
        return handle.read()


def manifest() -> dict:
    return yaml.safe_load(read("config.yaml"))


class TestIdentity(unittest.TestCase):
    def test_a_separate_add_on_next_to_the_old_one(self):
        config = manifest()
        self.assertEqual(config["slug"], "myhome_scs_bridge")
        self.assertEqual(config["name"], "MyHome SCS Bridge")
        self.assertEqual(config["panel_title"], "MyHome SCS Bridge")
        # The old add-on: slug myhome_scs_mqtt, base topic myhome.
        self.assertNotEqual(config["options"]["mqtt_base_topic"], "myhome")
        self.assertEqual(config["options"]["mqtt_base_topic"],
                         settings_module.MqttConfig().base_topic)

    def test_version_is_the_same_everywhere(self):
        top = re.search(r"^## (\S+)", read("CHANGELOG.md"), re.MULTILINE)
        self.assertIsNotNone(top, "CHANGELOG.md has no version heading")
        self.assertEqual(manifest()["version"], __version__)
        self.assertEqual(top.group(1), __version__)
        self.assertEqual(__version__, "1.25.0")
        # DOCS.md and README.md name the version in prose (install step, log
        # line to expect) - nothing else keeps those in step with a bump.
        self.assertIn(__version__, read("DOCS.md"))
        self.assertIn(__version__, read_project("README.md"))


class TestOptions(unittest.TestCase):
    def setUp(self) -> None:
        self.config = manifest()
        self.schema = self.config["schema"]
        self.options = self.config["options"]

    def test_every_default_has_a_schema_entry_and_required_ones_a_default(self):
        self.assertEqual(set(self.options) - set(self.schema), set())
        required = {key for key, kind in self.schema.items() if not str(kind).endswith("?")}
        self.assertEqual(required - set(self.options), set())

    def test_the_code_reads_every_option_and_nothing_else(self):
        source = read("app/settings.py")
        read_keys = set(re.findall(r'options,\s*"([a-z_]+)"', source))
        read_keys |= set(re.findall(r'options\.get\("([a-z_]+)"', source))
        self.assertEqual(read_keys, set(self.schema))

    def test_the_defaults_load_once_the_gateway_is_filled_in(self):
        options = dict(self.options, gateway_host="192.0.2.10", mqtt_host="192.0.2.20")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "options.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(options, handle)
            settings = settings_module.Settings.load(options_file=path,
                                                     fetch_mqtt_service=lambda: None,
                                                     data_dir=directory)
        self.assertEqual(settings.gateway.host, "192.0.2.10")
        self.assertEqual((settings.brake.threshold, settings.brake.window),
                         (settings_module.BRAKE_THRESHOLD, settings_module.BRAKE_WINDOW))
        self.assertEqual(settings.mqtt.base_topic, self.options["mqtt_base_topic"])

    def test_an_empty_gateway_address_stops_with_a_clear_message(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "options.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self.options, handle)
            with self.assertRaisesRegex(settings_module.SettingsError, "gateway_host"):
                settings_module.Settings.load(options_file=path,
                                              fetch_mqtt_service=lambda: None,
                                              data_dir=directory)

    def test_lists_and_ranges_match_the_code(self):
        self.assertEqual(self.schema["log_level"],
                         "list(" + "|".join(settings_module.LOG_LEVELS) + ")")
        self.assertEqual(self.schema["gateway_auth"],
                         "list(" + "|".join(settings_module.AUTH_MODES) + ")")
        low, high = settings_module.BRAKE_THRESHOLD_RANGE
        self.assertEqual(self.schema["bus_brake_threshold"], f"int({low},{high})")
        low, high = settings_module.BRAKE_WINDOW_RANGE
        self.assertEqual(self.schema["bus_brake_window"], f"float({low:g},{high:g})")
        self.assertEqual((self.options["bus_brake_threshold"], self.options["bus_brake_window"]),
                         (settings_module.BRAKE_THRESHOLD, settings_module.BRAKE_WINDOW))
        low, high = settings_module.COMMAND_RESERVE_RANGE
        self.assertEqual(self.schema["command_reserve"], f"float({low:g},{high:g})")
        self.assertEqual(self.options["command_reserve"], settings_module.COMMAND_RESERVE)
        low, high = settings_module.DEAD_AFTER_RANGE
        self.assertEqual(self.schema["dead_after"], f"int({low},{high})")
        self.assertEqual(self.options["dead_after"], settings_module.DEAD_AFTER)

    def test_the_instance_id_is_checked_by_the_supervisor_as_well(self):
        """The manifest refuses nonsense before the add-on even starts."""
        self.assertEqual(self.options["instance_id"], settings_module.DEFAULT_INSTANCE_ID)
        schema = self.schema["instance_id"]
        self.assertTrue(schema.startswith("match(") and schema.endswith(")"), schema)
        supervisor = re.compile(schema[len("match("):-1])
        longest = "a" * settings_module.INSTANCE_ID_MAX_LENGTH
        for good in (settings_module.DEFAULT_INSTANCE_ID, "haus2", "scs_bridge_2", longest):
            with self.subTest(good=good):
                self.assertTrue(supervisor.match(good), good)
                self.assertTrue(settings_module.INSTANCE_ID_PATTERN.match(good), good)
        for bad in ("", "Haus2", "haus 2", "haus-2", "haus/2", longest + "a"):
            with self.subTest(bad=bad):
                self.assertIsNone(supervisor.match(bad), bad)

    def test_translations_describe_every_option(self):
        for language in ("de", "en"):
            with self.subTest(language=language):
                texts = yaml.safe_load(read(f"translations/{language}.yaml"))["configuration"]
                self.assertEqual(set(texts), set(self.schema))
                for key, text in texts.items():
                    self.assertTrue(text.get("name") and text.get("description"), key)

    def test_no_real_addresses_or_passwords_in_the_defaults(self):
        text = read("config.yaml") + read("DOCS.md") + read("README.md")
        for candidate in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
            address = ipaddress.ip_address(candidate)
            documentation = ipaddress.ip_network("192.0.2.0/24")
            proxy = ipaddress.ip_address(server_module.INGRESS_PROXY)
            self.assertTrue(address in documentation or address == proxy
                            or address.is_loopback, candidate)
        self.assertEqual(self.options["gateway_password"], "12345")


class TestImage(unittest.TestCase):
    def test_least_privileges(self):
        config = manifest()
        self.assertEqual(config["map"], ["addon_config:rw"])
        self.assertEqual(config["services"], ["mqtt:need"])
        self.assertEqual(config.get("hassio_role", DEFAULT_ROLE), DEFAULT_ROLE)
        self.assertNotIn("host_network", config)
        self.assertNotIn("privileged", config)
        self.assertNotIn("ports", config)

    def test_architectures_home_assistant_still_supports(self):
        self.assertEqual(sorted(manifest()["arch"]), ["aarch64", "amd64"])

    def test_ingress_port_matches_the_web_server(self):
        config = manifest()
        self.assertTrue(config["ingress"])
        self.assertEqual(config.get("ingress_port", DEFAULT_INGRESS_PORT),
                         server_module.INGRESS_PORT)

    def test_dockerfile_pins_its_base_and_keeps_no_cache(self):
        dockerfile = read("Dockerfile")
        base = re.search(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE).group(1)
        self.assertEqual(base, "ghcr.io/home-assistant/base:3.22")
        self.assertNotIn("BUILD_FROM", dockerfile.split("FROM", 1)[1])
        self.assertIn("apk add --no-cache", dockerfile)
        self.assertIn("--no-cache-dir", dockerfile)
        self.assertIn("--only-binary=:all:", dockerfile)
        self.assertRegex(dockerfile, r'CMD \[ "/run.sh" \]')
        # DOCS.md 1.5 names the image the install pulls.
        self.assertIn(base, read("DOCS.md"))

    def test_run_script_hands_over_with_exec(self):
        script = read("run.sh")
        self.assertTrue(script.startswith("#!/usr/bin/with-contenv bashio"))
        self.assertRegex(script, r"(?m)^exec python3 -m app$")
        self.assertTrue(os.access(os.path.join(ADDON_DIR, "run.sh"), os.X_OK))

    def test_requirements_are_pinned(self):
        lines = [line.strip() for line in read("requirements.txt").splitlines()
                 if line.strip() and not line.startswith("#")]
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[0-9][0-9.]*$")

    def test_docker_ignores_what_macos_and_python_leave_behind(self):
        ignored = read(".dockerignore")
        for pattern in ("__pycache__", "._*", ".DS_Store"):
            self.assertIn(pattern, ignored)


if __name__ == "__main__":
    unittest.main()
