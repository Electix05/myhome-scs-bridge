"""Energy meters and the diagnostic entities of the add-on."""

from __future__ import annotations

import unittest

from myhome_scs_mqtt.app import __version__
from myhome_scs_mqtt.app.devices.sensors import COUNTER_INTERVAL, BridgeStatus, EnergyMeter

from ..helpers import Recorder, frame


class TestEnergyMeter(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = Recorder()
        self.meter = EnergyMeter(self.rec.context(), {"where": "51", "energy_today": True})

    def test_values(self):
        self.meter.on_own(frame("*#18*51*113*0000850##"))
        self.meter.on_own(frame("*#18*51*51*1234567##"))
        self.meter.on_own(frame("*#18*51*54*4200##"))
        self.assertEqual(self.rec.last("myhome/energy/51/power"), "850")
        self.assertEqual(self.rec.last("myhome/energy/51/energy"), "1234.567")
        self.assertEqual(self.rec.last("myhome/energy/51/energy_today"), "4.200")

    def test_pushed_power_updates(self):
        self.meter.on_own(frame("*#18*51*1200#1*120##"))
        self.assertEqual(self.rec.last("myhome/energy/51/power"), "120")

    def test_garbage_is_ignored(self):
        for raw in ("*#18*51*113*x##", "*#18*51*113*--1##", "*#18*51*99*5##", "*18*1*51##"):
            self.meter.on_own(frame(raw))
        self.assertEqual(self.rec.published, [])

    def test_poll_frames_rearm_the_push_updates(self):
        self.assertEqual(self.meter.poll_frames(),
                         ("*#18*51*#1200#1*30##", "*#18*51*113##", "*#18*51*51##",
                          "*#18*51*54##"))

    def test_discovery(self):
        ids = [oid for _c, oid, _p in self.meter.discovery()]
        self.assertEqual(ids, ["scs_bridge_18_51_power", "scs_bridge_18_51_energy",
                               "scs_bridge_18_51_energy_today"])


class TestBridgeStatus(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = Recorder()
        self.status = BridgeStatus(self.rec.context(),
                                   {"where": "192.0.2.10", "name": "MyHome SCS Bridge"})

    def test_diagnostics_stay_visible_while_the_link_is_down(self):
        for _component, _oid, payload in self.status.discovery():
            self.assertEqual(payload["availability_topic"], "myhome/bridge/lwt")
            self.assertNotIn("availability", payload)
            self.assertEqual(payload["device"]["identifiers"], ["scs_bridge"])
            self.assertEqual(payload["device"]["sw_version"], __version__)
            self.assertEqual(payload["device"]["name"], "MyHome SCS Bridge")

    def test_scan_buttons(self):
        calls = []
        self.status.on_scan = calls.append
        self.assertEqual(self.status.subscriptions(), ("myhome/bridge/scan/set",))
        self.status.on_command("myhome/bridge/scan/set", "PRESS")
        self.status.on_command("myhome/bridge/scan/set", "DEEP")
        self.assertEqual(calls, [False, True])
        buttons = {oid: payload for component, oid, payload in self.status.discovery()
                   if component == "button"}
        self.assertEqual(buttons["scs_bridge_scan_button"]["payload_press"], "PRESS")
        self.assertEqual(buttons["scs_bridge_scan_deep_button"]["payload_press"], "DEEP")

    def test_link_and_scan_state(self):
        self.status.set_connected(True)
        self.assertEqual(self.rec.last("myhome/bridge/link"), "connected")
        self.status.set_scan_state("läuft: " + "x" * 400)
        self.assertEqual(len(self.rec.last("myhome/bridge/scan")), 255)

    def test_the_last_frame_stays_out_of_home_assistant(self):
        # Every frame as an entity state buried the Home Assistant logbook.
        self.status.set_last_frame("*1*1*12##")
        self.assertEqual(self.status.last_frame, "*1*1*12##")
        self.assertEqual(self.rec.published, [])
        ids = [oid for _c, oid, _p in self.status.discovery()]
        self.assertNotIn("scs_bridge_last_frame", ids)

    def test_bus_counters_are_diagnostic_sensors(self):
        # Decided on 17.09.2026: refusals and silences stay visible for good.
        self.status.read_counters = lambda: {"refused": 4, "unanswered": 1}
        sensors = {oid: payload for component, oid, payload in self.status.discovery()
                   if component == "sensor"}
        for name, topic in (("refused", "myhome/bridge/refused"),
                            ("unanswered", "myhome/bridge/unanswered")):
            payload = sensors["scs_bridge_" + name]
            self.assertEqual(payload["state_topic"], topic)
            self.assertEqual(payload["entity_category"], "diagnostic")
            self.assertEqual(payload["state_class"], "total_increasing")
            self.assertEqual(payload["availability_topic"], "myhome/bridge/lwt")
        self.status.publish_state()
        self.assertEqual(self.rec.last("myhome/bridge/refused"), "4")
        self.assertEqual(self.rec.last("myhome/bridge/unanswered"), "1")

    def test_counters_go_out_on_change_but_not_more_often_than_every_few_seconds(self):
        counters = {"refused": 0, "unanswered": 0}
        self.status.read_counters = lambda: dict(counters)
        self.assertTrue(self.status.needs_tick)
        self.status.tick()
        self.assertEqual(self.rec.last("myhome/bridge/refused"), "0")
        self.rec.clear()
        counters["refused"] = 3
        self.rec.advance(1.0)
        self.status.tick()                         # too soon after the last look
        self.assertEqual(self.rec.published, [])
        self.rec.advance(COUNTER_INTERVAL)
        self.status.tick()
        self.assertEqual(self.rec.published, [("myhome/bridge/refused", "3", True)])
        self.rec.advance(COUNTER_INTERVAL)
        self.status.tick()                         # nothing changed
        self.assertEqual(len(self.rec.published), 1)

    def test_without_a_bridge_the_counters_read_zero(self):
        self.status.publish_state()
        self.assertEqual(self.rec.last("myhome/bridge/refused"), "0")


if __name__ == "__main__":
    unittest.main()
