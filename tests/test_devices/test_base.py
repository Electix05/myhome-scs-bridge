"""The entity contract every device type builds on."""

from __future__ import annotations

import unittest

from myhome_scs_mqtt.app import __version__
from myhome_scs_mqtt.app.devices import base
from myhome_scs_mqtt.app.devices.base import Entity, device_class, slug
from myhome_scs_mqtt.app.devices.climate import Climate, ThermoActuator
from myhome_scs_mqtt.app.devices.cover import Cover
from myhome_scs_mqtt.app.devices.events import DryContact, Keypad, Scenario
from myhome_scs_mqtt.app.devices.light import Light, Switch
from myhome_scs_mqtt.app.devices.lightmgmt import BmseSensor
from myhome_scs_mqtt.app.devices.sensors import EnergyMeter

from ..helpers import Recorder


class Plain(Entity):
    section = "plain"
    topic_group = "plain"

    def discovery(self):
        return [(self.component, self.object_id, self.base_payload())]


class TestHelpers(unittest.TestCase):
    def test_slug_keeps_addresses_readable_and_distinct(self):
        self.assertEqual(slug("#3"), "grp_3")
        self.assertEqual(slug("3"), "3")
        self.assertEqual(slug("12#4#01"), "12_4_01")
        self.assertEqual(slug("04"), "04")
        self.assertEqual(slug(""), "0")

    def test_device_class_passes_known_and_drops_unknown(self):
        self.assertEqual(device_class("binary_sensor", "Door"), "door")
        self.assertEqual(device_class("cover", "blind"), "blind")
        self.assertEqual(device_class("sensor", "anything"), "anything")
        self.assertEqual(device_class("switch", ""), "")
        with self.assertLogs(base._LOGGER, "WARNING"):
            self.assertEqual(device_class("binary_sensor", "tür", "Haustür"), "")

    def test_is_number(self):
        self.assertTrue(base.is_number("0210"))
        self.assertFalse(base.is_number("²"))
        self.assertFalse(base.is_number(""))
        self.assertFalse(base.is_number("-1"))


