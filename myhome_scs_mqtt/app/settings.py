"""Add-on options, MQTT credentials and the device definition file.

Everything in here is synchronous and may block (file access, one HTTP call
to the Supervisor).  The entry point runs :meth:`Settings.load` in a worker
thread, so the event loop never waits for it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import yaml

_LOGGER = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
OPTIONS_FILE = os.environ.get("OPTIONS_FILE", os.path.join(DATA_DIR, "options.json"))
DEFAULT_DEVICES_FILE = "/config/devices.yaml"

SUPERVISOR_API = "http://supervisor"
SUPERVISOR_TIMEOUT = 10.0
# Right after a power cut the Supervisor may still be busy; a few patient
# attempts are better than an add-on that stops for good.
SUPERVISOR_ATTEMPTS = 5
SUPERVISOR_RETRY_DELAY = 3.0

LOG_LEVELS = {
    "trace": 5,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}
# The same values as own.session.AUTH_MODES; settings must not depend on the
# protocol package, a test keeps both in step.
AUTH_MODES = ("auto", "none", "open", "hmac")

# When the adaptive brake of the bus pacing engages: after BRAKE_THRESHOLD
# refusals or silences within BRAKE_WINDOW seconds (own.gateway.BusPacing).
# 3 in 3 s is a provisional starting value, decided on 17.09.2026 - the final
# value is set after the measurement on real hardware.  Settings must not
# depend on the protocol package; a test keeps these in step with it.
BRAKE_THRESHOLD = 3
BRAKE_WINDOW = 3.0
BRAKE_THRESHOLD_RANGE = (1, 50)
BRAKE_WINDOW_RANGE = (0.5, 60.0)

# Frames per second the add-on keeps free for commands: our own housekeeping
# (refresh, probes, bus scan) may only use what is left of the hard ceiling of
# five frames per second (own.gateway.COMMAND_RESERVE, a test keeps both in
# step).  Measured on 24.09.2026: during a bus scan not a single lamp could be
# switched - thirteen attempts, all refused.
COMMAND_RESERVE = 1.5
COMMAND_RESERVE_RANGE = (0.0, 4.0)

# After this many refusals in a row an address counts as dead: it is no longer
# polled and shows up in Home Assistant as unavailable, until it answers again
# (one retry every ten minutes).  0 switches that off.  Decided on 24.09.2026.
DEAD_AFTER = 3
DEAD_AFTER_RANGE = (0, 20)

# Keys whose values are bus addresses.  They stay text exactly as written:
# unquoted, YAML reads 04 as the number 4 and 0210 even as octal 136.
ADDRESS_KEYS = ("where", "zone")

_TAG_STR = "tag:yaml.org,2002:str"
_NUMBER_TAGS = ("tag:yaml.org,2002:int", "tag:yaml.org,2002:float")

EXAMPLE_DEVICES = '''# Device definitions for the MyHome SCS Bridge.
#
# "where" is the OpenWebNet address of the device, exactly as it appears on the
# bus - for lights and shutters that is the A/PL address (A=2, PL=3 -> "23"),
# for thermostats the zone number, for meters the 5N address of the meter.
# Addresses on a local bus keep their suffix, e.g. "23#4#01".  Put addresses
# in quotes: "04" is not the number 4.
#
# Everything in this file is optional: with auto_discover enabled the bridge
# also creates entities for the addresses it sees on the bus.

lights:
  # - where: "11"
  #   name: Kitchen ceiling
  # - where: "12"
  #   name: Dining table
  #   dimmer: true          # reports its brightness in percent steps

covers:
  # - where: "21"
  #   name: Shutter living room
  #   travel_time: 25       # seconds, estimates the position of basic actuators
  # - where: "23"
  #   name: Venetian blind
  #   device_class: blind
  #   tilt_time: 1.4        # seconds for a full turn of the slats
  #   position_control: auto  # auto, native, software or off

climate:
  # - where: "1"
  #   name: Zone living room
  #   heizen: true
  #   kuehlen: false
  #   min_temp: 5
  #   max_temp: 35

energy:
  # - where: "51"
  #   name: House meter
  #   energy_total: true    # additionally expose the kWh totalizer

switches:
  # - where: "1"
  #   name: Garden socket
  #   who: 1                # 1 = lighting/actuator, 9 = auxiliary

scenes:
  # - where: "01"
  #   name: Scenario module 01
  #   scenarios: [1, 2, 3, 4]

binary_sensors:
  # - where: "1"            # contact number, on the bus it becomes 31
  #   name: Doorbell contact
  #   schliesser: true      # make contact: closed = triggered
  #   device_class: sound

buttons:
  # - where: "22"
  #   name: Wall keypad hallway
  #   kind: cen_plus        # cen_plus (WHO 25) or cen (WHO 15)
  #   buttons: [1, 2, 3, 4]

motion_sensors:
  # - where: "1010"
  #   name: Presence sensor hall
  #   motion_poll: 0        # seconds between presence requests, 0 = off
  #   off_delay: 60         # seconds without detection until "clear"

pumps:
  # - where: "3"            # actuator number, at the central unit: 0#3
  #   name: Circulation pump
  #   zone: "0"
'''


class SettingsError(Exception):
    """The add-on options cannot work; the message tells the user what to fix."""


@dataclass
class GatewayConfig:
    host: str = ""
    port: int = 20000
    password: str = ""
    auth: str = "auto"


DEFAULT_DISCOVERY_PREFIX = "homeassistant"
# Not "myhome": that is the base topic of the old add-on (myhome_scs_mqtt), and
# both clear topics under their base topic that they do not know.
DEFAULT_BASE_TOPIC = "scs_bridge"

# Identity of this instance of the add-on.  One Home Assistant can run the
# add-on several times - one instance per MyHome installation - and the id is
# what keeps them apart: it is the prefix of every unique_id and object_id (see
# devices/base.py, where the same value is the default of Context.bridge_id), the
# node level of every discovery topic, and part of the MQTT client id.  The
# default must stay "scs_bridge": changing it renames every entity in Home
# Assistant and its history starts over.
DEFAULT_INSTANCE_ID = "scs_bridge"
# Lower case letters, digits and underscores: the value ends up in MQTT topics
# and in entity ids, where nothing else is safe.
INSTANCE_ID_PATTERN = re.compile(r"^[a-z0-9_]+$")
# The old add-on "MyHome SCS MQTT Bridge" (slug myhome_scs_mqtt) removes every
# retained discovery message whose topic *contains* this text.  An instance id
# containing it would have its announcements swept away by the old add-on.
FOREIGN_INSTANCE_ID = "myhome_scs_bridge"
# Keeps unique_ids readable and the MQTT client id within every broker's limit.
INSTANCE_ID_MAX_LENGTH = 32


@dataclass
class MqttConfig:
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""
    ssl: bool = False
    discovery_prefix: str = DEFAULT_DISCOVERY_PREFIX
    base_topic: str = DEFAULT_BASE_TOPIC
    # See DEFAULT_INSTANCE_ID.  It travels with the MQTT settings because
    # everything it touches is on the MQTT side: the client id, the discovery
    # node and the prefix of every unique_id.
    instance_id: str = DEFAULT_INSTANCE_ID


@dataclass
class ScanConfig:
    on_start: bool = True
    zones: int = 20
    meters: int = 8
    contacts: int = 20


@dataclass
class BrakeConfig:
    """When the adaptive brake engages: ``threshold`` troubles in ``window`` s."""

    threshold: int = BRAKE_THRESHOLD
    window: float = BRAKE_WINDOW


@dataclass
class Settings:
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    brake: BrakeConfig = field(default_factory=BrakeConfig)
    devices_file: str = DEFAULT_DEVICES_FILE
    discovered_file: str = os.path.join(DATA_DIR, "discovered.json")
    auto_discover: bool = True
    poll_interval: int = 300
    command_reserve: float = COMMAND_RESERVE
    dead_after: int = DEAD_AFTER
    # In front of every device name, e.g. "Haus 2 Küche".  Empty: names stay
    # exactly as the device file writes them.
    name_prefix: str = ""
    log_level: str = "info"

    @property
    def instance_id(self) -> str:
        """Identity of this instance; see :data:`DEFAULT_INSTANCE_ID`.

        Stored once, in the MQTT settings, so that nothing can drift apart.
        """
        return self.mqtt.instance_id

    @classmethod
    def load(
        cls,
        options_file: str = OPTIONS_FILE,
        fetch_mqtt_service: Callable[[], dict[str, Any] | None] | None = None,
        data_dir: str = DATA_DIR,
    ) -> Settings:
        """Read the add-on options; ask the Supervisor for MQTT when needed.

        Raises:
            SettingsError: when the options cannot work.
        """
        options = read_options(options_file)
        gateway = GatewayConfig(
            host=_text(options, "gateway_host"),
            port=_integer(options, "gateway_port", 20000, 1, 65535),
            password=str(options.get("gateway_password") or ""),
            auth=_choice(options, "gateway_auth", AUTH_MODES, "auto"),
        )
        if not gateway.host:
            raise SettingsError(
                "No gateway host configured - set 'gateway_host' in the add-on options")

        mqtt = MqttConfig(
            host=_text(options, "mqtt_host"),
            port=_integer(options, "mqtt_port", 1883, 1, 65535),
            username=_text(options, "mqtt_username"),
            password=str(options.get("mqtt_password") or ""),
            discovery_prefix=_topic(options, "mqtt_discovery_prefix", DEFAULT_DISCOVERY_PREFIX),
            base_topic=_topic(options, "mqtt_base_topic", DEFAULT_BASE_TOPIC),
            instance_id=_instance_id(options, "instance_id"),
        )
        if not mqtt.host:
            fetch = fetch_mqtt_service or fetch_supervisor_mqtt
            service = fetch()
            if service:
                apply_mqtt_service(mqtt, service)
        if not mqtt.host:
            raise SettingsError(
                "No MQTT broker found. Install the Mosquitto add-on or fill in "
                "the mqtt_* options manually.")

        scan = ScanConfig(
            on_start=bool(options.get("scan_on_start", True)),
            zones=_integer(options, "scan_zones", 20, 0, 99),
            meters=_integer(options, "scan_meters", 8, 0, 99),
            contacts=_integer(options, "scan_contacts", 20, 0, 99),
        )
        brake = BrakeConfig(
            threshold=_integer(options, "bus_brake_threshold", BRAKE_THRESHOLD,
                               *BRAKE_THRESHOLD_RANGE),
            window=_decimal(options, "bus_brake_window", BRAKE_WINDOW, *BRAKE_WINDOW_RANGE),
        )
        log_level = _text(options, "log_level", "info").lower()
        if log_level not in LOG_LEVELS:
            _LOGGER.warning("Unknown log_level '%s' - using info", log_level)
            log_level = "info"
        devices_file = _text(options, "devices_file", DEFAULT_DEVICES_FILE)
        # Once, at start-up: a second instance that was only half converted is
        # the mistake that costs entities, and it is easy to spot from here.
        warning = instance_isolation_warning(mqtt.instance_id, mqtt.base_topic, devices_file)
        if warning:
            _LOGGER.warning("%s", warning)
        return cls(
            gateway=gateway,
            mqtt=mqtt,
            scan=scan,
            brake=brake,
            devices_file=devices_file,
            discovered_file=os.path.join(data_dir, "discovered.json"),
            auto_discover=bool(options.get("auto_discover", True)),
            poll_interval=_integer(options, "poll_interval", 300, 0, 86400),
            command_reserve=_decimal(options, "command_reserve", COMMAND_RESERVE,
                                     *COMMAND_RESERVE_RANGE),
            dead_after=_integer(options, "dead_after", DEAD_AFTER, *DEAD_AFTER_RANGE),
            name_prefix=_text(options, "name_prefix"),
            log_level=log_level,
        )


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------

def read_options(path: str) -> dict[str, Any]:
    """The options the Supervisor wrote; an empty dict when there are none."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        _LOGGER.warning("%s not found - falling back to defaults", path)
        return {}
    except (OSError, json.JSONDecodeError) as err:
        raise SettingsError(f"Cannot read {path}: {err}") from err
    if not isinstance(data, dict):
        raise SettingsError(f"{path} does not contain an object")
    return data


