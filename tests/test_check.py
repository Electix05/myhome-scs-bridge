"""The check of a device list before the web interface writes it."""

from __future__ import annotations

import logging
import unittest

from myhome_scs_mqtt.app.bridge import registry as registry_module
from myhome_scs_mqtt.app.bridge.check import check_devices, is_blank
from myhome_scs_mqtt.app.bridge.registry import FACTORIES, Registry
from myhome_scs_mqtt.app.devices import base as base_module

from .helpers import Recorder

GOOD = {
    "lights": [{"where": "12", "name": "Küche", "dimmer": True},
               {"where": "0211", "name": "Farbe", "rgbw": True, "tunable_white": True,
                "min_kelvin": 2200, "max_kelvin": 5000},
               {"where": "23#4#01", "name": "Lokaler Bus"}],
    "switches": [{"where": "14", "name": "Steckdose", "who": 1, "device_class": "outlet"}],
    "covers": [{"where": "21", "name": "Rollo", "travel_time": 25, "tilt_time": 1.4,
                "device_class": "blind"}],
    "climate": [{"where": "2", "name": "Bad", "heizen": True, "min_temp": 5, "max_temp": 30}],
    "energy": [{"where": "51", "name": "Zähler", "energy_total": True}],
    "motion_sensors": [{"where": "12", "name": "Melder", "motion_poll": 0, "off_delay": 60}],
    "pumps": [{"where": "1", "zone": "8"}, {"where": "1", "zone": "9"}, {"where": "3"}],
    "binary_sensors": [{"where": "1", "schliesser": True},
                       {"where": "1", "bus_prefix": "4", "oeffner": True},
                       {"where": "7", "name": "noch nicht zugeordnet"}],
    "buttons": [{"where": "22", "kind": "cen_plus", "buttons": [1, 2, 3]}],
    "scenes": [{"where": "01", "scenarios": [1, 2]}],
}


def found(sections: dict) -> list[tuple[str, int, str]]:
    return [(p["section"], p["index"], p["field"]) for p in check_devices(sections)]


class TestAGoodList(unittest.TestCase):
    def test_nothing_is_found_in_a_list_that_is_fine(self):
        self.assertEqual(check_devices(GOOD), [])

    def test_every_device_type_is_part_of_the_sample(self):
        self.assertEqual(set(GOOD), set(FACTORIES))

    def test_rows_nobody_filled_in_are_left_alone(self):
        # "+ Gerät" adds an empty row; it is simply not saved.
        sections = {"lights": [{"where": "", "name": ""}, {"where": "12"},
                               {"where": "", "name": "  ", "dimmer": False}]}
        self.assertEqual(check_devices(sections), [])
        self.assertTrue(is_blank({"where": "", "name": "", "motion_poll": 0}))
        self.assertFalse(is_blank({"where": "", "name": "Küche"}))

    def test_neighbours_on_purpose_are_no_doubles(self):
        # A BMSE sensor sits on the address of a light point; actuator 1 of two
        # zones and contact 1 next to IR interface 1 are two devices each.
        self.assertEqual(found({"lights": [{"where": "12"}],
                                "motion_sensors": [{"where": "12"}]}), [])