class TestEntity(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = Recorder()
        self.ctx = self.recorder.context()

    def test_identity_and_topics(self):
        entity = Plain(self.ctx, {"where": "04", "name": "Office"})
        self.assertEqual(entity.where, "04")
        self.assertEqual(entity.object_id, "scs_bridge_1_04")
        self.assertEqual(entity.topic_root, "myhome/plain/04")
        self.assertEqual(entity.state_topic, "myhome/plain/04/state")
        self.assertEqual(entity.command_topic, "myhome/plain/04/set")
        self.assertEqual(entity.name_topic, "myhome/plain/04/name")

    def test_address_given_as_number_still_becomes_text(self):
        entity = Plain(self.ctx, {"where": 12})
        self.assertEqual(entity.where, "12")
        self.assertEqual(entity.name, "MyHome device 12")

    def test_an_entry_without_address_is_refused(self):
        for config in ({}, {"where": ""}, {"where": "  "}, {"where": None}):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    Plain(self.ctx, config)

    def test_the_callers_dict_is_not_shared(self):
        config = {"where": "12", "name": "Office"}
        entity = Plain(self.ctx, config)
        config["name"] = "Changed"
        self.assertEqual(entity.to_config()["name"], "Office")

    def test_to_config_keeps_unknown_fields_and_drops_internal_ones(self):
        entity = Plain(self.ctx, {"where": "12", "custom": [1, 2], "section": "plain",
                                  "discovered": True})
        config = entity.to_config()
        self.assertEqual(config["custom"], [1, 2])
        self.assertNotIn("section", config)
        self.assertNotIn("discovered", config)
        self.assertTrue(entity.discovered)

    def test_availability_needs_the_add_on_the_link_and_the_address(self):
        payload = Plain(self.ctx, {"where": "12"}).discovery()[0][2]
        self.assertNotIn("availability_topic", payload)
        self.assertEqual(payload["availability_mode"], "all")
        self.assertEqual([item["topic"] for item in payload["availability"]],
                         ["myhome/bridge/lwt", "myhome/bridge/availability",
                          "myhome/plain/12/reachable"])
        for item in payload["availability"]:
            self.assertEqual((item["payload_available"], item["payload_not_available"]),
                             ("online", "offline"))

    def test_a_single_availability_topic_stays_simple(self):
        # Without a section it is no device of the installation (the add-on's
        # own diagnostics): no reachability of its own, so one topic is left.
        self.ctx.bridge_availability = ""

        class Diagnostic(Plain):
            section = ""

        payload = Diagnostic(self.ctx, {"where": "12"}).discovery()[0][2]
        self.assertEqual(payload["availability_topic"], "myhome/bridge/availability")
        self.assertNotIn("availability", payload)

    def test_base_payload(self):
        entity = Plain(self.ctx, {"where": "12", "name": "Office"})
        main = entity.base_payload()
        self.assertIsNone(main["name"])
        self.assertEqual(main["unique_id"], "scs_bridge_1_12")
        self.assertEqual(main["origin"], {"name": "MyHome SCS Bridge",
                                          "sw_version": __version__})
        self.assertEqual(main["device"]["via_device"], "scs_bridge")
        extra = entity.base_payload("lux", name="Brightness")
        self.assertEqual((extra["unique_id"], extra["name"]),
                         ("scs_bridge_1_12_lux", "Brightness"))

    def test_ids_never_collide_with_the_old_add_on(self):
        # The old add-on (myhome_scs_mqtt) uses "myhome_scs_bridge_..." ids and
        # clears every retained discovery topic that contains that text.
        entity = Plain(self.ctx, {"where": "12"})
        for text in (entity.object_id, entity.base_payload()["unique_id"]):
            self.assertFalse(text.startswith("myhome_scs_bridge"), text)
            self.assertNotIn("myhome_scs_bridge", text)

    def test_defaults_of_the_contract(self):
        entity = Plain(self.ctx, {"where": "12"})
        self.assertFalse(entity.needs_tick)
        self.assertTrue(entity.exclusive_address)
        self.assertTrue(entity.configured)
        for method in (entity.subscriptions, entity.poll_frames, entity.probe_frames,
                       entity.followup_frames):
            self.assertEqual(tuple(method()), ())
        self.assertEqual(tuple(entity.match_keys()), ("12",))
        with self.assertRaises(NotImplementedError):
            Entity(self.ctx, {"where": "1"}).discovery()


class TestRoundTrip(unittest.TestCase):
    """The web interface reads devices through to_config() and writes them back.

    Anything that does not come out again is lost on the next save - min_temp
    was, until this test existed.
    """

    CASES = [
        (Light, {"where": "12", "name": "Küche", "dimmer": True, "dimmer_speed": 5}),
        (Switch, {"where": "9", "name": "Steckdose", "who": 9, "device_class": "outlet"}),
        (Cover, {"where": "21", "name": "Rollo", "travel_time": 25, "tilt_time": 1.0,
                 "invert_position": True, "device_class": "blind",
                 "position_control": "software"}),
        (Cover, {"where": "22", "name": "Fremder Aktor", "advanced": False,
                 "position_feedback": True}),
        (Climate, {"where": "2", "name": "Bad", "min_temp": 16, "max_temp": 24,
                   "temp_step": 0.5, "default_setpoint": 21, "heizen": True,
                   "kuehlen": True, "luefter": False}),
        (ThermoActuator, {"where": "3", "name": "Pumpe", "zone": "0",
                          "device_class": "running"}),
        (EnergyMeter, {"where": "51", "name": "Zähler", "energy_total": True,
                       "energy_today": True}),
        (BmseSensor, {"where": "1010", "name": "Melder", "off_delay": 10,
                      "motion_poll": 2, "lux_interval": 30, "illuminance": True,
                      "motion": True}),
        (DryContact, {"where": "31", "name": "Klingel", "device_class": "sound",
                      "oeffner": True, "schliesser": False}),
        (Keypad, {"where": "22", "name": "Taster", "kind": "cen", "buttons": [1, 2, 3]}),
        (Scenario, {"where": "01", "name": "Szenen", "scenarios": [1, 2],
                    "scenario_names": {1: "Alles aus"}}),
    ]

    def test_every_option_survives(self):
        ctx = Recorder().context()
        for factory, config in self.CASES:
            with self.subTest(factory=factory.__name__, where=config["where"]):
                written = factory(ctx, dict(config, unknown_field="kept")).to_config()
                for key, value in config.items():
                    self.assertEqual(written.get(key), value,
                                     f"{factory.__name__} verliert {key}")
                self.assertEqual(written["unknown_field"], "kept")
                self.assertNotIn("section", written)
                self.assertNotIn("discovered", written)
                # And what comes out builds the same entity again.
                again = factory(ctx, written).to_config()
                self.assertEqual(again, written)

    def test_every_type_announces_itself(self):
        ctx = Recorder().context()
        for factory, config in self.CASES:
            with self.subTest(factory=factory.__name__):
                entities = factory(ctx, dict(config)).discovery()
                self.assertTrue(entities)
                unique_ids = [payload["unique_id"] for _c, _o, payload in entities]
                self.assertEqual(len(unique_ids), len(set(unique_ids)))


class TestReachability(unittest.TestCase):
    """An address that keeps being refused is silenced instead of asked again.

    Measured on 24.09.2026: a single address that does not exist was refused in
    every round and slowed the whole installation down.
    """

    def setUp(self) -> None:
        self.recorder = Recorder()
        self.ctx = self.recorder.context()
        self.ctx.dead_after = 3
        self.entity = Plain(self.ctx, {"where": "12342"})
        self.topic = "myhome/plain/12342/reachable"

    def refuse(self, times: int) -> None:
        for _ in range(times):
            self.entity.note_refusal()

    def test_three_refusals_in_a_row_silence_the_address(self):
        with self.assertLogs(base._LOGGER, "WARNING") as logs:
            self.refuse(3)
        self.assertTrue(self.entity.silenced)
        self.assertFalse(self.entity.poll_due())
        self.assertEqual(self.recorder.last(self.topic), "offline")
        self.assertTrue(self.recorder.retained(self.topic))
        self.assertIn("12342", logs.output[0])

    def test_two_refusals_leave_it_alone(self):
        self.refuse(2)
        self.assertFalse(self.entity.silenced)
        self.assertTrue(self.entity.poll_due())

    def test_an_answer_in_between_clears_the_count(self):
        self.refuse(2)
        self.entity.note_alive()
        self.refuse(2)
        self.assertFalse(self.entity.silenced)

    def test_it_comes_back_as_soon_as_it_answers(self):
        with self.assertLogs(base._LOGGER, "WARNING"):
            self.refuse(3)
        with self.assertLogs(base._LOGGER, "INFO") as logs:
            self.entity.note_alive()
        self.assertFalse(self.entity.silenced)
        self.assertTrue(self.entity.poll_due())
        self.assertEqual(self.recorder.last(self.topic), "online")
        self.assertIn("antwortet wieder", logs.output[-1])

    def test_one_single_retry_every_ten_minutes(self):
        with self.assertLogs(base._LOGGER, "WARNING"):
            self.refuse(3)
        self.recorder.advance(599)
        self.assertFalse(self.entity.poll_due())
        self.recorder.advance(2)
        self.assertTrue(self.entity.poll_due())     # one retry ...
        self.entity.note_poll_attempt()             # ... booked once its frame is queued ...
        self.assertFalse(self.entity.poll_due())    # ... and no more
        self.assertTrue(self.entity.silenced)       # still silenced until it answers

    def test_asking_is_only_booked_when_a_frame_really_goes_out(self):
        # Otherwise a retry that never left (broken entity, full queue) would
        # cost the address another ten minutes.
        with self.assertLogs(base._LOGGER, "WARNING"):
            self.refuse(3)
        self.recorder.advance(601)
        self.assertTrue(self.entity.poll_due())
        self.assertTrue(self.entity.poll_due(), "merely asking must not book the retry")

    def test_further_refusals_while_silenced_stay_quiet(self):
        with self.assertLogs(base._LOGGER, "WARNING") as logs:
            self.refuse(10)
        self.assertEqual(len(logs.records), 1)

    def test_without_the_option_nothing_is_silenced(self):
        self.ctx.dead_after = 0
        self.refuse(20)
        self.assertFalse(self.entity.silenced)
        self.assertTrue(self.entity.poll_due())
        self.assertIsNone(self.recorder.last(self.topic))

    def test_the_add_ons_own_entities_have_no_reachability(self):
        class Diagnostic(Plain):
            section = ""

        entity = Diagnostic(self.ctx, {"where": "12"})
        entity.publish_reachability()
        self.assertEqual(self.recorder.published, [])


if __name__ == "__main__":
    unittest.main()