def _text(options: dict[str, Any], key: str, default: str = "") -> str:
    value = options.get(key)
    return default if value is None else str(value).strip()


def _integer(options: dict[str, Any], key: str, default: int,
             minimum: int, maximum: int) -> int:
    value = options.get(key)
    if value is None or value == "":
        return default
    try:
        # Through str: int(4.5) would quietly give 4, int("4.5") refuses.
        number = int(str(value))
    except (TypeError, ValueError) as err:
        raise SettingsError(f"Option '{key}' must be a whole number, got {value!r}") from err
    if not minimum <= number <= maximum:
        raise SettingsError(f"Option '{key}' must be between {minimum} and {maximum}")
    return number


def _decimal(options: dict[str, Any], key: str, default: float,
             minimum: float, maximum: float) -> float:
    value = options.get(key)
    if value is None or value == "":
        return default
    try:
        # Through str, like _integer: a stray true must not become 1.0.
        number = float(str(value))
    except (TypeError, ValueError) as err:
        raise SettingsError(f"Option '{key}' must be a number, got {value!r}") from err
    # Written this way round, "nan" fails the check as well.
    if not minimum <= number <= maximum:
        raise SettingsError(f"Option '{key}' must be between {minimum:g} and {maximum:g}")
    return number


def _choice(options: dict[str, Any], key: str, allowed: tuple[str, ...], default: str) -> str:
    value = _text(options, key, default).lower()
    if value not in allowed:
        raise SettingsError(f"Option '{key}' must be one of {', '.join(allowed)}")
    return value


