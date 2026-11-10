"""The web interface: input checks, the HTTP server over real sockets, the monitor."""

from __future__ import annotations

import asyncio
import json
import logging
import unittest
import urllib.error
from unittest import mock

from myhome_scs_mqtt.app.bridge.registry import FACTORIES
from myhome_scs_mqtt.app.devices.base import DEVICE_CLASSES
from myhome_scs_mqtt.app.devices.light import Light
from myhome_scs_mqtt.app.own import messages as msg
from myhome_scs_mqtt.app.settings import parse_devices, render_device_file
from myhome_scs_mqtt.app.web import schema as schema_module, server as server_module
from myhome_scs_mqtt.app.web.monitor import BusMonitor
from myhome_scs_mqtt.app.web.server import (
    MAX_BODY, WebHandlers, WebServer, clean_devices, frame_problem, request_restart,
)

from .helpers import Recorder


async def http(port: int, method: str, path: str, body: object = None,
               raw_body: bytes | None = None, headers: dict[str, str] | None = None
               ) -> tuple[int, str]:
    """One request over a real socket; (status, body)."""
    if raw_body is None:
        raw_body = json.dumps(body).encode() if body is not None else b""
    head = f"{method} {path} HTTP/1.1\r\nHost: x\r\n"
    extra = {"Content-Length": str(len(raw_body))}
    extra.update(headers or {})
    head += "".join(f"{name}: {value}\r\n" for name, value in extra.items())
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(head.encode() + b"\r\n" + raw_body)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 10)
    finally:
        writer.close()
    header, _, content = raw.partition(b"\r\n\r\n")
    return int(header.split()[1]), content.decode("utf-8", "replace")


class TestSchema(unittest.TestCase):
    """The interface must not offer anything the add-on then throws away."""

    def test_offered_device_classes_are_the_valid_ones(self):
        for section, component in (("binary_sensors", "binary_sensor"), ("covers", "cover"),
                                   ("switches", "switch")):
            field = [f for f in schema_module.SCHEMA[section]["fields"]
                     if f["key"] == "device_class"][0]
            offered = {o["value"] if isinstance(o, dict) else o for o in field["options"]}
            self.assertEqual(offered, DEVICE_CLASSES[component], section)

    def test_every_class_carries_a_readable_label(self):
        for option in schema_module.CONTACT_CLASSES + schema_module.COVER_CLASSES:
            self.assertNotEqual(option["label"], option["value"])

    def test_every_device_type_of_the_bridge_is_offered(self):
        self.assertEqual(set(schema_module.SCHEMA), set(FACTORIES))

    def test_lights_offer_colour_and_a_kelvin_range(self):
        fields = {field["key"]: field for field in schema_module.SCHEMA["lights"]["fields"]}
        self.assertEqual(list(fields),
                         ["dimmer", "rgbw", "tunable_white", "min_kelvin", "max_kelvin"])
        for key in ("dimmer", "rgbw", "tunable_white"):
            self.assertEqual(fields[key]["type"], "bool", key)
        # The range of the lamp that was measured, as a hint in the field.
        self.assertEqual(fields["min_kelvin"]["placeholder"], "2000")
        self.assertEqual(fields["max_kelvin"]["placeholder"], "6200")
        self.assertEqual(fields["min_kelvin"]["type"], "number")


class TestDeviceFileRoundTrip(unittest.TestCase):
    """What the page saves has to come back unchanged - in every combination."""

    ENTRIES = [
        {"where": "12", "name": "Relais"},
        {"where": "13", "name": "Dimmer", "dimmer": True},
        {"where": "0211", "name": "Farbe", "rgbw": True},
        {"where": "29", "name": "Weiß", "tunable_white": True,
         "min_kelvin": 2200, "max_kelvin": 5000},
        {"where": "30", "name": "Beides", "dimmer": True, "rgbw": True,
         "tunable_white": True, "eigene_notiz": "Bauteil 2019"},
    ]

    def test_every_light_combination_survives_saving_and_loading(self):
        cleaned = clean_devices({"lights": self.ENTRIES})
        parsed = parse_devices(render_device_file(cleaned))
        self.assertEqual(parsed, cleaned)
        context = Recorder().context()
        for entry in parsed["lights"]:
            with self.subTest(where=entry["where"]):
                config = Light(context, entry).to_config()
                # Everything the user wrote is still there ...
                for key, value in entry.items():
                    self.assertEqual(config[key], value, key)
                # ... and saving a second time changes nothing more.
                self.assertEqual(Light(context, config).to_config(), config)

    def test_addresses_and_unknown_fields_stay_as_they_are(self):
        cleaned = clean_devices({"lights": self.ENTRIES})
        text = render_device_file(cleaned)
        self.assertIn("'0211'", text)           # quoted: not the number 211, not octal
        self.assertIn("Bauteil 2019", text)
        lamp = Light(Recorder().context(), parse_devices(text)["lights"][-1])
        self.assertEqual(lamp.to_config()["eigene_notiz"], "Bauteil 2019")
        self.assertEqual(lamp.color_modes(), ["hs", "color_temp"])


