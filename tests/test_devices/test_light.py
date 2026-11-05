"""WHO=1 lights, dimmers (including DALI) and switches."""

from __future__ import annotations

import json
import unittest

from myhome_scs_mqtt.app.devices import light as light_module
from myhome_scs_mqtt.app.devices.light import Light, Switch

from ..helpers import Recorder, frame


class LightTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = Recorder()
        self.ctx = self.rec.context()

    def state(self, where: str = "12") -> dict:
        return json.loads(self.rec.last(f"myhome/light/{where}/state"))


class TestDiscovery(LightTestCase):
    def test_a_relay_and_a_dimmer(self):
        relay = Light(self.ctx, {"where": "12", "name": "Kitchen"}).discovery()
        self.assertEqual(len(relay), 1)
        component, object_id, payload = relay[0]
        self.assertEqual((component, object_id), ("light", "scs_bridge_1_12"))
        self.assertEqual(payload["schema"], "json")
        self.assertFalse(payload["brightness"])
        self.assertEqual(payload["command_topic"], "myhome/light/12/set")
        dimmer = Light(self.ctx, {"where": "13", "dimmer": True}).discovery()[0][2]
        self.assertTrue(dimmer["brightness"])

    def test_an_older_device_file_still_reads(self):
        lamp = Light(self.ctx, {"where": "29", "name": "Alt", "dimmer100": True})
        self.assertTrue(lamp.dimmer and lamp.dimmer100)
        self.assertTrue(lamp.to_config()["dimmer"])
        self.assertNotIn("dimmer100", lamp.to_config())


class TestBusToState(LightTestCase):
    def test_on_and_off(self):
        lamp = Light(self.ctx, {"where": "12"})
        lamp.on_own(frame("*1*1*12##"))
        self.assertEqual(self.state(), {"state": "ON"})
        lamp.on_own(frame("*1*0*12##"))
        self.assertEqual(self.state(), {"state": "OFF"})

    def test_a_light_that_dims_becomes_a_dimmer(self):
        # A dimmer at 0 % or 100 % looks exactly like a relay on the bus.
        lamp = Light(self.ctx, {"where": "44"})
        lamp.on_own(frame("*1*5*44##"))
        self.assertTrue(lamp.dimmer)
        self.assertFalse(lamp.dimmer100)
        self.assertEqual(self.rec.rediscovered, [lamp])
        self.assertTrue(lamp.discovery()[0][2]["brightness"])
        self.assertEqual(self.state("44"), {"state": "ON", "brightness": 128})

    def test_dali_level_from_wissen(self):
        # *#1*29*1*175*2## = 75 %
        lamp = Light(self.ctx, {"where": "29"})
        lamp.on_own(frame("*#1*29*1*175*2##"))
        self.assertTrue(lamp.dimmer100)
        self.assertEqual(self.state("29"), {"state": "ON", "brightness": 191})

    def test_a_dark_dali_lamp_answers_in_dimension_4(self):
        lamp = Light(self.ctx, {"where": "29", "dimmer": True})
        lamp.on_own(frame("*#1*29*1*175*2##"))
        lamp.on_own(frame("*#1*29*4*100*2##"))
        # Off, and the last brightness is kept for switching on again.
        self.assertEqual(self.state("29"), {"state": "OFF", "brightness": 191})
        self.assertTrue(lamp.dimmer100)

    def test_a_rounded_step_is_followed_by_the_exact_level(self):
        # At a real 40 % the bus says WHAT 6.  The step is shown at once, the
        # exact level is asked for once the fade has settled.
        lamp = Light(self.ctx, {"where": "29", "dimmer": True})
        lamp.on_own(frame("*1*6*29##"))
        self.assertEqual(self.state("29"), {"state": "ON", "brightness": 153})
        self.assertEqual(self.rec.sent, [])
        self.rec.advance(light_module.LEVEL_SETTLE - 0.1)
        self.assertEqual(self.rec.sent, [])
        self.rec.advance(0.2)
        self.assertEqual(self.rec.sent, ["*#1*29*1##"])
        lamp.on_own(frame("*#1*29*1*140*2##"))
        self.assertEqual(self.state("29")["brightness"], 102)

    def test_several_steps_ask_only_once(self):
        lamp = Light(self.ctx, {"where": "29", "dimmer": True})
        for what in ("4", "5", "6"):
            lamp.on_own(frame(f"*1*{what}*29##"))
        self.rec.advance(light_module.LEVEL_SETTLE + 0.1)
        self.assertEqual(self.rec.sent, ["*#1*29*1##"])

    def test_frames_without_state_are_ignored(self):
        lamp = Light(self.ctx, {"where": "29"})
        for raw in ("*1*1000#1*29##", "*1*34*29##", "*#1*29*1*abc##", "*#1*29*1*250##",
                    "*#1*29*6*220##", "*#1*29##"):
            lamp.on_own(frame(raw))
        self.assertEqual(self.rec.published, [])
        self.assertFalse(lamp.dimmer)