def _topic(options: dict[str, Any], key: str, default: str) -> str:
    value = _text(options, key, default).strip("/")
    if not value or "#" in value or "+" in value:
        raise SettingsError(
            f"Option '{key}' must be a plain MQTT topic without # or +, got {value!r}")
    return value


def _instance_id(options: dict[str, Any], key: str) -> str:
    """The identity of this instance; the default when the option is empty.

    Raises:
        SettingsError: when the value cannot be part of a topic or an entity id,
            or when it would collide with the old add-on.
    """
    value = _text(options, key, DEFAULT_INSTANCE_ID) or DEFAULT_INSTANCE_ID
    if not INSTANCE_ID_PATTERN.match(value):
        raise SettingsError(
            f"Option '{key}' may only contain lower case letters, digits and "
            f"underscores - '{DEFAULT_INSTANCE_ID}' or 'haus2' for example. "
            f"Got {value!r}.")
    if len(value) > INSTANCE_ID_MAX_LENGTH:
        raise SettingsError(
            f"Option '{key}' must not be longer than {INSTANCE_ID_MAX_LENGTH} "
            f"characters, got {len(value)}.")
    if FOREIGN_INSTANCE_ID in value:
        raise SettingsError(
            f"Option '{key}' must not contain '{FOREIGN_INSTANCE_ID}'. The old add-on "
            "'MyHome SCS MQTT Bridge' deletes every discovery message whose topic "
            f"contains that text, so our entities would disappear. Got {value!r}.")
    return value