class TestFrameCheck(unittest.TestCase):
    def test_real_frames_pass(self):
        for frame in ("*1*1*29##", "*#1*29*#1*175*2##", "*#2*05*#11#001#1*50##",
                      "*#4*8*#11*1##", "*#13**16##"):
            self.assertEqual(frame_problem(frame), "", frame)

    def test_nonsense_never_reaches_the_bus(self):
        for text in ("Licht an", "*1*1*29", "1*1*29##", "", "*1*1*29## *1*0*29##",
                     "*1*1*29##*1*0*29##", "*1*1*29###", "*1*a*29##", "*1*1*29 ##",
                     "*1*1*" + "1" * 70 + "##"):
            self.assertTrue(frame_problem(text), repr(text))


class TestCleanDevices(unittest.TestCase):
    def test_entries_keep_every_field_and_addresses_stay_text(self):
        data = {"lights": [{"where": " 04 ", "name": "Büro", "custom_note": "2019"}],
                "unknown_section": [{"where": "1"}]}
        cleaned = clean_devices(data)
        self.assertEqual(cleaned["lights"], [{"where": "04", "name": "Büro",
                                              "custom_note": "2019"}])
        self.assertIn("unknown_section", cleaned)
        self.assertEqual(data["lights"][0]["where"], " 04 ", "the input must stay untouched")

    def test_what_cannot_be_right_is_refused(self):
        for data in ([], {"lights": {"where": "12"}}, {"lights": ["12"]},
                     {"lights": [{"name": "no address"}]}, {"lights": [{"where": 4}]},
                     {"lights": [{"where": "  "}]}, {"lights": [{"where": "12", "name": 5}]}):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    clean_devices(data)


class TestRestartRequest(unittest.TestCase):
    def test_without_a_supervisor_nothing_is_asked(self):
        opener = mock.Mock()
        with self.assertLogs(server_module._LOGGER, "WARNING"):
            self.assertFalse(request_restart(token="", opener=opener))
        opener.assert_not_called()

    def test_the_supervisor_is_asked_to_restart_this_add_on(self):
        opener = mock.MagicMock()
        self.assertTrue(request_restart(token="abc", opener=opener))
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "http://supervisor/addons/self/restart")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer abc")
        self.assertGreater(opener.call_args.kwargs["timeout"], 0)

    def test_a_restart_that_cuts_the_answer_short_is_no_error(self):
        opener = mock.Mock(side_effect=urllib.error.URLError("connection reset"))
        with self.assertNoLogs(server_module._LOGGER, "WARNING"):
            self.assertFalse(request_restart(token="abc", opener=opener))

    def test_a_refusal_of_the_supervisor_is_a_warning_and_closed(self):
        error = urllib.error.HTTPError("http://supervisor/addons/self/restart", 403,
                                       "Forbidden", None, None)
        error.close = mock.Mock(wraps=error.close)
        opener = mock.Mock(side_effect=error)
        with self.assertLogs(server_module._LOGGER, "WARNING"):
            self.assertFalse(request_restart(token="abc", opener=opener))
        error.close.assert_called_once()


NO_REVISION = "no revision"


class FakeBridge:
    """Records what the web interface asks for."""

    def __init__(self) -> None:
        self.saved: list[dict] = []
        # The revision each save came with; NO_REVISION for the plain form.
        self.revisions: list[object] = []
        self.sent: list[str] = []
        self.scans: list[bool] = []
        self.restarts = 0
        self.purges = 0
        self.purge_result: int | None = 3
        self.save_error: BaseException | None = None
        self.status_error: BaseException | None = None

    def handlers(self) -> WebHandlers:
        return WebHandlers(status=self.status, devices=lambda: {"lights": [{"where": "12"}]},
                           save_devices=self.save_devices,
                           frames=lambda: [{"frame": "*1*1*12##", "hint": "lights 12",
                                            "time": "12:00:00", "count": 1}],
                           send_frame=self.send_frame, scan=self.scans.append,
                           restart=self.restart, purge=self.purge)

    def status(self) -> dict:
        if self.status_error is not None:
            raise self.status_error
        return {"connected": True, "refused": 2, "unanswered": 1}

    async def save_devices(self, sections: dict, revision: object = NO_REVISION) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(sections)
        self.revisions.append(revision)

    async def purge(self) -> int | None:
        self.purges += 1
        return self.purge_result

    async def send_frame(self, frame: str) -> str:
        self.sent.append(frame)
        return ""

    def restart(self) -> None:
        self.restarts += 1