class TestCommands(LightTestCase):
    def test_switching(self):
        lamp = Light(self.ctx, {"where": "12"})
        lamp.on_command(lamp.command_topic, '{"state": "ON"}')
        lamp.on_command(lamp.command_topic, '{"state": "OFF"}')
        lamp.on_command(lamp.command_topic, "ON")
        self.assertEqual(self.rec.sent, ["*1*1*12##", "*1*0*12##", "*1*1*12##"])

    def test_percent_dimmer_uses_dimension_1(self):
        lamp = Light(self.ctx, {"where": "12", "dimmer": True})
        lamp.on_command(lamp.command_topic, '{"state": "ON", "brightness": 128}')
        self.assertEqual(self.rec.sent, ["*#1*12*#1*150*255##"])

    def test_classic_dimmer_uses_ten_steps(self):
        lamp = Light(self.ctx, {"where": "44"})
        lamp.on_own(frame("*1*5*44##"))            # learned: a 10-step dimmer
        lamp.on_command(lamp.command_topic, '{"state": "ON", "brightness": 255}')
        lamp.on_command(lamp.command_topic, '{"state": "ON", "brightness": 3}')
        self.assertEqual(self.rec.sent, ["*1*10*44##", "*1*2*44##"])

    def test_brightness_on_a_relay_just_switches_on(self):
        lamp = Light(self.ctx, {"where": "12"})
        lamp.on_command(lamp.command_topic, '{"state": "ON", "brightness": 50}')
        self.assertEqual(self.rec.sent, ["*1*1*12##"])

    def test_odd_payloads_do_not_crash(self):
        lamp = Light(self.ctx, {"where": "12", "dimmer": True})
        with self.assertLogs(light_module._LOGGER, "WARNING"):
            lamp.on_command(lamp.command_topic, '{"brightness": "bright"}')
        lamp.on_command(lamp.command_topic, "1")        # valid JSON, not an object
        lamp.on_command(lamp.command_topic, "[]")
        self.assertEqual(self.rec.sent, ["*1*1*12##", "*1*1*12##"])


class TestPolling(LightTestCase):
    def test_a_relay_is_asked_about_its_brightness_only_once(self):
        lamp = Light(self.ctx, {"where": "12"})
        self.assertEqual(lamp.poll_frames(), ("*#1*12##", "*#1*12*1##"))
        self.assertEqual(lamp.poll_frames(), ("*#1*12##",))

    def test_a_percent_dimmer_is_asked_every_round(self):
        lamp = Light(self.ctx, {"where": "12", "dimmer": True})
        lamp.poll_frames()
        self.assertEqual(lamp.poll_frames(), ("*#1*12##", "*#1*12*1##"))

    def test_scan_followup_only_for_lamps_not_yet_known_as_dimmer(self):
        self.assertEqual(Light(self.ctx, {"where": "12"}).followup_frames(), ("*#1*12*1##",))
        self.assertEqual(Light(self.ctx, {"where": "13", "dimmer": True}).followup_frames(), ())