def instance_isolation_warning(instance_id: str, base_topic: str, devices_file: str) -> str:
    """One line of warning when a second instance looks half converted, else "".

    Two instances of this add-on need three settings of their own: the instance
    id (unique_ids, discovery node, MQTT client id), the MQTT base topic (state
    and command topics) and the device file.  All three at their default is a
    single instance; all three changed is a proper second one.  A mixture is
    what goes wrong in practice - the entities of both instances then share
    their unique_ids, or both instances write the same device file.
    """
    at_default = {
        "instance_id": instance_id == DEFAULT_INSTANCE_ID,
        "mqtt_base_topic": base_topic == DEFAULT_BASE_TOPIC,
        "devices_file": devices_file == DEFAULT_DEVICES_FILE,
    }
    if len(set(at_default.values())) < 2:
        return ""
    changed = ", ".join(key for key, default in at_default.items() if not default)
    kept = ", ".join(key for key, default in at_default.items() if default)
    return (f"This looks like a half-converted second instance: {changed} differs from "
            f"the default while {kept} does not. A second instance of this add-on needs "
            "its own instance_id, mqtt_base_topic and devices_file - otherwise both "
            "instances share entities, topics or the same device file. Running a single "
            "instance? Then this line can be ignored.")


# --------------------------------------------------------------------------
# Supervisor
# --------------------------------------------------------------------------