class ServerTestCase(unittest.IsolatedAsyncioTestCase):
    allowed_peers: tuple[str, ...] | None = None

    async def asyncSetUp(self) -> None:
        self.bridge = FakeBridge()
        self.server = WebServer(self.bridge.handlers(), port=0, host="127.0.0.1",
                                allowed_peers=self.allowed_peers)
        await self.server.start()
        self.port = self.server.port

    async def asyncTearDown(self) -> None:
        await self.server.stop()


class TestPageAndRoutes(ServerTestCase):
    async def test_serves_a_self_contained_page(self):
        status, body = await http(self.port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("<title>MyHome SCS Bridge</title>", body)
        # Ingress rewrites the path, so every URL has to be relative.
        for absolute in ('href="/', 'src="/', "//cdn", "http://", "https://", "fetch('/"):
            self.assertNotIn(absolute, body)

    async def test_the_page_carries_no_lettering_and_no_image(self):
        _status, body = await http(self.port, "GET", "/")
        self.assertNotIn("body::before", body)
        self.assertNotIn("<img", body)

    async def test_the_page_escapes_what_it_shows(self):
        _status, body = await http(self.port, "GET", "/")
        self.assertIn("const esc", body)
        self.assertIn("esc(x.frame)", body)
        self.assertIn("esc(x.hint", body)

    async def test_the_page_restarts_only_after_a_confirmed_save(self):
        # Shipped with the old page: a failed save still said "saved" and
        # restarted the add-on.
        _status, body = await http(self.port, "GET", "/")
        save = body[body.index("async function save"):body.index("async function scan")]
        self.assertLess(save.index("if (!r.ok)"), save.index("api/restart"))

    async def test_ingress_prefix_and_api_routes(self):
        status, body = await http(self.port, "GET", "/api/hassio_ingress/token123/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"connected": True, "refused": 2, "unanswered": 1})
        status, body = await http(self.port, "GET", "/api/schema")
        self.assertEqual(json.loads(body), schema_module.SCHEMA)
        status, body = await http(self.port, "GET", "/api/frames")
        self.assertEqual(json.loads(body)["frames"][0]["frame"], "*1*1*12##")
        status, body = await http(self.port, "GET", "/api/devices")
        self.assertEqual(json.loads(body), {"lights": [{"where": "12"}]})
        # Any other path is the page, a wrong API path stays an error.
        self.assertEqual((await http(self.port, "GET", "/?reload=1"))[0], 200)
        self.assertEqual((await http(self.port, "GET", "/index.html"))[0], 200)
        self.assertEqual((await http(self.port, "GET", "/api/nope"))[0], 404)
        self.assertEqual((await http(self.port, "PUT", "/api/status"))[0], 405)

    async def test_saving_devices(self):
        payload = {"lights": [{"where": "12", "name": "Küche", "dimmer": True}]}
        status, body = await http(self.port, "POST", "/api/devices", payload)
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertEqual(self.bridge.saved, [payload])
        self.assertEqual(self.bridge.revisions, [NO_REVISION])

    async def test_saving_devices_together_with_the_revision_of_the_list(self):
        # What the page sends: the list and the state it was loaded at.
        devices = {"lights": [{"where": "12", "name": "Küche"}]}
        status, body = await http(self.port, "POST", "/api/devices",
                                  {"revision": 7, "devices": devices})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertEqual((self.bridge.saved, self.bridge.revisions), ([devices], [7]))
        # Without a number there is nothing to compare - saved like the plain form.
        await http(self.port, "POST", "/api/devices", {"devices": devices})
        self.assertEqual(self.bridge.revisions, [7, NO_REVISION])
        # The checks of the list apply to this form as well.
        status, _body = await http(self.port, "POST", "/api/devices",
                                   {"revision": 7, "devices": {"lights": [{"where": 4}]}})
        self.assertEqual(status, 400)
        status, _body = await http(self.port, "POST", "/api/devices",
                                   {"revision": "7", "devices": devices})
        self.assertEqual(status, 400)
        self.assertEqual(len(self.bridge.saved), 2)

    async def test_a_list_that_is_out_of_date_answers_409(self):
        self.bridge.save_error = server_module.WebError("409 Conflict", "inzwischen geändert")
        status, body = await http(self.port, "POST", "/api/devices",
                                  {"revision": 7, "devices": {"lights": [{"where": "12"}]}})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"ok": False, "error": "inzwischen geändert"})

    async def test_confirming_a_clean_up(self):
        status, body = await http(self.port, "POST", "/api/purge", {})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True, "removed": 3}))
        self.bridge.purge_result = None
        status, body = await http(self.port, "POST", "/api/purge")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["ok"])
        self.assertEqual(self.bridge.purges, 2)
        self.assertEqual((await http(self.port, "GET", "/api/purge"))[0], 405)

    async def test_the_page_brings_its_list_up_to_date_before_it_saves(self):
        _status, body = await http(self.port, "GET", "/")
        save = body[body.index("async function save"):body.index("async function scan")]
        # The list goes out with the state it was loaded at ...
        self.assertIn("revision: REVISION", save)
        # ... a refusal is handled before anything counts as saved ...
        self.assertLess(save.index("r.status === 409"), save.index("dirty = false"))
        self.assertLess(save.index("r.status === 409"), save.index("api/restart"))
        # ... and the page notices by itself when the add-on's list moved on.
        self.assertIn("s.revision !== REVISION", body)
        # Rows removed here are not brought back, changed ones not doubled.
        merge = body[body.index("function merge"):body.index("async function syncDevices")]
        self.assertIn("REMOVED.has", merge)
        self.assertIn("ORIGIN.get", merge)

    async def test_the_page_shows_what_needs_a_decision(self):
        _status, body = await http(self.port, "GET", "/")
        notes = body[body.index("function notes"):body.index("async function sendFrame")]
        for field in ("s.problems", "s.purge_held", "s.restart_pending"):
            self.assertIn(field, notes)
        # Whatever comes from the add-on goes in as text, never as markup.
        self.assertNotIn("innerHTML = s.", notes)
        self.assertNotIn("innerHTML +=", notes)
        # Removing devices from Home Assistant takes a second click.
        self.assertIn("dataset.armed", notes)
        self.assertIn("api/purge", notes)
        self.assertIn("s.mqtt_connected", body)

    async def test_an_empty_or_broken_request_never_empties_the_device_file(self):
        for raw in (b"", b"   ", b"{not json", b"[1, 2]", b"\xff\xfe"):
            with self.subTest(raw=raw):
                status, body = await http(self.port, "POST", "/api/devices", raw_body=raw)
                self.assertEqual(status, 400)
                self.assertFalse(json.loads(body)["ok"])
        status, body = await http(self.port, "POST", "/api/devices", {"lights": [{"where": 4}]})
        self.assertEqual(status, 400)
        self.assertIn("Adresse", json.loads(body)["error"])
        self.assertEqual(self.bridge.saved, [])

    async def test_a_failed_write_is_reported(self):
        self.bridge.save_error = OSError("disk full")
        with self.assertLogs(server_module._LOGGER, "ERROR"):
            status, body = await http(self.port, "POST", "/api/devices",
                                      {"lights": [{"where": "12"}]})
        self.assertEqual(status, 500)
        self.assertIn("Gerätedatei", json.loads(body)["error"])

    async def test_send_scan_and_restart(self):
        status, body = await http(self.port, "POST", "/api/send", {"frame": "*1*1*29##"})
        self.assertEqual(json.loads(body), {"ok": True, "error": ""})
        self.assertEqual(self.bridge.sent, ["*1*1*29##"])
        self.assertEqual((await http(self.port, "POST", "/api/send", {"frame": 12}))[0], 400)
        await http(self.port, "POST", "/api/scan", {"deep": True})
        await http(self.port, "POST", "/api/scan")
        await http(self.port, "POST", "/api/scan", {"deep": "yes"})
        self.assertEqual(self.bridge.scans, [True, False, False])
        status, _body = await http(self.port, "POST", "/api/restart")
        self.assertEqual((status, self.bridge.restarts), (200, 1))

    async def test_oversized_and_chunked_requests_are_refused(self):
        status, _body = await http(self.port, "POST", "/api/devices", raw_body=b"",
                                   headers={"Content-Length": str(MAX_BODY + 1)})
        self.assertEqual(status, 413)
        status, _body = await http(self.port, "POST", "/api/devices", raw_body=b"",
                                   headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 411)
        status, _body = await http(self.port, "GET", "/api/status",
                                   headers={"X-Filler": "x" * (server_module.MAX_HEAD + 10)})
        self.assertEqual(status, 431)

    async def test_a_silent_client_is_dropped(self):
        with mock.patch.object(server_module, "REQUEST_TIMEOUT", 0.2):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            writer.write(b"GET /api/status HTTP/1.1\r\n")        # never finishes
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 5), b"")
            writer.close()

    async def test_stop_does_not_wait_for_a_hanging_browser(self):
        # The Supervisor kills the add-on ten seconds after SIGTERM; a request
        # that never finishes must not eat that time.
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(b"GET /api/status HTTP/1.1\r\n")        # never finishes
            await writer.drain()
            await asyncio.sleep(0.05)
            loop = asyncio.get_running_loop()
            started = loop.time()
            with mock.patch.object(server_module, "STOP_WAIT", 0.3):
                await self.server.stop()
            self.assertLess(loop.time() - started, 1.5)
        finally:
            writer.close()
        self.assertLessEqual(server_module.STOP_WAIT, 2.0)

    async def test_a_broken_handler_answers_500_and_the_server_lives_on(self):
        self.bridge.status_error = RuntimeError("bug")
        with self.assertLogs(server_module._LOGGER, "ERROR"):
            status, _body = await http(self.port, "GET", "/api/status")
        self.assertEqual(status, 500)
        self.bridge.status_error = None
        self.assertEqual((await http(self.port, "GET", "/api/status"))[0], 200)

    async def test_a_lasting_bug_does_not_flood_the_log(self):
        # The page asks every three seconds - one traceback is enough.
        self.bridge.status_error = RuntimeError("bug")
        with self.assertLogs(server_module._LOGGER, "DEBUG") as logs:
            for _ in range(4):
                self.assertEqual((await http(self.port, "GET", "/api/status"))[0], 500)
        loud = [r for r in logs.records if r.levelno >= logging.ERROR]
        self.assertEqual(len(loud), 1, [r.getMessage() for r in loud])
        self.assertIsNotNone(loud[0].exc_info)