class TestColourDiscovery(LightTestCase):
    """RGBW and tunable white, measured 19.09.2026 (berichte/17 and 18)."""

    def test_colour_and_white_become_colour_modes(self):
        rgbw = Light(self.ctx, {"where": "0211", "rgbw": True}).discovery()[0][2]
        self.assertEqual(rgbw["supported_color_modes"], ["hs"])
        self.assertTrue(rgbw["brightness"])
        self.assertNotIn("min_kelvin", rgbw)

        white = Light(self.ctx, {"where": "29", "tunable_white": True}).discovery()[0][2]
        self.assertEqual(white["supported_color_modes"], ["color_temp"])
        # Kelvin instead of mireds, with the range of the measured lamp.
        self.assertTrue(white["color_temp_kelvin"])
        self.assertEqual((white["min_kelvin"], white["max_kelvin"]), (2000, 6200))

        both = Light(self.ctx, {"where": "30", "rgbw": True, "tunable_white": True,
                                "min_kelvin": 2200, "max_kelvin": "5000"}).discovery()[0][2]
        self.assertEqual(both["supported_color_modes"], ["hs", "color_temp"])
        self.assertEqual((both["min_kelvin"], both["max_kelvin"]), (2200, 5000))

    def test_a_plain_lamp_is_untouched(self):
        for config in ({"where": "12"}, {"where": "13", "dimmer": True}):
            with self.subTest(config=config):
                lamp = Light(self.ctx, config)
                payload = lamp.discovery()[0][2]
                self.assertNotIn("supported_color_modes", payload)
                self.assertNotIn("color_temp_kelvin", payload)
                self.assertEqual(lamp.probe_frames(), ())
        dimmer = Light(self.ctx, {"where": "13", "dimmer": True})
        self.assertEqual(dimmer.poll_frames(), ("*#1*13##", "*#1*13*1##"))

    def test_colour_implies_the_dimmer_and_keeps_unknown_fields(self):
        lamp = Light(self.ctx, {"where": "0211", "rgbw": True, "eigene_notiz": "2019"})
        self.assertTrue(lamp.dimmer and lamp.dimmer100)
        config = lamp.to_config()
        self.assertTrue(config["rgbw"] and config["dimmer"])
        self.assertEqual(config["eigene_notiz"], "2019")
        self.assertNotIn("min_kelvin", config)
        self.assertNotIn("tunable_white", config)

    def test_a_range_that_makes_no_sense_falls_back(self):
        with self.assertLogs(light_module._LOGGER, "WARNING"):
            lamp = Light(self.ctx, {"where": "29", "tunable_white": True,
                                    "min_kelvin": "warm"})
        self.assertEqual((lamp.min_kelvin, lamp.max_kelvin), (2000, 6200))
        self.assertEqual(lamp.to_config()["min_kelvin"], 2000)
        with self.assertLogs(light_module._LOGGER, "WARNING"):
            lamp = Light(self.ctx, {"where": "29", "tunable_white": True,
                                    "min_kelvin": 6000, "max_kelvin": 3000})
        self.assertEqual((lamp.min_kelvin, lamp.max_kelvin), (2000, 6200))


