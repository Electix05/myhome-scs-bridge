"""WHO=4 heating zones, fan coils and heating actuators."""

from __future__ import annotations

import unittest

from myhome_scs_mqtt.app.devices import climate as climate_module
from myhome_scs_mqtt.app.devices.climate import Climate, ThermoActuator

from ..helpers import Recorder, frame


class ClimateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = Recorder()
        self.ctx = self.rec.context()

    def zone(self, **config) -> Climate:
        return Climate(self.ctx, dict({"where": "8", "name": "Fancoil"}, **config))

    def last(self, suffix: str, where: str = "8") -> str | None:
        return self.rec.last(f"myhome/climate/{where}/{suffix}")


class TestDiscovery(ClimateTestCase):
    def test_modes_come_from_two_checkboxes(self):
        cases = [
            ({"heizen": True}, ["off", "heat", "auto"]),
            ({"heizen": True, "kuehlen": True}, ["off", "heat", "cool", "auto"]),
            ({"heizen": False, "kuehlen": True}, ["off", "cool", "auto"]),
            ({"modes": ["heat"]}, ["off", "heat", "auto"]),          # older files
            ({}, ["off", "heat", "auto"]),
        ]
        for config, modes in cases:
            with self.subTest(config=config):
                self.assertEqual(self.zone(**config).modes, modes)

    def test_every_zone_gets_its_temperature_sensor(self):
        zone = self.zone()
        entities = {object_id: payload for _c, object_id, payload in zone.discovery()}
        thermostat = entities["scs_bridge_4_8"]
        sensor = entities["scs_bridge_4_8_temperature"]
        self.assertEqual(sensor["device_class"], "temperature")
        self.assertEqual(sensor["unit_of_measurement"], "°C")
        self.assertEqual(sensor["state_topic"], thermostat["current_temperature_topic"])
        self.assertEqual(sensor["device"]["identifiers"], thermostat["device"]["identifiers"])

    def test_a_plain_zone_gets_no_empty_fancoil_entities(self):
        ids = [object_id for _c, object_id, _p in self.zone(where="3").discovery()]
        self.assertNotIn("scs_bridge_4_3_humidity", ids)
        self.assertNotIn("scs_bridge_4_3_fan", ids)
        self.assertNotIn("fan_modes", self.zone(where="3").discovery()[0][2])

    def test_only_a_zone_with_a_fan_gets_a_fan_control(self):
        self.assertNotIn("fan_modes", self.zone(luefter=False).discovery()[0][2])
        payload = self.zone(luefter=True).discovery()[0][2]
        self.assertEqual(payload["fan_modes"], ["auto", "1", "2", "3"])
        self.assertEqual(payload["fan_mode_command_topic"], "myhome/climate/8/fan_mode/set")

        detected = self.zone(where="9")
        self.assertNotIn("fan_modes", detected.discovery()[0][2])
        detected.on_own(frame("*#4*9*11*2##"))
        self.assertIn("fan_modes", detected.discovery()[0][2])
        self.assertEqual(self.rec.rediscovered, [detected])

    def test_humidity_appears_once_the_zone_reports_it(self):
        zone = self.zone(where="2")
        zone.on_own(frame("*#4*2*60*54##"))
        self.assertEqual(self.last("humidity", "2"), "54")
        ids = {object_id: payload for _c, object_id, payload in zone.discovery()}
        self.assertEqual(ids["scs_bridge_4_2_humidity"]["unit_of_measurement"], "%")