class TestWhatIsFound(unittest.TestCase):
    def test_a_row_with_a_name_and_no_address(self):
        # Until now such a row was dropped without a word when saving.
        problems = check_devices({"lights": [{"where": "12"}, {"where": "", "name": "Flur"}]})
        self.assertEqual([(p["section"], p["index"], p["field"]) for p in problems],
                         [("lights", 1, "where")])
        self.assertIn("Adresse fehlt", problems[0]["text"])

    def test_an_address_that_cannot_be_part_of_a_frame(self):
        for where in ("12a", "1 2", "12#", "#", "*12", "1.2", "Küche"):
            with self.subTest(where=where):
                self.assertEqual(found({"lights": [{"where": where}]}), [("lights", 0, "where")])
        for where in ("12", "0210", "23#4#01", "#3", " 12 "):
            with self.subTest(where=where):
                self.assertEqual(found({"lights": [{"where": where}]}), [])

    def test_the_same_address_twice(self):
        problems = check_devices({"lights": [{"where": "12"}, {"where": "13"}, {"where": "12"}]})
        self.assertEqual([(p["index"], p["field"]) for p in problems], [(2, "where")])
        self.assertIn("Zeile 1", problems[0]["text"])

    def test_a_double_is_found_even_next_to_a_row_that_has_another_fault(self):
        # Found in the browser: the first lamp had its kelvin range upside
        # down, and the second lamp on the same address went unnoticed.
        sections = {"lights": [{"where": "12", "min_kelvin": 5000, "max_kelvin": 3000},
                               {"where": "12"}]}
        self.assertEqual(found(sections), [("lights", 0, "max_kelvin"), ("lights", 1, "where")])
        # A value the device type refuses is named once, not twice.
        self.assertEqual(found({"covers": [{"where": "21", "travel_time": "fast"}]}),
                         [("covers", 0, "travel_time")])

    def test_one_actuator_as_a_light_and_as_a_switch(self):
        # Two sections, one device on the bus - the registry would drop the second.
        problems = check_devices({"lights": [{"where": "12"}], "switches": [{"where": "12"}]})
        self.assertEqual([(p["section"], p["index"]) for p in problems], [("switches", 0)])
        self.assertIn("Licht und Dimmer", problems[0]["text"])
        # On another WHO it is another device.
        self.assertEqual(found({"lights": [{"where": "12"}],
                                "switches": [{"where": "12", "who": 9}]}), [])

    def test_doubles_the_bare_address_does_not_show(self):
        self.assertEqual(found({"pumps": [{"where": "1", "zone": "8"},
                                          {"where": "1", "zone": "8"}]}), [("pumps", 1, "where")])
        self.assertEqual(found({"climate": [{"where": "2"}, {"where": "#2"}]}),
                         [("climate", 1, "where")])

    def test_a_contact_that_is_both_kinds(self):
        self.assertEqual(found({"binary_sensors": [{"where": "1", "schliesser": True,
                                                    "oeffner": True}]}),
                         [("binary_sensors", 0, "oeffner")])

    def test_a_colour_temperature_range(self):
        cases = [
            ({"min_kelvin": 5000, "max_kelvin": 3000}, ["max_kelvin"]),      # upside down
            ({"min_kelvin": 7000}, ["max_kelvin"]),                          # above the default
            ({"min_kelvin": 200}, ["min_kelvin"]),                           # no temperature
            ({"max_kelvin": "kalt"}, ["max_kelvin"]),
            ({"min_kelvin": 2700, "max_kelvin": 6500}, []),
        ]
        for extra, fields in cases:
            with self.subTest(extra=extra):
                problems = check_devices({"lights": [dict(extra, where="12")]})
                self.assertEqual([p["field"] for p in problems], fields)

    def test_numbers_that_cannot_be_meant(self):
        self.assertEqual(found({"covers": [{"where": "21", "travel_time": -5,
                                            "tilt_time": "kurz"}]}),
                         [("covers", 0, "travel_time"), ("covers", 0, "tilt_time")])
        self.assertEqual(found({"climate": [{"where": "2", "min_temp": 30, "max_temp": 20}]}),
                         [("climate", 0, "max_temp")])
        self.assertEqual(found({"climate": [{"where": "2", "min_temp": 40}]}),
                         [("climate", 0, "max_temp")])
        self.assertEqual(found({"motion_sensors": [{"where": "12", "off_delay": -1}]}),
                         [("motion_sensors", 0, "off_delay")])

    def test_lists_zones_and_who(self):
        self.assertEqual(found({"buttons": [{"where": "22", "buttons": [1, "oben"]}]}),
                         [("buttons", 0, "buttons")])
        self.assertEqual(found({"scenes": [{"where": "01", "scenarios": "1, 2"}]}),
                         [("scenes", 0, "scenarios")])
        self.assertEqual(found({"pumps": [{"where": "3", "zone": "Keller"}]}),
                         [("pumps", 0, "zone")])
        self.assertEqual(found({"switches": [{"where": "14", "who": "1a"}]}),
                         [("switches", 0, "who")])

    def test_what_only_the_device_type_itself_notices(self):
        # Nothing above looks at "tilt_start" - the class that reads it does.
        problems = check_devices({"covers": [{"where": "21", "tilt_start": "halb"}]})
        self.assertEqual([(p["index"], p["field"]) for p in problems], [(0, "")])
        self.assertIn("lässt sich so nicht lesen", problems[0]["text"])

    def test_an_unknown_section(self):
        problems = check_devices({"ligths": [{"where": "12"}]})
        self.assertEqual([(p["section"], p["index"]) for p in problems], [("ligths", 0)])

    def test_several_findings_in_one_list_keep_their_rows(self):
        sections = {"lights": [{"where": "12"}, {"where": "x"}, {"where": "12"}],
                    "covers": [{"where": "21", "travel_time": -1}]}
        self.assertEqual(found(sections), [("lights", 1, "where"), ("lights", 2, "where"),
                                           ("covers", 0, "travel_time")])

    def test_what_is_no_list_at_all_is_refused(self):
        for data in ([], "lights", {"lights": {"where": "12"}}, {"lights": ["12"]}):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    check_devices(data)