class TestOnlyIngressMayConnect(ServerTestCase):
    allowed_peers = ("172.30.32.2",)

    async def test_everybody_else_is_turned_away(self):
        with self.assertLogs(server_module._LOGGER, "WARNING") as logs:
            status, _body = await http(self.port, "GET", "/api/status")
            await http(self.port, "GET", "/api/status")
        self.assertEqual(status, 403)
        self.assertIn("127.0.0.1", logs.records[0].getMessage())
        self.assertEqual(len([r for r in logs.records if r.levelno >= logging.WARNING]), 1)

    def test_the_default_allows_the_ingress_proxy(self):
        self.assertIn("172.30.32.2", server_module.DEFAULT_ALLOWED_PEERS)


class TestBusMonitor(unittest.TestCase):
    def make(self, known=(("lights", "12"),), size: int = 200) -> BusMonitor:
        return BusMonitor(lambda section, where: (section, where) in known, size=size,
                          now=lambda: "12:00:00")

    def test_the_monitor_stays_readable(self):
        monitor = self.make()
        for _ in range(5):
            monitor.record(msg.parse("*#1*1010*7*0*0*2##"))
        monitor.record(msg.parse("*#13**0*21*11*44*999##"))         # the gateway clock
        monitor.record(msg.parse(msg.ACK))
        monitor.record(msg.parse("*1*1*12##"))
        monitor.record(msg.parse("*1*1*33##"))
        frames = monitor.frames()
        self.assertEqual([f["frame"] for f in frames],
                         ["*#1*1010*7*0*0*2##", "*1*1*12##", "*1*1*33##"])
        self.assertEqual(frames[0]["count"], 5)
        self.assertEqual(frames[1]["hint"], "lights 12")
        self.assertEqual(frames[2]["hint"], "lights 33  (neu)")
        self.assertEqual(frames[2]["time"], "12:00:00")

    def test_the_monitor_is_bounded_and_hands_out_copies(self):
        monitor = self.make(size=3)
        for where in range(10, 20):
            monitor.record(msg.parse(f"*1*1*{where}##"))
        frames = monitor.frames()
        self.assertEqual([f["frame"] for f in frames], ["*1*1*17##", "*1*1*18##", "*1*1*19##"])
        frames[0]["count"] = 99
        self.assertEqual(monitor.frames()[0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