def fetch_supervisor_mqtt(
    token: str | None = None,
    attempts: int = SUPERVISOR_ATTEMPTS,
    retry_delay: float = SUPERVISOR_RETRY_DELAY,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    """Ask the Supervisor for the MQTT service (Mosquitto add-on).

    Returns the ``data`` part of the answer, or None when there is no
    Supervisor, no service, or no answer after a few attempts.
    """
    token = os.environ.get("SUPERVISOR_TOKEN", "") if token is None else token
    if not token:
        return None
    request = urllib.request.Request(
        SUPERVISOR_API + "/services/mqtt",
        headers={"Authorization": "Bearer " + token},
    )
    for attempt in range(1, attempts + 1):
        try:
            with opener(request, timeout=SUPERVISOR_TIMEOUT) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as err:
            # An answer, just not a good one: retrying will not change it.
            _LOGGER.warning("Supervisor did not hand out MQTT credentials: %s", err)
            err.close()             # the error carries the open response
            return None
        except (urllib.error.URLError, OSError, ValueError) as err:
            if attempt >= attempts:
                _LOGGER.warning("Supervisor did not hand out MQTT credentials: %s", err)
                return None
            _LOGGER.debug("Supervisor not reachable (%s) - attempt %d of %d",
                          err, attempt, attempts)
            sleep(retry_delay)
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and data.get("host"):
            return data
        _LOGGER.warning("Supervisor offers no MQTT service")
        return None
    return None


def apply_mqtt_service(mqtt: MqttConfig, data: dict[str, Any]) -> None:
    """Take host and credentials from the Supervisor's MQTT service."""
    mqtt.host = str(data.get("host") or "")
    try:
        mqtt.port = int(data.get("port") or 1883)
    except (TypeError, ValueError):
        mqtt.port = 1883
    mqtt.username = str(data.get("username") or "")
    mqtt.password = str(data.get("password") or "")
    mqtt.ssl = bool(data.get("ssl", False))
    _LOGGER.info("Using the MQTT broker offered by the Supervisor: %s:%s",
                 mqtt.host, mqtt.port)


# --------------------------------------------------------------------------
# Device file
# --------------------------------------------------------------------------

@dataclass
class DeviceDefinitions:
    """What devices.yaml describes.

    ``complete`` is False when parts of the file could not be read.  The bridge
    then leaves the broker alone: removing the entities of every device it did
    not understand would turn a typo into lost Home Assistant entities.
    """

    sections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    complete: bool = True
    problems: list[str] = field(default_factory=list)

    def problem(self, text: str) -> None:
        self.complete = False
        self.problems.append(text)
        _LOGGER.error("%s", text)


class _DeviceFileLoader(yaml.SafeLoader):
    """A safe YAML loader that keeps bus addresses as text."""

    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict:
        # The tags were resolved while composing; changing them here, before
        # the values are built, keeps an unquoted 0210 as the text "0210".
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                if (isinstance(key_node, yaml.ScalarNode)
                        and key_node.value in ADDRESS_KEYS
                        and isinstance(value_node, yaml.ScalarNode)
                        and value_node.tag in _NUMBER_TAGS):
                    value_node.tag = _TAG_STR
        return super().construct_mapping(node, deep=deep)


def parse_devices(text: str) -> Any:
    """Parse device YAML with addresses kept as written."""
    return yaml.load(text, Loader=_DeviceFileLoader)  # noqa: S506 - SafeLoader subclass


DEVICE_FILE_HEADER = "# Written by the web interface of the MyHome SCS Bridge.\n\n"


def render_device_file(sections: dict[str, list[dict[str, Any]]],
                       order: Iterable[str] = ()) -> str:
    """devices.yaml text: the sections in ``order`` first, empty ones left out.

    Every field of an entry is kept, known or not.  Addresses are text and stay
    quoted where YAML would otherwise read a number ("04", "0210").
    """
    ordered = {section: sections[section] for section in order if sections.get(section)}
    ordered.update({section: items for section, items in sections.items()
                    if section not in ordered and items})
    body = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False,
                          default_flow_style=False) if ordered else ""
    return DEVICE_FILE_HEADER + body


def write_device_file(path: str, sections: dict[str, list[dict[str, Any]]],
                      order: Iterable[str] = ()) -> None:
    """Render and write devices.yaml atomically.  Blocking - run it in a thread.

    The old add-on wrote the file on the event loop: a slow disk froze the
    whole bridge, bus included, for as long as the write took.

    Raises:
        OSError: when the file cannot be written - the old file stays.
    """
    atomic_write_text(path, render_device_file(sections, order))


def load_devices(path: str) -> DeviceDefinitions:
    """Read devices.yaml, creating a commented example on first start."""
    result = DeviceDefinitions()
    if not path:
        return result
    if not os.path.exists(path):
        try:
            atomic_write_text(path, EXAMPLE_DEVICES)
            _LOGGER.info("Created an example device file at %s", path)
        except OSError as err:
            _LOGGER.warning("Could not create %s: %s", path, err)
        return result

    try:
        with open(path, encoding="utf-8") as handle:
            data = parse_devices(handle.read())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as err:
        result.problem(f"Cannot read {path}: {err}")
        return result
    if data is None:
        return result
    if not isinstance(data, dict):
        result.problem(f"{path} must contain a mapping of device types")
        return result

    for section, items in data.items():
        if items is None:
            continue
        if not isinstance(items, list):
            result.problem(f"Section '{section}' in {path} must be a list - ignored")
            continue
        entries = []
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                result.problem(f"Entry {index} in section '{section}' is not a mapping - ignored")
            elif item.get("where") in (None, ""):
                result.problem(f"Entry {index} in section '{section}' has no 'where' - ignored")
            elif not isinstance(item["where"], str):
                # After the loader a real address is always text; anything else
                # (a nested mapping, "on", a date) is a slip in the file.
                result.problem(
                    f"Entry {index} in section '{section}' has a 'where' that is not "
                    f"plain text ({item['where']!r}) - ignored")
            else:
                entries.append(item)
        result.sections[str(section)] = entries
    return result


def atomic_write_text(path: str, text: str) -> None:
    """Write a file so that a power cut leaves either the old or the new one.

    Raises:
        OSError: when the directory or the file cannot be written.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