class TestColourCommands(LightTestCase):
    def test_a_colour_becomes_the_measured_frame(self):
        lamp = Light(self.ctx, {"where": "0211", "rgbw": True})
        lamp.is_on = True
        lamp.on_command(lamp.command_topic, '{"state": "ON", "color": {"h": 120, "s": 100}}')
        self.assertEqual(self.rec.sent, ["*#1*0211*#12*120*100*100##"])

    def test_on_with_a_colour_switches_on_first(self):
        # Measured: a lamp that is off ignores the colour and stays dark.
        lamp = Light(self.ctx, {"where": "0211", "rgbw": True})
        lamp.on_command(lamp.command_topic, '{"state": "ON", "color": {"h": 243, "s": 96}}')
        self.assertEqual(self.rec.sent, ["*1*1*0211##", "*#1*0211*#12*243*96*100##"])

    def test_brightness_and_colour_need_no_switch_command(self):
        # The level frame switches the lamp on by itself (WISSEN.md, DALI).
        lamp = Light(self.ctx, {"where": "0211", "rgbw": True})
        lamp.on_command(lamp.command_topic,
                        '{"state": "ON", "brightness": 128, "color": {"h": 120, "s": 50}}')
        self.assertEqual(self.rec.sent,
                         ["*#1*0211*#1*150*255##", "*#1*0211*#12*120*50*100##"])

    def test_the_brightness_is_untouched_by_a_colour(self):
        lamp = Light(self.ctx, {"where": "0211", "rgbw": True})
        lamp.on_own(frame("*#1*0211*1*140*2##"))                 # 40 %
        self.rec.sent.clear()
        lamp.on_command(lamp.command_topic, '{"state": "ON", "color": {"h": 1, "s": 2}}')
        self.assertEqual(self.rec.sent, ["*#1*0211*#12*1*2*100##"])
        # And the answer of the lamp leaves the brightness where it was.
        lamp.on_own(frame("*#1*0211*12*1*2*100##"))
        self.assertEqual(self.state("0211")["brightness"], 102)

    def test_the_white_temperature_stays_in_the_configured_range(self):
        lamp = Light(self.ctx, {"where": "29", "tunable_white": True})
        lamp.is_on = True
        for kelvin in (2700, 9000, 1000):
            lamp.on_command(lamp.command_topic, f'{{"state": "ON", "color_temp": {kelvin}}}')
        self.assertEqual(self.rec.sent, ["*#1*29*#14*37037##",      # 2700 K
                                         "*#1*29*#14*16129##",      # cut to 6200 K
                                         "*#1*29*#14*50000##"])     # cut to 2000 K

    def test_switching_off_stays_one_frame(self):
        lamp = Light(self.ctx, {"where": "0211", "rgbw": True, "tunable_white": True})
        lamp.on_command(lamp.command_topic, '{"state": "OFF", "color": {"h": 1, "s": 2}}')
        self.assertEqual(self.rec.sent, ["*1*0*0211##"])

    def test_a_lamp_without_colour_just_switches_on(self):
        lamp = Light(self.ctx, {"where": "12", "dimmer": True})
        lamp.on_command(lamp.command_topic,
                        '{"state": "ON", "color": {"h": 120, "s": 100}, "color_temp": 3000}')
        self.assertEqual(self.rec.sent, ["*1*1*12##"])

    def test_odd_colours_never_reach_the_bus(self):
        lamp = Light(self.ctx, {"where": "0211", "rgbw": True, "tunable_white": True})
        lamp.is_on = True
        with self.assertLogs(light_module._LOGGER, "WARNING"):
            lamp.on_command(lamp.command_topic, '{"color": {"h": "rot", "s": 100}}')
        with self.assertLogs(light_module._LOGGER, "WARNING"):
            lamp.on_command(lamp.command_topic, '{"color_temp": "warm"}')
        lamp.on_command(lamp.command_topic, '{"color": {"r": 255, "g": 0, "b": 0}}')
        # Three commands, three plain switch-on frames - nothing colourful.
        self.assertEqual(self.rec.sent, ["*1*1*0211##"] * 3)

    def test_a_broken_brightness_does_not_swallow_the_colour(self):
        lamp = Light(self.ctx, {"where": "0211", "rgbw": True})
        lamp.is_on = True
        with self.assertLogs(light_module._LOGGER, "WARNING"):
            lamp.on_command(lamp.command_topic,
                            '{"brightness": "hell", "color": {"h": 120, "s": 100}}')
        self.assertEqual(self.rec.sent, ["*#1*0211*#12*120*100*100##"])
        # On its own a broken brightness still sends nothing at all.
        self.rec.sent.clear()
        with self.assertLogs(light_module._LOGGER, "WARNING"):
            lamp.on_command(lamp.command_topic, '{"brightness": "hell"}')
        self.assertEqual(self.rec.sent, [])


