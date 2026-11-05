"""The bus scan as pure data: plan, classification and the generated report."""

from __future__ import annotations

import unittest
from datetime import datetime

from myhome_scs_mqtt.app import scan
from myhome_scs_mqtt.app.devices.climate import Climate, ThermoActuator
from myhome_scs_mqtt.app.devices.cover import Cover
from myhome_scs_mqtt.app.devices.events import DryContact, Keypad, Scenario
from myhome_scs_mqtt.app.devices.light import Light, Switch
from myhome_scs_mqtt.app.devices.lightmgmt import BmseSensor
from myhome_scs_mqtt.app.devices.sensors import EnergyMeter
from myhome_scs_mqtt.app.own import messages as msg
from myhome_scs_mqtt.app.scan import (
    Classified, ScanOptions, build_plan, classify, plan_size, render_devices_yaml,
)
from myhome_scs_mqtt.app.settings import parse_devices


def frames_of(phases) -> list[str]:
    return [frame for phase in phases for frame in phase.frames]


class TestPlan(unittest.TestCase):
    def test_the_plan_only_asks_never_switches(self):
        for options in (ScanOptions(), ScanOptions(deep=True)):
            for frame in frames_of(build_plan(options)):
                self.assertTrue(frame.startswith("*#"), f"{frame} is a command, not a request")
                self.assertIn(msg.parse(frame).kind,
                              (msg.KIND_STATUS_REQUEST, msg.KIND_DIMENSION_REQUEST))

    def test_the_plan_covers_every_device_class(self):
        phases = build_plan(ScanOptions())
        self.assertEqual([phase.label for phase in phases],
                         ["Gateway", "Licht", "Rollläden", "Heizzonen", "Energiezähler",
                          "Kontakte"])
        frames = frames_of(phases)
        for expected in ("*#13**16##", "*#13**15##", "*#1*0##", "*#1*4##", "*#2*0##",
                         "*#4*7*0##", "*#18*53*113##", "*#25*312##"):
            self.assertIn(expected, frames)

    def test_ranges_are_configurable(self):
        small = plan_size(build_plan(ScanOptions(zones=2, meters=1, contacts=1)))
        self.assertEqual(small, 2 + 10 + 10 + 2 + 1 + 1)
        self.assertEqual(plan_size(build_plan(ScanOptions(zones=0, meters=0, contacts=0))),
                         2 + 10 + 10)
        zones = [p for p in build_plan(ScanOptions(zones=500)) if p.label == "Heizzonen"][0]
        self.assertEqual(len(zones.frames), 99)

    def test_the_deep_scan_includes_four_digit_addresses(self):
        deep = build_plan(ScanOptions(deep=True))[-1]
        self.assertEqual(deep.label, "Feinsuche")
        self.assertEqual(len(deep.frames), 11 * 15 * 2)
        for expected in ("*#1*01##", "*#2*99##", "*#1*0912##", "*#1*1015##", "*#2*1001##"):
            self.assertIn(expected, deep.frames)


