"""The registry: entities from the device file and the discovery memory, routing."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from myhome_scs_mqtt.app.bridge import registry as registry_module
from myhome_scs_mqtt.app.bridge.registry import (
    FACTORIES, Registry, load_discovered, save_discovered,
)
from myhome_scs_mqtt.app.devices.sensors import BridgeStatus
from myhome_scs_mqtt.app.own import messages as msg
from myhome_scs_mqtt.app.scan import Classified, classify

from .helpers import Recorder, frame


def build(definitions: dict, discovered: list | None = None, **kwargs) -> Registry:
    registry = Registry(Recorder().context(), **kwargs)
    registry.build(definitions, discovered or [])
    return registry


class TestBuild(unittest.TestCase):
    def test_every_section_has_a_factory(self):
        self.assertEqual(sorted(FACTORIES),
                         ["binary_sensors", "buttons", "climate", "covers", "energy",
                          "lights", "motion_sensors", "pumps", "scenes", "switches"])

    def test_configured_entities(self):
        registry = build({"lights": [{"where": "12"}], "covers": [{"where": "21"}]})
        self.assertEqual([(e.section, e.where) for e in registry.entities],
                         [("lights", "12"), ("covers", "21")])
        self.assertEqual(registry.discovered, [])

    def test_unknown_sections_and_broken_entries_do_not_stop_the_rest(self):
        with self.assertLogs(registry_module._LOGGER, "WARNING") as logs:
            registry = build({"alarms": [{"where": "1"}],
                              "covers": [{"where": "21", "travel_time": "fast"},
                                         {"where": "22"}]})
        self.assertEqual([e.where for e in registry.entities], ["22"])
        messages = " ".join(r.getMessage() for r in logs.records)
        self.assertIn("alarms", messages)
        self.assertIn("covers 21", messages)

    def test_what_could_not_be_read_is_reported_as_a_problem(self):
        # The bridge removes from Home Assistant what it does not know.  A
        # mistyped section name ("ligths") or a value a device type cannot
        # read leaves a device out that still exists - the bridge has to learn
        # about that, or the typo deletes the entity.
        with self.assertLogs(registry_module._LOGGER, "WARNING"):
            registry = build({"ligths": [{"where": "12"}, {"where": "13"}],
                              "covers": [{"where": "21", "travel_time": "fast"},
                                         {"where": "22"}]})
        self.assertEqual(len(registry.problems), 2)
        self.assertIn("ligths", registry.problems[0])
        self.assertIn("2 Einträge", registry.problems[0])
        self.assertIn("covers 21", registry.problems[1])
        self.assertEqual(registry.memory_problems, [])

    def test_a_readable_file_has_no_problems(self):
        with self.assertLogs(registry_module._LOGGER, "WARNING"):
            registry = build({"lights": [{"where": "12", "name": "A"},
                                         {"where": "12", "name": "B"}],    # twice: no loss
                              "alarms": []},                               # nothing in it
                             [{"section": "covers", "where": "33"}])
        self.assertEqual((registry.problems, registry.memory_problems), ([], []))

    def test_an_unreadable_memory_entry_is_a_problem_and_stays_remembered(self):
        # Dropped here, it would be gone from discovered.json with the next
        # write - and its entity from Home Assistant one start later.
        with self.assertLogs(registry_module._LOGGER, "ERROR"):
            registry = build({}, [{"section": "covers", "where": "33", "travel_time": "fast"},
                                  {"section": "alarms", "where": "1"},
                                  {"section": "lights", "where": "12"}])
        self.assertEqual([e.where for e in registry.entities], ["12"])
        # Not the device file's fault - saving from the page stays possible.
        self.assertEqual(registry.problems, [])
        self.assertEqual(len(registry.memory_problems), 2)
        self.assertIn("covers 33", registry.memory_problems[0])
        self.assertEqual([item["where"] for item in registry.discovered], ["33", "1", "12"])

    def test_an_address_defined_twice_is_used_once(self):
        with self.assertLogs(registry_module._LOGGER, "WARNING"):
            registry = build({"lights": [{"where": "12", "name": "A"},
                                         {"where": "12", "name": "B"}]})
        self.assertEqual([e.name for e in registry.entities], ["A"])

    def test_the_device_file_wins_over_the_discovery_memory(self):
        registry = build(
            {"lights": [{"where": "12", "name": "Kitchen"}]},
            [{"section": "lights", "where": "12"}, {"section": "covers", "where": "33"}],
        )
        self.assertEqual([(e.section, e.where, e.discovered) for e in registry.entities],
                         [("lights", "12", False), ("covers", "33", True)])
        self.assertEqual(registry.discovered, [{"section": "covers", "where": "33"}])

    def test_contacts_and_pumps_are_not_created_twice(self):
        # Their bus address is not their "where": contact 1 is 31 on the bus,
        # pump 3 is 0#3.  The old lookup by (who, where) missed both.
        registry = build(
            {"binary_sensors": [{"where": "1", "schliesser": True}],
             "pumps": [{"where": "3"}]},
            [{"section": "binary_sensors", "where": "1"}, {"section": "pumps", "where": "3"}],
        )
        self.assertEqual(len(registry.entities), 2)
        self.assertEqual(registry.discovered, [])

    def test_the_same_number_on_different_bus_addresses_is_no_duplicate(self):
        # Actuator 1 of zone 8 (8#1) and of zone 9 (9#1); contact 1 (31) and
        # IR interface 1 (41) - all written "where: 1", four devices.
        with self.assertNoLogs(registry_module._LOGGER, "WARNING"):
            registry = build({
                "pumps": [{"where": "1", "zone": "8"}, {"where": "1", "zone": "9"}],
                "binary_sensors": [{"where": "1", "schliesser": True},
                                   {"where": "1", "bus_prefix": "4", "schliesser": True}],
            })
        entities = registry.entities
        self.assertEqual(len(entities), 4)
        self.assertEqual(len({e.object_id for e in entities}), 4, "unique ids collide")
        self.assertEqual(len({e.state_topic for e in entities}), 4, "state topics collide")
        self.assertEqual([e.zone for e in registry.route(frame("*#4*9#1*20*1##"))], ["9"])
        self.assertEqual([e.bus_where for e in registry.route(frame("*25*32#1*41##"))], ["41"])

    def test_the_same_bus_address_twice_is_still_a_duplicate(self):
        with self.assertLogs(registry_module._LOGGER, "WARNING"):
            registry = build({"pumps": [{"where": "1", "zone": "8"},
                                        {"where": "1", "zone": "8", "name": "again"}],
                              "binary_sensors": [{"where": "1", "bus_prefix": "4"},
                                                 {"where": "1", "bus_prefix": 4}]})
        self.assertEqual(len(registry.entities), 2)

    def test_a_switch_and_a_light_at_one_address_are_one_actuator(self):
        registry = build({"switches": [{"where": "12"}]}, [{"section": "lights", "where": "12"}])
        self.assertEqual([e.section for e in registry.entities], ["switches"])

    def test_a_presence_sensor_may_share_the_address_of_a_light(self):
        registry = build({"motion_sensors": [{"where": "12"}]},
                         [{"section": "lights", "where": "12"}])
        self.assertEqual(sorted(e.section for e in registry.entities),
                         ["lights", "motion_sensors"])

    def test_device_entities_and_ticking(self):
        registry = build({"lights": [{"where": "12"}], "covers": [{"where": "21"}],
                          "motion_sensors": [{"where": "1010"}]})
        status = BridgeStatus(registry.context, {"where": "192.0.2.10"})
        registry.register(status)
        self.assertNotIn(status, registry.device_entities())
        # The status ticks, too: it looks at the bus counters now and then.
        self.assertEqual(sorted(e.section for e in registry.ticking()),
                         ["", "covers", "motion_sensors"])
        self.assertIn(status, registry.ticking())
        self.assertTrue(registry.known("covers", "21"))
        self.assertFalse(registry.known("covers", "12"))


class TestRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build({
            "lights": [{"where": "12"}, {"where": "41#4#01"}],
            "motion_sensors": [{"where": "12"}],
            "climate": [{"where": "8"}],
            "pumps": [{"where": "1", "zone": "8"}, {"where": "3"}],
            "binary_sensors": [{"where": "99", "oeffner": True}],
        })

    def sections(self, raw: str) -> list[str]:
        return [entity.section for entity in self.registry.route(frame(raw))]

    def test_plain_addresses(self):
        self.assertEqual(self.sections("*1*1*12##"), ["lights", "motion_sensors"])
        self.assertEqual(self.sections("*25*31#0*399##"), ["binary_sensors"])
        self.assertEqual(self.sections("*1*1*13##"), [])
        self.assertEqual(self.sections("*2*1*12##"), [])       # another WHO

    def test_addresses_with_suffix(self):
        self.assertEqual(self.sections("*1*1*41#4#01##"), ["lights"])
        self.assertEqual(self.sections("*#4*0#3*20*1##"), ["pumps"])

    def test_a_zone_actuator_reaches_the_actuator_and_the_zone(self):
        # "8#1" is routed by its full address first, then by the zone.
        self.assertEqual(self.sections("*#4*8#1*20*5##"), ["pumps", "climate"])

    def test_both_zone_address_forms(self):
        self.assertEqual(self.sections("*4*303*8##"), ["climate"])
        self.assertEqual(self.sections("*4*303*#8##"), ["climate"])

    def test_command_topics(self):
        light = self.registry.by_topic("myhome/light/12/set")
        self.assertEqual((light.section, light.where), ("lights", "12"))
        self.assertIsNone(self.registry.by_topic("myhome/light/99/set"))
        self.assertIn("myhome/climate/8/setpoint/set", self.registry.topics)

    def test_add_topics_reports_only_new_ones(self):
        light = self.registry.by_topic("myhome/light/12/set")
        self.assertEqual(self.registry.add_topics(light), [])


class TestAutoDiscovery(unittest.TestCase):
    def test_creates_and_remembers(self):
        registry = build({})
        entity = registry.auto_create(classify(msg.parse("*1*5*44##")))
        self.assertEqual((entity.section, entity.where, entity.dimmer), ("lights", "44", True))
        self.assertTrue(entity.discovered)
        self.assertEqual(registry.discovered,
                         [{"dimmer": True, "section": "lights", "where": "44"}])
        # Commands for it are routed at once.
        self.assertIs(registry.by_topic("myhome/light/44/set"), entity)

    def test_no_duplicates_and_no_unknown_sections(self):
        registry = build({"binary_sensors": [{"where": "99", "oeffner": True}]})
        self.assertIsNone(registry.auto_create(Classified("binary_sensors", "99", {})))
        self.assertIsNone(registry.auto_create(Classified("alarms", "1", {})))
        self.assertEqual(registry.discovered, [])

    def test_the_number_of_automatic_entities_is_limited(self):
        registry = build({}, max_auto_entities=3)
        with self.assertLogs(registry_module._LOGGER, "WARNING") as logs:
            for where in range(20, 30):
                registry.auto_create(Classified("covers", str(where), {}))
        self.assertEqual(len(registry.discovered), 3)
        self.assertEqual(len(logs.records), 1, "the limit is reported once")

    def test_what_an_entity_learns_is_remembered(self):
        registry = build({}, [{"section": "lights", "where": "44"}])
        lamp = registry.entities[0]
        lamp.on_own(frame("*#1*44*1*167*2##"))            # it turns out to be a dimmer
        self.assertTrue(registry.remember(lamp))
        self.assertTrue(registry.discovered[0]["dimmer"])
        self.assertEqual(registry.discovered[0]["section"], "lights")
        other = build({"lights": [{"where": "12"}]}).entities[0]
        self.assertFalse(registry.remember(other))

    def test_forget(self):
        registry = build({}, [{"section": "covers", "where": "33"}])
        registry.forget_discovered()
        self.assertEqual(registry.discovered, [])

    def test_forgetting_some_keeps_what_was_found_meanwhile(self):
        # The web interface writes the device file in a worker thread.  A
        # device that shows up on the bus meanwhile is not in that file.
        registry = build({}, [{"section": "covers", "where": "33"}])
        written = list(registry.discovered)
        late = registry.auto_create(Classified("lights", "44", {}))
        registry.forget_discovered(written)
        self.assertEqual(registry.discovered, [{"section": "lights", "where": "44"}])
        self.assertTrue(registry.is_remembered(late))
        self.assertFalse(registry.is_remembered(registry.entities[0]))


class TestDiscoveredFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "discovered.json")

    def write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_roundtrip(self):
        items = [{"section": "covers", "where": "04"}, {"section": "lights", "where": "12",
                                                         "dimmer": True}]
        self.assertTrue(save_discovered(self.path, items))
        self.assertEqual(load_discovered(self.path), (items, True))

    def test_a_missing_file_is_a_fresh_start(self):
        self.assertEqual(load_discovered(self.path), ([], True))

    def test_a_broken_file_is_not_complete(self):
        for text in ("{broken", '{"section": "lights"}'):
            with self.subTest(text=text):
                self.write(text)
                with self.assertLogs(registry_module._LOGGER, "WARNING"):
                    self.assertEqual(load_discovered(self.path), ([], False))

    def test_unreadable_entries_are_dropped_and_reported(self):
        self.write(json.dumps([{"section": "covers", "where": "33"}, {"section": "lights"},
                               "text", {"section": "lights", "where": 12}]))
        with self.assertLogs(registry_module._LOGGER, "WARNING"):
            items, complete = load_discovered(self.path)
        self.assertEqual(items, [{"section": "covers", "where": "33"}])
        self.assertFalse(complete)

    def test_a_write_failure_is_reported_not_raised(self):
        blocked = os.path.join(self._tmp.name, "file")
        self.write("x")
        os.replace(self.path, blocked)
        with self.assertLogs(registry_module._LOGGER, "WARNING"):
            self.assertFalse(save_discovered(os.path.join(blocked, "discovered.json"), []))


if __name__ == "__main__":
    unittest.main()