class TestBusToState(ClimateTestCase):
    def test_temperature_from_wissen(self):
        zone = self.zone(where="1")
        zone.on_own(frame("*#4*1*0*0243##"))
        self.assertEqual(self.last("temperature", "1"), "24.3")

    def test_the_setpoint_that_counts_wins(self):
        # Measured on a fan coil zone: 12 showed 29.0 while 14 stayed at 21.0.
        zone = self.zone()
        zone.on_own(frame("*#4*8*14*0210*3##"))
        self.assertEqual(self.last("setpoint"), "21.0")
        zone.on_own(frame("*#4*8*12*0290*3##"))
        self.assertEqual(self.last("setpoint"), "29.0")
        zone.on_own(frame("*#4*8*14*0210*3##"))
        self.assertEqual(self.last("setpoint"), "29.0")

    def test_fan_setting_is_shown_as_set_on_the_thermostat(self):
        zone = self.zone()
        zone.on_own(frame("*#4*8*11*3##"))
        zone.on_own(frame("*#4*8*19*5*8##"))            # heating circuit at speed 3
        self.assertEqual(self.last("fan"), "3")
        self.assertEqual(self.last("action"), "heating")
        zone.on_own(frame("*#4*8*11*0##"))
        self.assertEqual(self.last("fan"), "auto")
        self.assertEqual(self.last("fan_mode"), "auto")

    def test_a_stopping_fancoil_is_noticed_at_once(self):
        # The actuator reports at once, the valves minutes later; a fan coil
        # says 5 for "off" - checking only for 0 kept it "heating".
        zone = self.zone()
        zone.on_own(frame("*#4*8*19*5*8##"))
        self.assertEqual(self.last("action"), "heating")
        zone.on_own(frame("*#4*8#1*20*5##"))
        self.assertEqual(self.last("action"), "idle")
        zone.on_own(frame("*#4*8#1*20*7##"))
        self.assertEqual(self.last("action"), "heating")
        self.assertEqual(zone.fan_stage, "2")
        zone.on_own(frame("*#4*8#1*20*0##"))              # a plain valve: 0 and 1
        self.assertEqual(self.last("action"), "idle")

    def test_mode_codes(self):
        zone = self.zone(heizen=True, kuehlen=True)
        cases = [("303", "off"), ("1", "heat"), ("0", "cool"), ("210", "auto"),
                 ("110", "heat"), ("111", "cool"), ("102", "off")]
        for what, mode in cases:
            with self.subTest(what=what):
                zone.on_own(frame(f"*4*{what}*8##"))
                self.assertEqual(self.last("mode"), mode)

    def test_the_zone_off_frame_from_wissen(self):
        zone = self.zone(where="1")
        zone.on_own(frame("*4*303*1##"))
        self.assertEqual((self.last("mode", "1"), self.last("action", "1")), ("off", "off"))

    def test_frames_without_meaning_publish_nothing(self):
        zone = self.zone(where="1")
        for raw in ("*#4*1*13*00##", "*#4*1*0*abc##", "*#4*1*60*x##", "*#4*1*0##"):
            zone.on_own(frame(raw))
        self.assertEqual(self.rec.published, [])


class TestCommands(ClimateTestCase):
    def test_zone_commands_go_out_in_both_address_forms(self):
        # "#8" for a zone at a central unit, "8" for a standalone thermostat -
        # both are acknowledged, only the right one acts.
        zone = self.zone()
        zone.on_command("myhome/climate/8/mode/set", "off")
        zone.on_command("myhome/climate/8/mode/set", "auto")
        self.assertEqual(self.rec.sent, ["*4*303*8##", "*4*303*#8##", "*4*311*8##", "*4*311*#8##"])

    def test_the_fan_speed_is_set_without_hash(self):
        # Measured: *#4*#8*#11*1## NACK, *#4*8*#11*1## ACK.
        zone = self.zone(luefter=True)
        zone.on_command("myhome/climate/8/fan_mode/set", "2")
        zone.on_command("myhome/climate/8/fan_mode/set", "auto")
        self.assertEqual(self.rec.sent, ["*#4*8*#11*2##", "*#4*8*#11*0##"])
        self.assertEqual(self.last("fan_mode"), "auto")

    def test_the_fan_mode_topic_is_not_mistaken_for_the_mode_topic(self):
        # ".../fan_mode/set" ends with "/mode/set" as well.
        zone = self.zone(luefter=True)
        zone.on_command("myhome/climate/8/fan_mode/set", "1")
        self.assertEqual(self.rec.sent, ["*#4*8*#11*1##"])

    def test_setpoint_in_both_forms_and_clamped(self):
        zone = self.zone(where="2", min_temp=16, max_temp=24)
        zone.on_command("myhome/climate/2/setpoint/set", "21.5")
        self.assertEqual(self.rec.sent, ["*#4*#2*#14*0215*3##", "*#4*2*#14*0215*3##"])
        zone.on_command("myhome/climate/2/setpoint/set", "30")
        self.assertEqual(self.rec.sent[-1], "*#4*2*#14*0240*3##")
        self.assertEqual(self.last("setpoint", "2"), "24.0")

    def test_heat_or_cool_rewrites_the_setpoint(self):
        zone = self.zone(heizen=True, kuehlen=True)
        zone.on_command("myhome/climate/8/mode/set", "cool")
        self.assertEqual(self.rec.sent[:2], ["*4*0*8##", "*4*0*#8##"])
        self.assertIn("*#4*8*#14*0200*3##", self.rec.sent)
        # Published once, and already with the new mode.
        modes = [p for t, p, _r in self.rec.published if t == "myhome/climate/8/mode"]
        self.assertEqual(modes, ["cool"])

    def test_nonsense_is_refused(self):
        zone = self.zone(luefter=True)
        with self.assertLogs(climate_module._LOGGER, "WARNING"):
            zone.on_command("myhome/climate/8/setpoint/set", "warm")
            zone.on_command("myhome/climate/8/setpoint/set", "nan")
            zone.on_command("myhome/climate/8/mode/set", "dry")
            zone.on_command("myhome/climate/8/fan_mode/set", "turbo")
        self.assertEqual(self.rec.sent, [])