class TestClassify(unittest.TestCase):
    def check(self, raw: str, section: str, where: str, extra: dict | None = None):
        result = classify(msg.parse(raw))
        self.assertEqual(result, Classified(section, where, extra or {}), raw)

    def test_lights_and_dimmers(self):
        self.check("*1*1*12##", "lights", "12")
        self.check("*1*0*13##", "lights", "13")
        self.check("*1*6*29##", "lights", "29", {"dimmer": True})
        # DALI from WISSEN.md: dimension 1 when lit, 4 when dark.
        self.check("*#1*29*1*175*2##", "lights", "29", {"dimmer": True})
        self.check("*#1*29*4*100*2##", "lights", "29", {"dimmer": True})
        self.check("*1*1*41#4#01##", "lights", "41#4#01")

    def test_colour_and_white_are_noted(self):
        # Measured 19.09.2026, berichte/17-messung-dali-farbe.md: a lamp that
        # reports a colour or a white temperature dims as well.
        self.check("*#1*0211*12*243*96*100##", "lights", "0211",
                   {"dimmer": True, "rgbw": True})
        self.check("*#1*29*14*38528##", "lights", "29",
                   {"dimmer": True, "tunable_white": True})

    def test_what_is_not_a_lamp(self):
        for raw in (
            "*1*1*0##", "*1*1*00##", "*1*1*000##",     # everything, however written
            "*1*1*#3##",                                 # a group
            "*1*1*3##",                                  # a whole area
            "*1*34*1010##",                              # light management (WISSEN.md)
            "*1*1000#1*29##",                            # command translation
            "*#1*1010*6*220##",                          # BMSE brightness
            "*#1*12##",                                  # our own request
        ):
            with self.subTest(frame=raw):
                self.assertIsNone(classify(msg.parse(raw)))

    def test_shutters(self):
        self.check("*2*1*21##", "covers", "21")
        # Addresses stay text: "04" is not 4.
        self.check("*#2*04*10*10*50*001*0##", "covers", "04")
        result = classify(msg.parse("*#2*05*10*11*27*001*0##"))
        self.assertNotIn("advanced", result.extra)

    def test_refused_position_command_forms_are_no_devices(self):
        # Forms WISSEN.md lists as refused by the actuator; they carry a WHAT
        # that is no movement report.
        for raw in ("*2*11#020#001*04##", "*2*11#020*04##"):
            with self.subTest(frame=raw):
                self.assertIsNone(classify(msg.parse(raw)))

    def test_heating_zones(self):
        self.check("*#4*1*0*0243##", "climate", "1")
        self.check("*#4*2*14*0210*3##", "climate", "2")
        # A fan coil zone switched on reports the setpoint in force first.
        self.check("*#4*8*12*0290*3##", "climate", "8")
        # Zones report with the plain address (WISSEN.md); "#2" is the command
        # form for a zone at a central unit and no report of a device.
        for raw in ("*#4*#2*14*0210*3##", "*#4*0*0*0243##", "*#4*8#1*20*8##",
                    "*#4*0#3*20*1##", "*4*303*8##", "*#4*8*12##"):
            with self.subTest(frame=raw):
                self.assertIsNone(classify(msg.parse(raw)))

    def test_meters_contacts_and_keypads(self):
        self.check("*#18*51*113*0000850##", "energy", "51")
        self.check("*25*32#1*399##", "binary_sensors", "99")
        self.check("*25*21#2*22##", "buttons", "22", {"kind": "cen_plus"})
        self.check("*15*3*51##", "buttons", "51", {"kind": "cen"})
        for raw in ("*25*21#2*52##", "*#13**15*200##", "*5*1*1##",
                    # bare requests - the scan's own, or a second client's
                    "*#25*312##", "*#25*22##", "*#15*51##"):
            with self.subTest(frame=raw):
                self.assertIsNone(classify(msg.parse(raw)))

    def test_the_scan_plan_itself_finds_nothing(self):
        # Whatever the scan asks must never turn into a device by itself.
        for options in (ScanOptions(), ScanOptions(deep=True)):
            for frame in frames_of(build_plan(options)):
                with self.subTest(frame=frame):
                    self.assertIsNone(classify(msg.parse(frame)))

    def test_section_names_match_the_device_types(self):
        pairs = [(scan.SECTION_LIGHTS, Light), (scan.SECTION_SWITCHES, Switch),
                 (scan.SECTION_COVERS, Cover), (scan.SECTION_CLIMATE, Climate),
                 (scan.SECTION_PUMPS, ThermoActuator), (scan.SECTION_ENERGY, EnergyMeter),
                 (scan.SECTION_CONTACTS, DryContact), (scan.SECTION_MOTION, BmseSensor),
                 (scan.SECTION_BUTTONS, Keypad), (scan.SECTION_SCENES, Scenario)]
        for name, factory in pairs:
            self.assertEqual(name, factory.section)
        self.assertEqual(sorted(scan.SECTION_ORDER), sorted(name for name, _f in pairs))


class TestReport(unittest.TestCase):
    SECTIONS = {
        "lights": [{"where": "05", "name": 'Küche "hell"', "dimmer": True}],
        "climate": [{"where": "2", "name": "Bad", "heizen": True, "min_temp": 16.5}],
        "scenes": [{"where": "01", "name": "Szenen", "scenarios": [1, 2],
                    "scenario_names": {1: "Alles aus", 2: "Kino"}}],
        "pumps": [{"where": "3", "name": "Pumpe", "zone": "0"}],
        "motion_sensors": [{"where": "1010", "name": "Melder", "off_delay": 60}],
        "custom_section": [{"where": "7", "name": "C:\\Pfad\nzweite Zeile",
                            "odd key": "x"}],
    }

    def test_addresses_stay_strings_and_names_survive(self):
        text = render_devices_yaml(self.SECTIONS, 6, generated=datetime(2026, 9, 16, 12, 30))
        self.assertIn('- where: "05"', text)
        self.assertIn("dimmer: true", text)
        self.assertIn("6 Geräte gefunden", text)
        self.assertIn("vom 16.09.2026 12:30", text)

    def test_the_report_reads_back_exactly(self):
        text = render_devices_yaml(self.SECTIONS, 6)
        self.assertEqual(parse_devices(text), self.SECTIONS)

    def test_no_section_is_left_out(self):
        # The old report knew eight sections and silently dropped the rest.
        text = render_devices_yaml(self.SECTIONS)
        for section in self.SECTIONS:
            self.assertIn(f"{section}:", text)

    def test_where_and_name_come_first(self):
        text = render_devices_yaml({"lights": [{"dimmer": True, "name": "A", "where": "12"}]})
        lines = [line for line in text.splitlines() if line.startswith(("  -", "    "))]
        self.assertEqual(lines, ['  - where: "12"', '    name: "A"', "    dimmer: true"])

    def test_empty_sections_and_none_values_are_skipped(self):
        text = render_devices_yaml({"lights": [], "covers": [{"where": "21", "name": None}]})
        self.assertNotIn("lights:", text)
        self.assertNotIn("name:", text)


if __name__ == "__main__":
    unittest.main()
