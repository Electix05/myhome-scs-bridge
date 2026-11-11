"""What the web interface shows next to a row, and the addresses it leaves alone."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from myhome_scs_mqtt.app.bridge import registry as registry_module
from myhome_scs_mqtt.app.bridge.live import TESTABLE, device_key, state_text
from myhome_scs_mqtt.app.bridge.registry import FACTORIES, load_ignored, save_ignored

from .helpers import Recorder, frame


def make(section: str, **config):
    return FACTORIES[section](Recorder().context(), config)


def heard(entity, *frames: str):
    """Hand frames to an entity the way the bridge does."""
    for raw in frames:
        entity.heard = True
        entity.on_own(frame(raw))
    return entity


class TestStateText(unittest.TestCase):
    def test_nothing_is_shown_before_the_bus_said_something(self):
        # "off" and "open" are only what the entity was created with.
        for section, where in (("lights", "12"), ("switches", "14"), ("covers", "21"),
                               ("climate", "2"), ("energy", "51"), ("pumps", "3"),
                               ("motion_sensors", "1010")):
            with self.subTest(section=section):
                self.assertEqual(state_text(make(section, where=where)), "")
        self.assertEqual(state_text(make("binary_sensors", where="1", schliesser=True)), "")

    def test_lights_and_switches(self):
        lamp = heard(make("lights", where="12"), "*1*1*12##")
        self.assertEqual(state_text(lamp), "an")
        self.assertEqual(state_text(heard(lamp, "*1*0*12##")), "aus")
        dimmer = heard(make("lights", where="13", dimmer=True), "*#1*13*1*140*2##")
        self.assertEqual(state_text(dimmer), "an · 40 %")
        self.assertEqual(state_text(heard(make("switches", where="14"), "*1*1*14##")), "an")

    def test_shutters(self):
        shutter = heard(make("covers", where="21"), "*2*1*21##")
        self.assertEqual(state_text(shutter), "öffnet")
        reporting = heard(make("covers", where="22"), "*#2*22*10*10*40*001*0##")
        self.assertEqual(state_text(reporting), "offen · 40 %")
        # While it travels the number is where it started from - not shown.
        self.assertEqual(state_text(heard(reporting, "*2*2*22##")), "schließt")

    def test_heating_meters_and_sensors(self):
        zone = heard(make("climate", where="2"), "*#4*2*0*0215##")
        self.assertEqual(state_text(zone), "21.5 °C · Soll 20.0 °C")
        meter = heard(make("energy", where="51"), "*#18*51*113*0000850##")
        self.assertEqual(state_text(meter), "850 W")
        sensor = heard(make("motion_sensors", where="1010", motion=False), "*#1*1010*6*120##")
        self.assertEqual(state_text(sensor), "120 lx")

    def test_contacts(self):
        contact = make("binary_sensors", where="1", schliesser=True)
        self.assertEqual(state_text(heard(contact, "*25*32#1*31##")), "ausgelöst")
        self.assertEqual(state_text(heard(contact, "*25*31#1*31##")), "in Ruhe")
        self.assertEqual(state_text(make("binary_sensors", where="7")), "nicht zugeordnet")

    def test_a_silenced_address_says_so_whatever_it_was(self):
        lamp = heard(make("lights", where="12"), "*1*1*12##")
        lamp.silenced = True
        self.assertEqual(state_text(lamp), "stillgelegt")

    def test_keypads_and_scenarios_have_no_state(self):
        self.assertEqual(state_text(heard(make("buttons", where="22"), "*25*21#1*22##")), "")
        self.assertEqual(state_text(make("scenes", where="01")), "")

    def test_the_key_is_the_row_of_the_page(self):
        # A zone written "#2" is saved - and shown - as "2".
        self.assertEqual(device_key(make("climate", where="#2")), "climate|2")
        self.assertEqual(device_key(make("lights", where="0211")), "lights|0211")

    def test_only_what_merely_switches_can_be_switched_by_hand(self):
        self.assertEqual(sorted(TESTABLE), ["lights", "switches"])


class TestIgnoredFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "ignored.json")

    def test_roundtrip(self):
        self.assertTrue(save_ignored(self.path, {"lights|77", "binary_sensors|12"}))
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), ["binary_sensors|12", "lights|77"])
        self.assertEqual(load_ignored(self.path), {"lights|77", "binary_sensors|12"})

    def test_a_missing_file_ignores_nothing(self):
        self.assertEqual(load_ignored(self.path), set())

    def test_a_broken_file_is_a_warning_and_ignores_nothing(self):
        for text in ("{broken", '{"lights|77": true}'):
            with self.subTest(text=text):
                with open(self.path, "w", encoding="utf-8") as handle:
                    handle.write(text)
                with self.assertLogs(registry_module._LOGGER, "WARNING"):
                    self.assertEqual(load_ignored(self.path), set())

    def test_what_is_no_address_is_left_out(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(["lights|77", 12, "ohne Trenner", None], handle)
        self.assertEqual(load_ignored(self.path), {"lights|77"})

    def test_a_write_failure_is_reported_not_raised(self):
        # A file where the folder should be: the write cannot work.
        blocker = os.path.join(self._tmp.name, "file")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        with self.assertLogs(registry_module._LOGGER, "WARNING"):
            self.assertFalse(save_ignored(os.path.join(blocker, "ignored.json"), set()))


if __name__ == "__main__":
    unittest.main()