class TestColourFromTheBus(LightTestCase):
    def test_a_colour_report_becomes_the_state(self):
        lamp = Light(self.ctx, {"where": "0211", "rgbw": True})
        lamp.on_own(frame("*1*1*0211##"))
        lamp.on_own(frame("*#1*0211*12*243*96*100##"))
        self.assertEqual(self.state("0211"),
                         {"state": "ON", "brightness": 255, "color_mode": "hs",
                          "color": {"h": 243, "s": 96}})

    def test_the_hundred_in_a_colour_is_no_brightness(self):
        lamp = Light(self.ctx, {"where": "0211", "rgbw": True})
        lamp.on_own(frame("*#1*0211*1*140*2##"))                 # 40 %
        self.assertEqual(self.state("0211")["brightness"], 102)
        lamp.on_own(frame("*#1*0211*12*120*98*100##"))
        self.assertEqual(self.state("0211")["brightness"], 102)

    def test_a_white_temperature_report_becomes_kelvin(self):
        lamp = Light(self.ctx, {"where": "29", "tunable_white": True})
        lamp.on_own(frame("*#1*29*14*38528##"))
        state = self.state("29")
        self.assertEqual(state["color_mode"], "color_temp")
        self.assertEqual(state["color_temp"], 2596)

    def test_a_report_outside_the_range_is_pulled_in(self):
        lamp = Light(self.ctx, {"where": "29", "tunable_white": True})
        lamp.on_own(frame("*#1*29*14*945##"))           # the cold end it reported
        self.assertEqual(self.state("29")["color_temp"], 6200)
        lamp.on_own(frame("*#1*29*14*65534##"))         # and the warm one
        self.assertEqual(self.state("29")["color_temp"], 2000)

    def test_only_one_colour_mode_at_a_time(self):
        lamp = Light(self.ctx, {"where": "30", "rgbw": True, "tunable_white": True})
        lamp.on_own(frame("*#1*30*12*120*98*100##"))
        self.assertEqual(self.state("30")["color_mode"], "hs")
        self.assertNotIn("color_temp", self.state("30"))
        lamp.on_own(frame("*#1*30*14*37040##"))
        state = self.state("30")
        self.assertEqual((state["color_mode"], state["color_temp"]), ("color_temp", 2700))
        self.assertNotIn("color", state)

    def test_a_lamp_that_reports_a_colour_is_upgraded_once(self):
        lamp = Light(self.ctx, {"where": "0211"})
        with self.assertLogs(light_module._LOGGER, "INFO"):
            lamp.on_own(frame("*#1*0211*12*352*70*100##"))
        self.assertTrue(lamp.rgbw and lamp.dimmer and lamp.dimmer100)
        self.assertEqual(self.rec.rediscovered, [lamp])
        self.assertEqual(lamp.discovery()[0][2]["supported_color_modes"], ["hs"])
        self.assertTrue(lamp.to_config()["rgbw"])
        lamp.on_own(frame("*#1*0211*12*120*98*100##"))
        self.assertEqual(self.rec.rediscovered, [lamp], "announced twice")

    def test_a_lamp_that_reports_a_white_temperature_is_upgraded(self):
        lamp = Light(self.ctx, {"where": "29", "dimmer": True})
        with self.assertLogs(light_module._LOGGER, "INFO"):
            lamp.on_own(frame("*#1*29*14*37040##"))
        self.assertTrue(lamp.tunable_white)
        self.assertEqual(self.rec.rediscovered, [lamp])
        payload = lamp.discovery()[0][2]
        self.assertEqual(payload["supported_color_modes"], ["color_temp"])
        self.assertTrue(payload["color_temp_kelvin"])
        self.assertTrue(lamp.to_config()["tunable_white"])

    def test_nonsense_changes_nothing(self):
        lamp = Light(self.ctx, {"where": "0211", "rgbw": True, "tunable_white": True})
        for raw in ("*#1*0211*12*243*96##", "*#1*0211*12*abc*96*100##",
                    "*#1*0211*12*360*96*100##", "*#1*0211*14*0##",
                    "*#1*0211*14*warm##", "*#1*0211*14##"):
            with self.subTest(frame=raw):
                lamp.on_own(frame(raw))
        self.assertEqual(self.rec.published, [])


class TestColourPolling(LightTestCase):
    def test_the_colour_questions_are_probes_and_stop_once_answered(self):
        lamp = Light(self.ctx, {"where": "0211", "rgbw": True, "tunable_white": True})
        self.assertEqual(lamp.probe_frames(), ("*#1*0211*12##", "*#1*0211*14##"))
        self.assertEqual(lamp.poll_frames(),
                         ("*#1*0211##", "*#1*0211*1##", "*#1*0211*12##", "*#1*0211*14##"))
        # A probe only counts as one when it is part of the poll round.
        self.assertTrue(set(lamp.probe_frames()) <= set(lamp.poll_frames()))
        lamp.on_own(frame("*#1*0211*12*120*98*100##"))
        self.assertEqual(lamp.probe_frames(), ("*#1*0211*14##",))
        lamp.on_own(frame("*#1*0211*14*37040##"))
        self.assertEqual(lamp.probe_frames(), ())
        self.assertEqual(lamp.poll_frames(), ("*#1*0211##", "*#1*0211*1##"))


class TestSwitch(LightTestCase):
    def test_switch_on_another_who(self):
        switch = Switch(self.ctx, {"where": "1", "who": 9, "device_class": "outlet"})
        payload = switch.discovery()[0][2]
        self.assertEqual(payload["device_class"], "outlet")
        self.assertEqual(switch.object_id, "scs_bridge_9_1")
        switch.on_command(switch.command_topic, "ON")
        switch.on_command(switch.command_topic, "off")
        self.assertEqual(self.rec.sent, ["*9*1*1##", "*9*0*1##"])
        self.assertEqual(switch.poll_frames(), ("*#9*1##",))
        self.assertEqual(switch.to_config()["who"], 9)

    def test_a_switch_never_turns_into_a_dimmer(self):
        switch = Switch(self.ctx, {"where": "12"})
        switch.on_own(frame("*1*5*12##"))
        self.assertEqual(self.rec.last("myhome/switch/12/state"), "ON")
        self.assertEqual(self.rec.rediscovered, [])
        switch.on_own(frame("*#1*12*1*150*2##"))
        switch.on_own(frame("*1*0*12##"))
        self.assertEqual(self.rec.last("myhome/switch/12/state"), "OFF")


if __name__ == "__main__":
    unittest.main()