class TestTheCheckAndTheStartAgree(unittest.TestCase):
    """What passes the check must not become a problem when the add-on starts."""

    SAMPLES = [
        GOOD,
        {"lights": [{"where": "12"}, {"where": "12"}]},
        {"lights": [{"where": "12"}], "switches": [{"where": "12"}]},
        {"covers": [{"where": "21", "travel_time": "fast"}]},
        {"covers": [{"where": "21", "tilt_start": "halb"}]},
        {"ligths": [{"where": "12"}]},
        {"motion_sensors": [{"where": "12", "lux_interval": "oft"}]},
        {"climate": [{"where": "2", "min_temp": "kalt"}]},
        {"lights": [{"where": "12"}], "motion_sensors": [{"where": "12"}]},
    ]

    def test_a_list_that_passes_starts_without_a_problem(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        passed = 0
        for sections in self.SAMPLES:
            with self.subTest(sections=sections):
                registry = Registry(Recorder().context())
                registry.build(sections, [])
                wanted = sum(len(items) for items in sections.values())
                starts_clean = (not registry.problems
                                and len(registry.device_entities()) == wanted)
                if not check_devices(sections):
                    passed += 1
                    self.assertTrue(starts_clean, "the check let through what the start drops")
                else:
                    self.assertFalse(starts_clean, "the check refused what the start reads")
        self.assertGreaterEqual(passed, 2)


class TestTheCheckStaysOutOfTheLog(unittest.TestCase):
    def test_trying_out_device_types_leaves_no_line_in_the_log(self):
        # The page asks while somebody types; "kelvin: 2" on the way to 2700
        # must not be a warning in the add-on's log with every key press.
        sections = {"lights": [{"where": "12", "tunable_white": True, "min_kelvin": 2}],
                    "switches": [{"where": "14", "device_class": "toaster"}],
                    "covers": [{"where": "21", "tilt_start": "halb"}]}
        package = logging.getLogger(base_module.__name__.rsplit(".", 1)[0])
        handlers = list(package.handlers)
        with self.assertNoLogs(level="DEBUG"):
            self.assertTrue(check_devices(sections))
        self.assertTrue(package.propagate)
        self.assertEqual(package.handlers, handlers)

    def test_real_entities_log_as_before_afterwards(self):
        check_devices(GOOD)
        with self.assertLogs(base_module._LOGGER, "WARNING"):
            base_module.device_class("switch", "toaster", "Test")

    def test_the_log_is_back_even_when_the_check_fails(self):
        package = logging.getLogger(base_module.__name__.rsplit(".", 1)[0])
        with self.assertRaises(ValueError):
            check_devices({"lights": [{"where": "12"}], "covers": "kaputt"})
        self.assertTrue(package.propagate)
        self.assertEqual(registry_module.FACTORIES["lights"].section, "lights")


if __name__ == "__main__":
    unittest.main()