class TestPolling(ClimateTestCase):
    def test_probes_are_repeated_until_answered(self):
        zone = self.zone(where="2")
        for _ in range(5):
            self.assertIn("*#4*2*60##", zone.poll_frames())
            self.assertIn("*#4*2*60##", zone.probe_frames())
        zone.on_own(frame("*#4*2*60*41##"))
        self.assertIn("*#4*2*60##", zone.poll_frames())
        self.assertNotIn("*#4*2*60##", zone.probe_frames())

    def test_regular_poll_frames(self):
        frames = self.zone(where="2").poll_frames()
        for expected in ("*#4*2##", "*#4*2*0##", "*#4*2*12##", "*#4*2*19##"):
            self.assertIn(expected, frames)

    def test_routing_keys_cover_both_forms(self):
        self.assertEqual(self.zone().match_keys(), ("8", "#8"))


class TestFanConfig(ClimateTestCase):
    def test_an_empty_fan_checkbox_stays_empty(self):
        # "Lüfter leer lassen heißt: selbst erkennen" - writing False on save
        # would switch the detection off for good.
        self.assertNotIn("luefter", self.zone().to_config())
        detected = self.zone()
        detected.on_own(frame("*#4*8*11*1##"))
        self.assertTrue(detected.to_config()["luefter"])
        self.assertFalse(self.zone(luefter=False).to_config()["luefter"])


class TestThermoActuator(ClimateTestCase):
    def test_the_pump_hangs_on_the_central_unit(self):
        pump = ThermoActuator(self.ctx, {"where": "3", "name": "Pumpe"})
        self.assertEqual(pump.bus_where, "0#3")
        self.assertEqual(pump.match_keys(), ("0#3",))
        self.assertEqual(pump.poll_frames(), ("*#4*0#3*20##",))
        self.assertEqual(pump.discovery()[0][2]["device_class"], "running")
        self.assertEqual(pump.object_id, "scs_bridge_4_0_3_pump")

    def test_nothing_is_claimed_before_the_first_report(self):
        pump = ThermoActuator(self.ctx, {"where": "3"})
        pump.publish_state()
        self.assertEqual(self.rec.published, [])
        pump.on_own(frame("*#4*0#3*20*1##"))
        self.assertEqual(self.rec.last("myhome/pump/3/state"), "ON")
        pump.on_own(frame("*#4*0#3*20*0##"))
        self.assertEqual(self.rec.last("myhome/pump/3/state"), "OFF")

    def test_fancoil_codes_of_a_zone_actuator(self):
        actuator = ThermoActuator(self.ctx, {"where": "1", "zone": "8"})
        self.assertEqual(actuator.bus_where, "8#1")
        actuator.on_own(frame("*#4*8#1*20*8##"))
        self.assertEqual(self.rec.last("myhome/pump/8_1/state"), "ON")
        actuator.on_own(frame("*#4*8#1*20*5##"))       # 5 = off on a fan coil
        self.assertEqual(self.rec.last("myhome/pump/8_1/state"), "OFF")

    def test_actuator_1_of_two_zones_are_two_devices(self):
        # Every fan coil zone has its actuator at <zone>#1 (WISSEN.md).
        first = ThermoActuator(self.ctx, {"where": "1", "zone": "8"})
        second = ThermoActuator(self.ctx, {"where": "1", "zone": "9"})
        self.assertNotEqual(first.object_id, second.object_id)
        self.assertNotEqual(first.state_topic, second.state_topic)
        self.assertEqual(second.state_topic, "myhome/pump/9_1/state")
        self.assertEqual(second.to_config(), {"where": "1", "name": "Pumpe 1", "zone": "9"})

    def test_a_write_from_the_central_unit_is_no_report(self):
        pump = ThermoActuator(self.ctx, {"where": "1", "zone": "8"})
        pump.on_own(frame("*#4*8#1*#20*8##"))
        self.assertEqual(self.rec.published, [])

    def test_unknown_device_class_is_dropped(self):
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "WARNING"):
            pump = ThermoActuator(self.ctx, {"where": "3", "device_class": "pumpe"})
        self.assertNotIn("device_class", pump.discovery()[0][2])


if __name__ == "__main__":
    unittest.main()
