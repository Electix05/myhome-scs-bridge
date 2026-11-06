"""End-to-end: the real add-on image against a real Home Assistant and a real broker.

Started by .github/workflows/ha-compat.yaml for Home Assistant stable, beta and dev:

    HA_TAG=beta BRIDGE_IMAGE=<image> python tests/e2e/run_e2e.py

Only the gateway is simulated (tests/fake_gateway.py), and the Supervisor is left
out: the add-on gets the broker from its options.  Home Assistant is set up through
its public API, the way a person does it in the browser - onboarding, then the MQTT
integration.  The checks then go through the whole chain in both directions, restart
every part once, and finally read Home Assistant's log and repairs for anything that
concerns MQTT or this add-on.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import yaml
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "contract"))

import variants  # noqa: E402
from myhome_scs_mqtt.app.own import messages as msg  # noqa: E402
from myhome_scs_mqtt.app.own.gateway import KEEPALIVE_FRAME  # noqa: E402
from myhome_scs_mqtt.app.scan import ScanOptions, build_plan  # noqa: E402

# The job may run 45 minutes.  The checks stop themselves well before that, so that
# a run that hangs somewhere still gets to print the logs needed to find out why.
RUN_LIMIT = 30 * 60
RUN_DEADLINE = time.monotonic() + RUN_LIMIT
COMPOSE_TIMEOUT = 300

HA_URL = "http://127.0.0.1:8123"
WS_URL = "ws://127.0.0.1:8123/api/websocket"
CLIENT_ID = HA_URL + "/"
CONTROL = ("127.0.0.1", 20001)
COMPOSE_FILE = HERE / "compose.yaml"
GATEWAY_HOST = "fake-gateway"

OPTIONS = {
    "gateway_host": GATEWAY_HOST,
    "gateway_port": 20000,
    "gateway_password": "12345",
    "gateway_auth": "none",
    "mqtt_host": "mosquitto",
    "mqtt_port": 1883,
    "mqtt_discovery_prefix": variants.DISCOVERY_PREFIX,
    "mqtt_base_topic": variants.BASE_TOPIC,
    "devices_file": "/e2e/devices.yaml",
    "instance_id": "scs_bridge",
    "auto_discover": False,
    "scan_on_start": False,
    "scan_zones": 0,
    "scan_meters": 0,
    "scan_contacts": 0,
    "poll_interval": 0,
    "log_level": "info",
}

# A line in Home Assistant's log that concerns us: a warning or an error about MQTT
# or about this add-on.  Anything else in there is somebody else's business - that
# includes deprecation warnings: the ones about MQTT carry "mqtt" in the logger name
# and are caught here, and Home Assistant also files them as repair issues.
CONCERNS_US = re.compile(r"\b(WARNING|ERROR|CRITICAL)\b.*(mqtt|scs_bridge)", re.IGNORECASE)
# Expected while the checks restart the parts on purpose.
EXPECTED_NOISE: tuple[re.Pattern[str], ...] = ()
# Written into Home Assistant's log through its own logging (system_log.write) at the
# end of the run.  It has to come out of the log again and be caught by CONCERNS_US -
# otherwise the log check reads the wrong thing and would pass on nothing.
CONTROL_LOGGER = "scs_bridge.e2e"
CONTROL_MESSAGE = "control sample of the end-to-end run"
# A line of the add-on's own log that reports a problem (format: app/__main__.py).
BRIDGE_PROBLEM = re.compile(r" (ERROR|CRITICAL) |Traceback")


class OutOfTime(Exception):
    """The run as a whole took longer than RUN_LIMIT."""


# --------------------------------------------------------------------- helpers

def log(text: str) -> None:
    print(text, flush=True)


def compose(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), *args],
                            capture_output=True, text=True, timeout=COMPOSE_TIMEOUT, check=False)
    if check and result.returncode:
        raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def pull(service: str, attempts: int = 3) -> None:
    """Download the image of ``service``: a hiccup of the registry is no failure of ours."""
    for attempt in range(1, attempts + 1):
        try:
            compose("pull", "--quiet", service)
            return
        except RuntimeError as err:
            if attempt == attempts:
                raise
            log(f"     note: pulling {service} failed (attempt {attempt}): {err}")
            time.sleep(20 * attempt)


def wait_for(probe: Callable[[], Any], timeout: float, interval: float = 1.0) -> Any:
    """The first truthy answer of ``probe`` within ``timeout`` seconds, else None.

    Raises OutOfTime once the whole run has used up RUN_LIMIT.
    """
    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() >= RUN_DEADLINE:
            raise OutOfTime(f"the checks ran longer than {RUN_LIMIT // 60} minutes")
        try:
            value = probe()
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, WebSocketException):
            value = None        # not up yet, or the thing is not there yet
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def gateway(request: dict[str, Any]) -> dict[str, Any]:
    """One request to the control port of tests/e2e/gateway.py."""
    with socket.create_connection(CONTROL, timeout=10) as connection:
        connection.sendall((json.dumps(request) + "\n").encode())
        return json.loads(connection.makefile(encoding="utf-8").readline())


def number(text: Any) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def close_to(values: Any, expected: list[float], tolerance: float = 1.0) -> bool:
    return (isinstance(values, (list, tuple)) and len(values) == len(expected)
            and all(abs(float(a) - b) <= tolerance for a, b in zip(values, expected, strict=True)))


def scan_only_frames() -> set[str]:
    """The frames only a bus scan sends with OPTIONS - no keepalive, no refresh round."""
    plan = build_plan(ScanOptions(zones=OPTIONS["scan_zones"], meters=OPTIONS["scan_meters"],
                                  contacts=OPTIONS["scan_contacts"], deep=False))
    sent_anyway = {KEEPALIVE_FRAME, msg.status_request(msg.WHO_LIGHTING, "0")}
    for entity in variants.build_registry(GATEWAY_HOST).entities:
        sent_anyway.update(entity.poll_frames())
        sent_anyway.update(entity.probe_frames())
    return {frame for phase in plan for frame in phase.frames} - sent_anyway


# ---------------------------------------------------------- Home Assistant

class HomeAssistant:
    def __init__(self) -> None:
        self.token = ""
        self.refresh_token = ""

    def request(self, method: str, path: str, body: Any = None, form: dict[str, str] | None = None,
                auth: bool = True, retry: bool = True) -> Any:
        data, headers = None, {}
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(HA_URL + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as err:
            if err.code == 401 and auth and retry and self.refresh_token:
                self.refresh()
                return self.request(method, path, body, form, auth, retry=False)
            raise RuntimeError(f"{method} {path}: HTTP {err.code} {err.read()[:300]!r}") from err
        return json.loads(text) if text.strip() else None

    def refresh(self) -> None:
        tokens = self.request("POST", "/auth/token", auth=False, form={
            "grant_type": "refresh_token", "client_id": CLIENT_ID,
            "refresh_token": self.refresh_token})
        self.token = tokens["access_token"]

    def onboard(self) -> None:
        answer = self.request("POST", "/api/onboarding/users", auth=False, body={
            "client_id": CLIENT_ID, "name": "CI", "username": "ci",
            "password": secrets.token_urlsafe(18), "language": "en"})
        tokens = self.request("POST", "/auth/token", auth=False, form={
            "grant_type": "authorization_code", "client_id": CLIENT_ID,
            "code": answer["auth_code"]})
        self.token, self.refresh_token = tokens["access_token"], tokens["refresh_token"]
        # Not needed for the checks; finishing the onboarding keeps the log quiet.
        for path, body in (("/api/onboarding/core_config", {}),
                           ("/api/onboarding/analytics", {}),
                           ("/api/onboarding/integration",
                            {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID})):
            try:
                self.request("POST", path, body=body)
            except RuntimeError as err:
                log(f"     note: {err}")

    def set_up_mqtt(self) -> None:
        """The MQTT integration, through the same form a person fills in.

        The form is read, not assumed: a field Home Assistant adds later gets its
        default, so a new option does not break the check.
        """
        flow = self.request("POST", "/api/config/config_entries/flow",
                            body={"handler": "mqtt", "show_advanced_options": False})
        for _ in range(6):
            kind = flow.get("type")
            if kind == "create_entry":
                return
            if kind == "menu":
                answer: dict[str, Any] = {"next_step_id": "broker"}
            elif kind == "form":
                if flow.get("errors"):
                    raise RuntimeError(f"MQTT setup refused: {flow['errors']}")
                answer = fill_form(flow.get("data_schema") or [],
                                   {"broker": "mosquitto", "port": 1883})
            else:
                raise RuntimeError(f"unexpected step in the MQTT setup: {flow}")
            flow = self.request("POST", f"/api/config/config_entries/flow/{flow['flow_id']}",
                                body=answer)
        raise RuntimeError(f"the MQTT setup did not finish: {flow}")

    def ws(self, kind: str, **fields: Any) -> Any:
        with connect(WS_URL, open_timeout=20, max_size=None) as websocket:
            json.loads(websocket.recv(timeout=20))             # auth_required
            websocket.send(json.dumps({"type": "auth", "access_token": self.token}))
            reply = json.loads(websocket.recv(timeout=20))
            if reply.get("type") != "auth_ok":
                raise RuntimeError(f"websocket login: {reply}")
            websocket.send(json.dumps({"id": 1, "type": kind, **fields}))
            while True:
                message = json.loads(websocket.recv(timeout=60))
                if message.get("id") == 1 and message.get("type") == "result":
                    if not message.get("success"):
                        raise RuntimeError(f"{kind}: {message.get('error')}")
                    return message["result"]

    def entities(self) -> dict[str, str]:
        """unique_id -> entity_id of every MQTT entity."""
        return {entry["unique_id"]: entry["entity_id"]
                for entry in self.ws("config/entity_registry/list")
                if entry.get("platform") == "mqtt"}

    def states(self) -> dict[str, dict[str, Any]]:
        return {state["entity_id"]: state for state in self.request("GET", "/api/states")}

    def call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self.request("POST", f"/api/services/{domain}/{service}", body=data)


def fill_form(fields: list[dict[str, Any]], known: dict[str, Any]) -> dict[str, Any]:
    """Answer a config flow form: known values, defaults, and the safest choice."""
    answer: dict[str, Any] = {}
    for field in fields:
        name = field["name"]
        if name in known:
            answer[name] = known[name]
        elif field.get("type") == "expandable":
            answer[name] = fill_form(field.get("schema") or [], known)
        elif "default" in field:
            answer[name] = field["default"]
        elif field.get("required"):
            selector = field.get("selector") or {}
            if "boolean" in selector:
                answer[name] = False
            elif "select" in selector:
                options = [option["value"] if isinstance(option, dict) else option
                           for option in selector["select"].get("options", [])]
                answer[name] = "off" if "off" in options else options[0]
            else:
                answer[name] = ""
    return answer


# ------------------------------------------------------------------ checks

class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, what: str, ok: Any, detail: str | Callable[[], str] = "") -> bool:
        """Log one check.  A callable ``detail`` only runs when the check failed."""
        if not ok and callable(detail):
            try:
                detail = detail()
            except Exception as err:  # noqa: BLE001 - a broken detail must not hide the failure
                detail = f"(no detail: {type(err).__name__}: {err})"
        log(f"{'ok  ' if ok else 'FAIL'} {what}" + (f"  ->  {detail}" if detail and not ok else ""))
        if not ok:
            self.failures.append(f"{what}: {detail}" if detail else what)
        return bool(ok)


def is_on(state: dict[str, Any]) -> bool:
    return state["state"] == "on"


# Bus -> Home Assistant: frames the gateway sends, the entity, what it must show.
BUS_TO_HA: list[tuple[str, list[str], str, Callable[[dict[str, Any]], bool]]] = [
    ("relay reports on", ["*1*1*11##"], "scs_bridge_1_11", is_on),
    ("relay reports off", ["*1*0*11##"], "scs_bridge_1_11", lambda s: s["state"] == "off"),
    ("dimmer reports 75 %", ["*#1*12*1*175*2##"], "scs_bridge_1_12",
     lambda s: is_on(s) and s["attributes"].get("brightness") == 191),
    ("colour lamp reports a colour", ["*1*1*13##", "*#1*13*12*243*96*100##"], "scs_bridge_1_13",
     lambda s: s["attributes"].get("color_mode") == "hs"
     and close_to(s["attributes"].get("hs_color"), [243, 96])),
    ("white lamp reports 2700 K", ["*1*1*14##", "*#1*14*14*37037##"], "scs_bridge_1_14",
     lambda s: s["attributes"].get("color_mode") == "color_temp"
     and abs((number(s["attributes"].get("color_temp_kelvin")) or 0) - 2700) <= 10),
    ("socket reports on", ["*1*1*16##"], "scs_bridge_1_16", is_on),
    ("shutter starts going up", ["*2*1*21##"], "scs_bridge_2_21",
     lambda s: s["state"] == "opening"),
    ("shutter reports its position", ["*#2*22*10*10*50*001*0##"], "scs_bridge_2_22",
     lambda s: s["attributes"].get("current_position") == 50),
    ("zone reports 24.3 degrees", ["*#4*1*0*0243##"], "scs_bridge_4_1_temperature",
     lambda s: number(s["state"]) == 24.3),
    ("thermostat shows the same", [], "scs_bridge_4_1",
     lambda s: number(s["attributes"].get("current_temperature")) == 24.3),
    ("meter reports 850 W", ["*#18*51*113*0000850##"], "scs_bridge_18_51_power",
     lambda s: number(s["state"]) == 850),
    ("contact closes", ["*25*32#1*31##"], "scs_bridge_25_1", is_on),
    ("presence detected", ["*#1*1010*7*1*0*2##"], "scs_bridge_1_1010_bmse_motion", is_on),
    ("brightness measured", ["*#1*1010*6*220##"], "scs_bridge_1_1010_bmse_lux",
     lambda s: number(s["state"]) == 220),
    ("key press arrives as an event", ["*25*21#1*22##"], "scs_bridge_25_22_btn1",
     lambda s: s["attributes"].get("event_type") == "press"),
]

# Home Assistant -> bus: the action, the entity, the data, the frame that must arrive.
HA_TO_BUS: list[tuple[str, str, str, str, dict[str, Any], str]] = [
    ("relay on", "light", "turn_on", "scs_bridge_1_11", {}, "*1*1*11##"),
    ("relay off", "light", "turn_off", "scs_bridge_1_11", {}, "*1*0*11##"),
    ("dimmer to 50 %", "light", "turn_on", "scs_bridge_1_12", {"brightness": 128},
     "*#1*12*#1*150*255##"),
    ("colour green", "light", "turn_on", "scs_bridge_1_13", {"hs_color": [120, 100]},
     "*#1*13*#12*120*100*100##"),
    ("white 2700 K", "light", "turn_on", "scs_bridge_1_14", {"color_temp_kelvin": 2700},
     "*#1*14*#14*37037##"),
    ("socket on", "switch", "turn_on", "scs_bridge_1_16", {}, "*1*1*16##"),
    ("shutter up", "cover", "open_cover", "scs_bridge_2_21", {}, "*2*1*21##"),
    ("shutter stop", "cover", "stop_cover", "scs_bridge_2_21", {}, "*2*0*21##"),
    ("shutter to 30 %", "cover", "set_cover_position", "scs_bridge_2_22", {"position": 30},
     "*#2*22*#11#001#1*30##"),
    ("zone to 22 degrees", "climate", "set_temperature", "scs_bridge_4_1", {"temperature": 22},
     "*#4*1*#14*0220*3##"),
    ("fan coil to speed 2", "climate", "set_fan_mode", "scs_bridge_4_2", {"fan_mode": "2"},
     "*#4*2*#11*2##"),
    ("scenario 1", "scene", "turn_on", "scs_bridge_0_01_scn1", {}, "*0*1*01##"),
]

RELAY = "scs_bridge_1_11"
LINK = "scs_bridge_link"
SCAN_STATE = "scs_bridge_scan"
SCAN_BUTTON = "scs_bridge_scan_button"


class EndToEnd:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.ha = HomeAssistant()
        self.report = Report()
        self.ids: dict[str, str] = {}
        announced = variants.announced(GATEWAY_HOST)
        self.expected = [payload["unique_id"] for _c, _o, payload in announced]
        self.component_of = {payload["unique_id"]: component for component, _o, payload in announced}
        # Entities that only appear once the bus has told the bridge about them.
        self.taught = set(self.expected) - {
            payload["unique_id"] for _c, _o, payload in variants.announced(GATEWAY_HOST, teach_frames=())}

    # -------------------------------------------------------------- set-up

    def prepare(self) -> None:
        (self.workdir / "ha").mkdir(parents=True)
        (self.workdir / "data").mkdir()
        (self.workdir / "options.json").write_text(json.dumps(OPTIONS), encoding="utf-8")
        (self.workdir / "devices.yaml").write_text(
            yaml.safe_dump(variants.DEVICES, allow_unicode=True, sort_keys=False), encoding="utf-8")
        # The containers run as root, the runner does not; nothing in here is secret.
        for path in (self.workdir, self.workdir / "ha", self.workdir / "data"):
            path.chmod(0o777)

    def start(self) -> None:
        log("== Start: broker, simulated gateway, add-on, Home Assistant")
        pull("mosquitto")       # the only image that comes from a registry at this point
        compose("up", "-d")
        up = wait_for(lambda: self.ha.request("GET", "/api/onboarding", auth=False) is not None, 600, 3)
        if not up:
            raise RuntimeError("Home Assistant did not come up within 10 minutes")
        version = compose("exec", "-T", "homeassistant", "python3", "-c",
                          "import homeassistant.const as c; print(c.__version__)").strip()
        log(f"   Home Assistant {version}")
        self.ha.onboard()
        self.ha.set_up_mqtt()
        log("   onboarding done, MQTT integration set up")

    # -------------------------------------------------------------- checks

    def check_entities(self) -> bool:
        first = [uid for uid in self.expected if uid not in self.taught]
        found = wait_for(lambda: (ids := self.ha.entities()) and all(uid in ids for uid in first)
                         and ids, 180, 3)
        if not self.report.check(f"Home Assistant created all {len(first)} entities", found,
                                 lambda: f"missing: {sorted(set(first) - set(self.ha.entities()))}"):
            return False
        wait_for(lambda: gateway({"emit": "*#4*0##"}).get("event_sessions"), 60)
        for frame in variants.TEACH_FRAMES:
            gateway({"emit": frame})
        found = wait_for(lambda: (ids := self.ha.entities()) and all(uid in ids for uid in self.taught)
                         and ids, 60, 2)
        self.report.check("entities learned from the bus appear as well", found,
                          lambda: f"missing: {sorted(self.taught - set(self.ha.entities()))}")
        self.ids = self.ha.entities()
        for uid in self.expected:
            entity_id = self.ids.get(uid, "")
            component = self.component_of[uid]
            if entity_id and not entity_id.startswith(component + "."):
                self.report.check(f"{uid} is a {component}", False, entity_id)
        return True

    def check_devices(self) -> None:
        devices = self.ha.ws("config/device_registry/list")
        hub = next((d for d in devices if ["mqtt", "scs_bridge"] in d.get("identifiers", [])), None)
        self.report.check("the bridge appears as a device", hub)
        if hub:
            children = [d for d in devices if any(i[0] == "mqtt" and str(i[1]).startswith("scs_bridge_")
                                                  for i in d.get("identifiers", []))]
            linked = [d for d in children if d.get("via_device_id") == hub["id"]]
            log(f"   note: {len(linked)} of {len(children)} devices hang below the bridge (via_device)")

    def entity(self, uid: str) -> dict[str, Any] | None:
        return self.ha.states().get(self.ids.get(uid, ""))

    def not_available(self) -> list[str]:
        """The expected entities without a state or with "unavailable" - one request for all."""
        states = self.ha.states()
        return [uid for uid in self.expected
                if states.get(self.ids.get(uid, ""), {}).get("state") in (None, "unavailable")]

    def check_available(self) -> None:
        linked = wait_for(lambda: (state := self.entity(LINK)) and is_on(state), 90, 2)
        self.report.check("the gateway link shows up as connected", linked)
        wait_for(lambda: not self.not_available(), 60, 2)
        missing = self.not_available()
        self.report.check("no entity is unavailable or without a state", not missing, f"{missing}")

    def check_bus_to_ha(self) -> None:
        log("== Bus -> Home Assistant")
        for what, frames, uid, expected in BUS_TO_HA:
            for frame in frames:
                gateway({"emit": frame})
            state = wait_for(lambda uid=uid, expected=expected: (s := self.entity(uid)) and expected(s)
                             and s, 20, 0.5)
            self.report.check(what, state, lambda uid=uid: f"{uid}: {self.entity(uid)}")

    def check_ha_to_bus(self) -> None:
        log("== Home Assistant -> bus")
        for what, domain, service, uid, data, frame in HA_TO_BUS:
            gateway({"clear": True})
            self.ha.call(domain, service, {"entity_id": self.ids.get(uid, uid), **data})
            arrived = wait_for(lambda frame=frame: frame in gateway({"frames": True})["frames"], 20, 0.5)
            self.report.check(what, arrived,
                              lambda frame=frame: f"expected {frame}, got {gateway({'frames': True})['frames']}")

    def published_discoveries(self) -> int:
        return compose("logs", "--no-color", "bridge").count("Published MQTT discovery")

    def check_restarts(self) -> None:
        log("== Restarts")
        before = self.published_discoveries()
        compose("restart", "homeassistant")
        back = wait_for(lambda: self.ha.request("GET", "/api/") is not None, 300, 3)
        self.report.check("Home Assistant comes back after a restart", back)
        again = wait_for(lambda: self.published_discoveries() > before, 120, 3)
        self.report.check("the add-on announces itself again after Home Assistant's restart", again)
        kept = wait_for(lambda: (ids := self.ha.entities()) and all(uid in ids for uid in self.expected)
                        and ids, 120, 3)
        self.report.check("all entities are still there", kept)
        gateway({"clear": True})
        self.ha.call("light", "turn_on", {"entity_id": self.ids.get(RELAY)})
        self.report.check("switching works after Home Assistant's restart",
                          wait_for(lambda: "*1*1*11##" in gateway({"frames": True})["frames"], 30, 1))

        compose("stop", "bridge")
        self.report.check("entities turn unavailable when the add-on stops",
                          wait_for(lambda: (s := self.entity(RELAY)) and s["state"] == "unavailable", 60, 1))
        compose("start", "bridge")
        self.report.check("and come back when it starts again",
                          wait_for(lambda: (s := self.entity(RELAY)) and s["state"] != "unavailable", 120, 2))

        compose("stop", "fake-gateway")
        self.report.check("a lost gateway shows as disconnected",
                          wait_for(lambda: (s := self.entity(LINK)) and s["state"] == "off", 90, 2))
        self.report.check("and its entities as unavailable",
                          wait_for(lambda: (s := self.entity(RELAY)) and s["state"] == "unavailable", 60, 2))
        compose("start", "fake-gateway")
        self.report.check("the link comes back with the gateway",
                          wait_for(lambda: (s := self.entity(LINK)) and is_on(s), 180, 3))

    def check_scan_button(self) -> None:
        log("== Bus scan")
        gateway({"clear": True})
        self.ha.call("button", "press", {"entity_id": self.ids.get(SCAN_BUTTON)})
        # Only a frame that nothing but a scan sends proves it: the keepalive, and right
        # after the gateway's restart a whole refresh round, cross the same session.
        scan_only = scan_only_frames()
        started = wait_for(lambda: scan_only & set(gateway({"frames": True})["frames"]), 60, 1)
        self.report.check("the scan button starts a bus scan", started,
                          lambda: f"none of {sorted(scan_only)} arrived")
        finished = wait_for(lambda: (state := self.entity(SCAN_STATE))
                            and state["state"].startswith("fertig") and state, 180, 2)
        self.report.check("the scan finishes and says so in Home Assistant", finished,
                          lambda: f"{self.entity(SCAN_STATE)}")

    def check_logs(self) -> None:
        log("== Logs and repairs")
        self.ha.call("system_log", "write", {"message": CONTROL_MESSAGE, "level": "warning",
                                              "logger": CONTROL_LOGGER})
        ha_log = (wait_for(lambda: (text := compose("logs", "--no-color", "homeassistant"))
                           and CONTROL_MESSAGE in text and text, 30, 1)
                  or compose("logs", "--no-color", "homeassistant"))
        control = [line for line in ha_log.splitlines() if CONTROL_MESSAGE in line]
        self.report.check("the log check sees a warning written into Home Assistant's log",
                          control and all(CONCERNS_US.search(line) for line in control),
                          f"{len(ha_log)} characters read, control sample: {control}")
        lines = [line for line in ha_log.splitlines()
                 if CONCERNS_US.search(line) and CONTROL_MESSAGE not in line
                 and not any(pattern.search(line) for pattern in EXPECTED_NOISE)]
        self.report.check("Home Assistant logs no warning about MQTT or the add-on", not lines,
                          "\n    ".join([""] + lines[:20]))
        issues = [issue for issue in self.ha.ws("repairs/list_issues").get("issues", [])
                  if issue.get("domain") == "mqtt" or "scs_bridge" in json.dumps(issue)]
        self.report.check("no repair issue about MQTT or the add-on", not issues, f"{issues}")
        problems = [line for line in compose("logs", "--no-color", "bridge").splitlines()
                    if BRIDGE_PROBLEM.search(line)]
        self.report.check("the add-on logs no error", not problems, "\n    ".join([""] + problems[:20]))

    # ----------------------------------------------------------------- run

    def run(self) -> int:
        self.prepare()
        try:
            self.start()
            if self.check_entities():
                self.check_devices()
                self.check_available()
                self.check_bus_to_ha()
                self.check_ha_to_bus()
                self.check_restarts()
                self.check_scan_button()
            self.check_logs()
        except Exception as err:  # noqa: BLE001 - whatever happened, the logs below must be printed
            self.report.check("the end-to-end run itself", False, f"{type(err).__name__}: {err}")
        if self.report.failures:
            log("\n== The end of the logs")
            for service in ("homeassistant", "bridge", "fake-gateway", "mosquitto"):
                log(f"--- {service}")
                log(compose("logs", "--no-color", "--tail", "200", service, check=False))
            log(f"\n{len(self.report.failures)} check(s) failed:")
            for failure in self.report.failures:
                log(f"  - {failure}")
            return 1
        log("\nAll end-to-end checks passed.")
        return 0


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="scs-e2e-", dir=os.environ.get("RUNNER_TEMP")))
    os.environ["E2E_DIR"] = str(workdir)
    try:
        return EndToEnd(workdir).run()
    finally:
        compose("down", "--volumes", "--timeout", "20", check=False)


if __name__ == "__main__":
    sys.exit(main())
