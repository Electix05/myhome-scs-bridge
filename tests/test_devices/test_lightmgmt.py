"""BMSE light management sensors: lux and held presence."""

from __future__ import annotations

import unittest

from myhome_scs_mqtt.app.devices.lightmgmt import BmseSensor

from ..helpers import Recorder, frame

LUX = "myhome/sensor/1010/illuminance"
MOTION = "myhome/sensor/1010/motion"


class BmseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = Recorder()
        self.ctx = self.rec.context()

    def sensor(self, **config) -> BmseSensor:
        return BmseSensor(self.ctx, dict({"where": "1010", "name": "Hall"}, **config))

    def run_ticks(self, sensor: BmseSensor, seconds: int) -> None:
        for _ in range(seconds):
            self.rec.advance(1.0)
            sensor.tick()


class TestValues(BmseTestCase):
    def test_lux_and_motion_from_wissen(self):
        sensor = self.sensor()
        sensor.on_own(frame("*#1*1010*6*220##"))
        self.assertEqual(self.rec.last(LUX), "220")
        sensor.on_own(frame("*#1*1010*7*1*0*2##"))
        self.assertEqual(self.rec.last(MOTION), "ON")
        self.assertFalse(self.rec.retained(MOTION))
        self.assertTrue(self.rec.retained(LUX))

    def test_light_management_frame_counts_as_detection(self):
        sensor = self.sensor()
        sensor.on_own(frame("*1*34*1010##"))
        self.assertEqual(self.rec.last(MOTION), "ON")

    def test_unchanged_values_are_not_repeated(self):
        sensor = self.sensor()
        for _ in range(3):
            sensor.on_own(frame("*#1*1010*6*220##"))
        self.assertEqual([t for t, _p, _r in self.rec.published].count(LUX), 1)
        sensor.republish()
        self.assertEqual([t for t, _p, _r in self.rec.published].count(LUX), 2)

    def test_discovery(self):
        entities = {oid: (component, payload)
                    for component, oid, payload in self.sensor().discovery()}
        lux = entities["scs_bridge_1_1010_bmse_lux"][1]
        self.assertEqual((lux["device_class"], lux["unit_of_measurement"]), ("illuminance", "lx"))
        component, motion = entities["scs_bridge_1_1010_bmse_motion"]
        self.assertEqual((component, motion["device_class"]), ("binary_sensor", "motion"))
        # The hold time belongs to the add-on, not to Home Assistant.
        self.assertNotIn("off_delay", motion)

    def test_shares_its_address_with_a_light_point(self):
        sensor = self.sensor(where="12")
        self.assertFalse(sensor.exclusive_address)
        self.assertEqual(sensor.object_id, "scs_bridge_1_12_bmse")
        self.assertEqual(sensor.topic_root, "myhome/sensor/12")


class TestHoldTime(BmseTestCase):
    def test_motion_is_held_until_the_detections_stop(self):
        sensor = self.sensor(off_delay=10)
        sensor.on_own(frame("*#1*1010*7*1*0*2##"))
        # A presence sensor drops to zero between two movements.
        for _ in range(3):
            self.run_ticks(sensor, 3)
            sensor.on_own(frame("*#1*1010*7*0*0*2##"))
            self.run_ticks(sensor, 3)
            sensor.on_own(frame("*#1*1010*7*1*0*2##"))
        self.assertEqual(self.rec.last(MOTION), "ON")
        self.assertNotIn("OFF", [p for t, p, _r in self.rec.published if t == MOTION])
        # Only silence releases it.
        self.run_ticks(sensor, 9)
        self.assertEqual(self.rec.last(MOTION), "ON")
        self.run_ticks(sensor, 2)
        self.assertEqual(self.rec.last(MOTION), "OFF")

    def test_without_hold_time_the_sensor_is_followed(self):
        sensor = self.sensor(off_delay=0)
        sensor.on_own(frame("*#1*1010*7*1*0*2##"))
        sensor.on_own(frame("*#1*1010*7*0*0*2##"))
        self.assertEqual(self.rec.last(MOTION), "OFF")


class TestPolling(BmseTestCase):
    def test_lux_slowly_and_presence_only_when_asked(self):
        sensor = self.sensor(lux_interval=30, motion_poll=0)
        self.run_ticks(sensor, 60)
        self.assertEqual(self.rec.sent, ["*#1*1010*6##"] * 2)
        polling = self.sensor(lux_interval=0, motion_poll=5)
        self.rec.clear()
        self.run_ticks(polling, 10)
        self.assertEqual(self.rec.sent, ["*#1*1010*7##"] * 2)

    def test_a_busy_bus_skips_the_polls(self):
        sensor = self.sensor(lux_interval=1, motion_poll=1)
        self.rec.depth = 5
        self.run_ticks(sensor, 5)
        self.assertEqual(self.rec.sent, [])

    def test_the_hold_time_ends_even_on_a_busy_bus(self):
        sensor = self.sensor(off_delay=2)
        sensor.on_own(frame("*#1*1010*7*1*0*2##"))
        self.rec.depth = 5
        self.run_ticks(sensor, 3)
        self.assertEqual(self.rec.last(MOTION), "OFF")

    def test_poll_frames(self):
        self.assertEqual(self.sensor().poll_frames(), ("*#1*1010*6##", "*#1*1010*7##"))
        self.assertEqual(self.sensor(motion=False).poll_frames(), ("*#1*1010*6##",))


if __name__ == "__main__":
    unittest.main()
