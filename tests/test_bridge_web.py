"""The web interface wired to a running bridge: fake gateway in, real HTTP out."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from unittest import mock

from myhome_scs_mqtt.app import __version__
from myhome_scs_mqtt.app.bridge import bridge as bridge_module
from myhome_scs_mqtt.app.settings import load_devices
from myhome_scs_mqtt.app.web import server as server_module

from .helpers import wait_until
from .test_bridge import BridgeTestCase
from .test_web import http


class WebBridgeTestCase(BridgeTestCase):
    async def asyncSetUp(self) -> None:
        self.restarts: list[str] = []
        await super().asyncSetUp()
        self.bridge._request_restart = lambda: self.restarts.append("asked") or True
        self.assertTrue(await wait_until(lambda: self.bridge.web.port != 0))
        self.port = self.bridge.web.port


class TestStatusAndDevices(WebBridgeTestCase):
    async def test_the_status_shows_link_mqtt_and_bus_counters(self):
        status, body = await http(self.port, "GET", "/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["connected"])
        self.assertEqual(data["version"], __version__)
        self.assertEqual(data["devices"], 5)
        self.assertEqual(data["host"], f"127.0.0.1:{self.settings.gateway.port}")
        for key in ("sent", "received", "sent_per_minute", "received_per_minute", "queue",
                    "throttled", "refused", "unanswered", "brake_gap", "dropped",
                    "mqtt_broker", "mqtt_connected", "subscriptions", "scan"):
            self.assertIn(key, data)

    async def test_lists_configured_and_discovered_devices(self):
        await self.fake.emit("*2*1*77##")                       # a shutter shows up
        self.assertTrue(await wait_until(lambda: self.bridge.registry.known("covers", "77")))
        _status, body = await http(self.port, "GET", "/api/devices")
        data = json.loads(body)
        self.assertEqual([item["where"] for item in data["lights"]], ["12"])
        self.assertEqual([item["where"] for item in data["covers"]], ["21", "77"])

    async def test_saving_writes_the_device_file_and_forgets_the_discovery_memory(self):
        await self.fake.emit("*2*1*77##")
        self.assertTrue(await wait_until(lambda: self.bridge.registry.discovered != []))
        payload = {
            "covers": [{"where": "0210", "name": "Rollo", "tilt_time": 1.0}],
            "lights": [{"where": "04", "name": "Küche neu", "dimmer": True,
                        "custom_note": "bleibt erhalten"}],
        }
        status, body = await http(self.port, "POST", "/api/devices", payload)
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        written = await asyncio.to_thread(load_devices, self.devices_file)
        self.assertTrue(written.complete, written.problems)
        # Addresses survive as text - "04" is not the number 4, "0210" no octal.
        self.assertEqual(written.sections["lights"][0]["where"], "04")
        self.assertEqual(written.sections["covers"][0]["where"], "0210")
        self.assertEqual(written.sections["lights"][0]["custom_note"], "bleibt erhalten")
        self.assertEqual(list(written.sections), ["lights", "covers"])    # known order
        self.assertEqual(self.bridge.registry.discovered, [])
        self.assertTrue(await wait_until(lambda: self.discovered_file() == []))


class TestSavingFromAListThatIsOutOfDate(WebBridgeTestCase):
    """The page shows a copy of the device list - and copies grow old.

    Saving writes the whole device file anew and empties the discovery memory.
    A device the add-on found after the page had loaded its list is in neither
    of them afterwards: with the next start it is gone from Home Assistant.
    """

    async def revision(self) -> int:
        _status, body = await http(self.port, "GET", "/api/status")
        return json.loads(body)["revision"]

    def device_file(self) -> str:
        with open(self.devices_file, encoding="utf-8") as handle:
            return handle.read()

    async def test_the_status_carries_what_the_page_has_to_know(self):
        _status, body = await http(self.port, "GET", "/api/status")
        data = json.loads(body)
        self.assertIsInstance(data["revision"], int)
        self.assertEqual((data["problems"], data["purge_held"], data["restart_pending"]),
                         ([], None, False))

    async def test_a_save_from_an_old_list_is_refused_and_changes_nothing(self):
        loaded_at = await self.revision()
        before = self.device_file()
        await self.fake.emit("*2*1*77##")                       # found after the page loaded
        self.assertTrue(await wait_until(lambda: self.bridge.registry.discovered != []))
        status, body = await http(self.port, "POST", "/api/devices", {
            "revision": loaded_at, "devices": {"lights": [{"where": "12", "name": "Küche"}]}})
        self.assertEqual(status, 409)
        answer = json.loads(body)
        self.assertFalse(answer["ok"])
        self.assertIn("geändert", answer["error"])
        self.assertEqual(self.device_file(), before)
        self.assertEqual(self.bridge.registry.discovered, [{"section": "covers", "where": "77"}])

    async def test_a_save_from_the_current_list_goes_through(self):
        await self.fake.emit("*2*1*77##")
        self.assertTrue(await wait_until(lambda: self.bridge.registry.discovered != []))
        current = await self.revision()
        status, body = await http(self.port, "POST", "/api/devices", {
            "revision": current, "devices": {"covers": [{"where": "77", "name": "Rollo"}]}})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertIn("Rollo", self.device_file())
        self.assertEqual(self.bridge.registry.discovered, [])
        # A second tab with the list from before is out of date now as well.
        self.assertNotEqual(await self.revision(), current)

    async def test_a_revision_that_is_no_number_is_refused(self):
        for revision in ("7", 1.5, True, [1]):
            with self.subTest(revision=revision):
                status, _body = await http(self.port, "POST", "/api/devices", {
                    "revision": revision, "devices": {"lights": [{"where": "12"}]}})
                self.assertEqual(status, 400)

    async def test_a_device_found_while_the_file_is_written_stays_remembered(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        real_write = bridge_module.write_device_file

        def slow_write(*args):
            started.set()
            release.wait(3.0)
            return real_write(*args)

        with mock.patch.object(bridge_module, "write_device_file", slow_write):
            saving = asyncio.create_task(http(self.port, "POST", "/api/devices", {
                "revision": await self.revision(),
                "devices": {"lights": [{"where": "12", "name": "Küche"}]}}))
            self.assertTrue(await wait_until(started.is_set))
            await self.fake.emit("*2*1*78##")                   # while the disk is busy
            self.assertTrue(await wait_until(lambda: self.bridge.registry.known("covers", "78")))
            release.set()
            status, _body = await saving
        self.assertEqual(status, 200)
        self.assertEqual(self.bridge.registry.discovered, [{"section": "covers", "where": "78"}])
        self.assertTrue(await wait_until(
            lambda: self.discovered_file() == [{"section": "covers", "where": "78"}]))

    async def test_after_a_save_the_page_gets_back_what_it_saved(self):
        # "Save only": the running entities still are the old list.  Handing
        # those out again would make the next save write the old list back.
        saved = {"lights": [{"where": "12", "name": "Küche neu"}],
                 "covers": [{"where": "21", "name": "Rollo"}]}
        status, _body = await http(self.port, "POST", "/api/devices", saved)
        self.assertEqual(status, 200)
        _status, body = await http(self.port, "GET", "/api/devices")
        self.assertEqual(json.loads(body), saved)
        _status, body = await http(self.port, "GET", "/api/status")
        self.assertTrue(json.loads(body)["restart_pending"])
        # What shows up on the bus afterwards joins the saved list.
        await self.fake.emit("*2*1*77##")
        self.assertTrue(await wait_until(lambda: self.bridge.registry.known("covers", "77")))
        _status, body = await http(self.port, "GET", "/api/devices")
        data = json.loads(body)
        self.assertEqual([item["where"] for item in data["covers"]], ["21", "77"])
        self.assertEqual(data["lights"], saved["lights"])
        self.assertNotIn("climate", data)                       # removed stays removed

    async def test_the_replaced_device_file_is_kept_next_to_it(self):
        before = self.device_file()
        status, _body = await http(self.port, "POST", "/api/devices",
                                   {"lights": [{"where": "12", "name": "Küche"}]})
        self.assertEqual(status, 200)
        for suffix in (".bak", ".manual.bak"):
            with open(self.devices_file + suffix, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), before, suffix)


class TestSavingWhileTheDeviceFileIsUnreadable(WebBridgeTestCase):
    """The page only shows what the bridge could read.

    Its list therefore lacks the lamp under the mistyped section.  Written to
    the device file, that list would finish what the typo began: the lamp is
    gone from the file, the next start finds a complete list and removes the
    lamp from Home Assistant.
    """

    devices = 'ligths:\n  - where: "12"\n    name: Kitchen\ncovers:\n  - where: "21"\n'

    async def asyncSetUp(self) -> None:
        with self.assertLogs("myhome_scs_mqtt.app", "WARNING"):
            await super().asyncSetUp()

    async def test_saving_is_refused_and_the_file_stays_as_it_is(self):
        _status, body = await http(self.port, "GET", "/api/status")
        self.assertTrue(json.loads(body)["saving_blocked"])
        _status, body = await http(self.port, "GET", "/api/devices")
        self.assertEqual(list(json.loads(body)), ["covers"])    # the lamp is not in the list
        for payload in ({"covers": [{"where": "21", "name": "Rollo"}]},
                        {"revision": self.bridge.revision,
                         "devices": {"covers": [{"where": "21", "name": "Rollo"}]}}):
            status, body = await http(self.port, "POST", "/api/devices", payload)
            self.assertEqual(status, 423)
            self.assertIn("nicht vollständig gelesen", json.loads(body)["error"])
        with open(self.devices_file, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self.devices)
        self.assertEqual(os.listdir(self.dir), ["devices.yaml"])

    async def test_the_page_locks_its_save_buttons(self):
        _status, body = await http(self.port, "GET", "/")
        self.assertIn("b.disabled = !!s.saving_blocked", body)
        self.assertEqual(body.count('class="primary save"') + body.count('class="save"'), 2)


class TestAnUnreadableMemoryDoesNotLockThePage(WebBridgeTestCase):
    """What the bridge once found and cannot read any more is not in a file
    anybody could correct - saving from the page is the way out."""

    async def asyncSetUp(self) -> None:
        memory = [{"section": "covers", "where": "33", "travel_time": "fast"}]
        with mock.patch.object(bridge_module, "load_discovered",
                               lambda _path: (list(memory), True)):
            with self.assertLogs("myhome_scs_mqtt.app", "WARNING"):
                await super().asyncSetUp()

    async def test_nothing_is_removed_but_saving_works(self):
        status = self.bridge.web_status()
        self.assertEqual(len(status["problems"]), 1)
        self.assertFalse(status["saving_blocked"])
        self.assertEqual(self.collect_calls, [])                # no clean-up meanwhile
        code, _body = await http(self.port, "POST", "/api/devices",
                                 {"lights": [{"where": "12", "name": "Küche"}]})
        self.assertEqual(code, 200)
        self.assertEqual(self.bridge.registry.discovered, [])


class TestConfirmingTheCleanUp(WebBridgeTestCase):
    async def test_the_page_can_confirm_a_held_clean_up(self):
        self.leftovers = [f"homeassistant/light/scs_bridge/scs_bridge_1_{where}/config"
                          for where in range(50, 60)]
        with self.assertLogs("myhome_scs_mqtt.app", "WARNING"):
            await self.bridge.discovery.purge_stale()           # what the start does
        _status, body = await http(self.port, "GET", "/api/status")
        self.assertEqual(json.loads(body)["purge_held"]["entities"], 10)
        self.assertFalse([t for t, payload, _r in self.mqtt.published if payload == ""])

        with self.assertLogs("myhome_scs_mqtt.app", "WARNING"):
            status, body = await http(self.port, "POST", "/api/purge", {})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True, "removed": 10}))
        self.assertEqual(len([t for t, payload, _r in self.mqtt.published if payload == ""]), 10)
        _status, body = await http(self.port, "GET", "/api/status")
        self.assertIsNone(json.loads(body)["purge_held"])

    async def test_a_broker_that_cannot_be_asked_removes_nothing(self):
        self.bridge.discovery._collect = lambda *_args: None
        with self.assertLogs("myhome_scs_mqtt.app", "WARNING"):
            status, body = await http(self.port, "POST", "/api/purge", {})
        self.assertEqual(status, 200)
        answer = json.loads(body)
        self.assertFalse(answer["ok"])
        self.assertIn("nichts entfernt", answer["error"])
        self.assertFalse([t for t, payload, _r in self.mqtt.published if payload == ""])

    async def test_only_a_post_confirms(self):
        status, _body = await http(self.port, "GET", "/api/purge")
        self.assertEqual(status, 405)


class TestFramesByHand(WebBridgeTestCase):
    async def test_a_hand_written_frame_reaches_the_bus(self):
        status, body = await http(self.port, "POST", "/api/send", {"frame": " *1*1*29## "})
        self.assertEqual((status, json.loads(body)["error"]), (200, ""))
        self.assertIn("*1*1*29##", self.fake.received)

    async def test_a_refused_frame_says_so_and_does_not_brake(self):
        # Trying frames by hand is an experiment, not overload.
        self.fake.nack_frames.add("*1*1*77##")
        for _ in range(4):
            _status, body = await http(self.port, "POST", "/api/send", {"frame": "*1*1*77##"})
            self.assertIn("abgelehnt", json.loads(body)["error"])
        self.assertEqual((self.bridge.gateway.refused, self.bridge.gateway.brake_gap), (0, 0.0))

    async def test_nonsense_is_not_put_on_the_bus(self):
        for text in ("Licht an", "*1*1*29", "1*1*29##", "", "*1*1*29## *1*0*29##",
                     "*1*1*29##*1*0*29##"):
            _status, body = await http(self.port, "POST", "/api/send", {"frame": text})
            self.assertTrue(json.loads(body)["error"], f"{text!r} went through unchecked")
        await asyncio.sleep(0.3)
        self.assertFalse([f for f in self.fake.received if f.startswith("*1*1*29")])


class TestMonitorScanRestart(WebBridgeTestCase):
    async def test_the_monitor_shows_what_happens_on_the_bus(self):
        await self.fake.emit("*1*1*12##")
        await self.fake.emit("*1*1*33##")
        self.assertTrue(await wait_until(
            lambda: any(f["frame"] == "*1*1*33##" for f in self.bridge.monitor.frames())))
        _status, body = await http(self.port, "GET", "/api/frames")
        hints = {f["frame"]: f["hint"] for f in json.loads(body)["frames"]}
        self.assertEqual(hints["*1*1*12##"], "lights 12")
        self.assertEqual(hints["*1*1*33##"], "lights 33  (neu)")

    async def test_the_scan_can_be_started(self):
        status, _body = await http(self.port, "POST", "/api/scan", {"deep": False})
        self.assertEqual(status, 200)
        self.assertTrue(await wait_until(
            lambda: self.bridge.status.scan_state.startswith(("läuft", "fertig"))))

    async def test_restart_is_asked_for_once_the_page_has_its_answer(self):
        status, body = await http(self.port, "POST", "/api/restart")
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertTrue(await wait_until(lambda: self.restarts == ["asked"]))

    async def test_a_double_click_asks_only_once(self):
        self.bridge.schedule_restart()
        self.bridge.schedule_restart()
        self.assertTrue(await wait_until(lambda: self.restarts))
        await asyncio.sleep(bridge_module.RESTART_DELAY + 0.2)
        self.assertEqual(self.restarts, ["asked"])

    async def test_stopping_drops_a_restart_that_is_still_waiting(self):
        with mock.patch.object(bridge_module, "RESTART_DELAY", 3600):
            self.bridge.schedule_restart()
            await asyncio.sleep(0.05)
            await self.bridge.stop()
        self.assertEqual(self.restarts, [])

    async def test_stop_closes_the_web_interface(self):
        await self.bridge.stop()
        with self.assertRaises(OSError):
            await http(self.port, "GET", "/api/status")

    async def test_a_hanging_browser_does_not_delay_the_goodbye_to_mqtt(self):
        # After SIGTERM the add-on may be killed within seconds.
        _reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(b"GET /api/status HTTP/1.1\r\n")      # never finishes
            await writer.drain()
            with mock.patch.object(server_module, "STOP_WAIT", 1.0):
                stopping = asyncio.create_task(self.bridge.stop())
                self.assertTrue(await wait_until(lambda: self.mqtt.stopped, 0.5),
                                "MQTT waited for the web interface")
                self.assertFalse(stopping.done())
                await stopping
            with self.assertRaises(OSError):
                await http(self.port, "GET", "/api/status")     # no new requests
        finally:
            writer.close()


class TestNothingBlocksTheBus(WebBridgeTestCase):
    """The two web bugs of the old add-on (berichte/12-rueckfrage-webfehler.md).

    Writing the device file and asking the Supervisor for a restart ran on the
    event loop: while the disk or the Supervisor took its time, the whole
    bridge stood still - bus frames, keepalive, the page itself.  Both run in a
    worker thread now; the bus must be heard while that thread is still busy.
    """

    def slow(self, work=lambda *_args: True, wait: float = 3.0):
        """A stand-in that blocks its thread until released: (function, started, done).

        Run on the event loop instead of a worker thread, it would hold the
        loop for its full wait - and ``done`` would be set before the bus was
        heard, which fails the test (see the counter-check below).
        """
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        # Never leave a worker thread waiting, whatever the test did.
        self.addCleanup(release.set)

        def function(*args):
            started.set()
            release.wait(wait)
            done.set()
            return work(*args)

        function.release = release.set
        return function, started, done

    async def assert_the_bus_is_heard_meanwhile(self, done: threading.Event) -> None:
        await self.fake.emit("*1*0*12##")
        self.assertTrue(await wait_until(
            lambda: any(f["frame"] == "*1*0*12##" for f in self.bridge.monitor.frames()), 1.0),
            "a bus frame did not get through while the worker thread was busy")
        status, _body = await http(self.port, "GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(done.is_set(), "the slow part ran on the event loop")

    async def test_a_slow_disk_does_not_stop_the_bus(self):
        slow_write, started, done = self.slow(bridge_module.write_device_file)
        with mock.patch.object(bridge_module, "write_device_file", slow_write):
            saving = asyncio.create_task(
                http(self.port, "POST", "/api/devices", {"lights": [{"where": "12"}]}))
            self.assertTrue(await wait_until(started.is_set))
            await self.assert_the_bus_is_heard_meanwhile(done)
            self.assertFalse(saving.done(), "the page got its answer before the file was written")
            slow_write.release()
            status, body = await saving
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))

    async def test_a_slow_supervisor_does_not_stop_the_bus(self):
        slow_restart, started, done = self.slow()
        self.bridge._request_restart = slow_restart
        with mock.patch.object(bridge_module, "RESTART_DELAY", 0):
            status, _body = await http(self.port, "POST", "/api/restart")
            self.assertEqual(status, 200)
            self.assertTrue(await wait_until(started.is_set))
            await self.assert_the_bus_is_heard_meanwhile(done)
        slow_restart.release()
        self.assertTrue(await wait_until(done.is_set))

    async def test_counter_check_the_old_way_would_be_caught(self):
        # Proof that the check works: the same restart run on the event loop,
        # as in the old add-on, fails it.
        slow_restart, started, done = self.slow(wait=0.3)
        self.bridge._request_restart = slow_restart

        async def on_the_loop(function, *args):
            return function(*args)

        with mock.patch.object(bridge_module, "RESTART_DELAY", 0), \
                mock.patch.object(bridge_module.asyncio, "to_thread", on_the_loop):
            await http(self.port, "POST", "/api/restart")
            self.assertTrue(await wait_until(started.is_set))
            with self.assertRaises(AssertionError):
                await self.assert_the_bus_is_heard_meanwhile(done)
