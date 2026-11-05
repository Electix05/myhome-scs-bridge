"""Dry contacts, CEN/CEN+ keypads and scenario modules."""

from __future__ import annotations

import json
import unittest

from myhome_scs_mqtt.app.devices import events as events_module
from myhome_scs_mqtt.app.devices.events import DryContact, Keypad, Scenario

from ..helpers import Recorder, frame


class EventsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = Recorder()
        self.ctx = self.rec.context()


class TestDryContact(EventsTestCase):
    def test_make_and_break_contacts_behave_oppositely(self):
        make = DryContact(self.ctx, {"where": "1", "schliesser": True})
        brk = DryContact(self.ctx, {"where": "2", "oeffner": True})
        self.assertEqual((make.bus_where, brk.bus_where), ("31", "32"))
        # Measured: WHAT 32 when the contact closes, WHAT 31 when it opens.
        make.on_own(frame("*25*32#1*31##"))
        brk.on_own(frame("*25*32#1*32##"))
        self.assertEqual(self.rec.last("myhome/contact/1/state"), "ON")
        self.assertEqual(self.rec.last("myhome/contact/2/state"), "OFF")
        make.on_own(frame("*25*31#0*31##"))
        brk.on_own(frame("*25*31#0*32##"))
        self.assertEqual(self.rec.last("myhome/contact/1/state"), "OFF")
        self.assertEqual(self.rec.last("myhome/contact/2/state"), "ON")

    def test_nothing_is_published_before_the_bus_said_something(self):
        contact = DryContact(self.ctx, {"where": "1", "schliesser": True})
        contact.publish_state()
        self.assertEqual(self.rec.published, [])

    def test_an_unclassified_contact_stays_out(self):
        for config in ({"where": "7"}, {"where": "8", "schliesser": True, "oeffner": True}):
            with self.subTest(config=config):
                contact = DryContact(self.ctx, config)
                self.assertFalse(contact.configured)
                self.assertEqual(contact.discovery(), [])
                self.assertEqual(contact.poll_frames(), ())
                contact.on_own(frame(f"*25*32#1*3{config['where']}##"))
        self.assertEqual(self.rec.published, [])

    def test_ir_interface_and_older_spelling(self):
        contact = DryContact(self.ctx, {"where": "5", "bus_prefix": 4, "invert": True})
        self.assertEqual(contact.bus_where, "45")
        self.assertTrue(contact.break_contact)
        config = contact.to_config()
        self.assertEqual((config["schliesser"], config["oeffner"]), (False, True))
        self.assertNotIn("invert", config)
        self.assertEqual(config["bus_prefix"], 4)

    def test_contact_1_and_ir_interface_1_are_two_devices(self):
        contact = DryContact(self.ctx, {"where": "1", "schliesser": True})
        infrared = DryContact(self.ctx, {"where": "1", "bus_prefix": "4", "schliesser": True})
        self.assertEqual((contact.bus_where, infrared.bus_where), ("31", "41"))
        self.assertEqual((contact.object_id, infrared.object_id),
                         ("scs_bridge_25_1", "scs_bridge_25_4_1"))
        self.assertEqual((contact.state_topic, infrared.state_topic),
                         ("myhome/contact/1/state", "myhome/contact/4_1/state"))

    def test_device_class(self):
        good = DryContact(self.ctx, {"where": "6", "schliesser": True, "device_class": "door"})
        self.assertEqual(good.discovery()[0][2]["device_class"], "door")
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "WARNING"):
            bad = DryContact(self.ctx, {"where": "5", "schliesser": True,
                                        "device_class": "tür"})
        self.assertNotIn("device_class", bad.discovery()[0][2])
        # The file keeps what the user wrote.
        self.assertEqual(bad.to_config()["device_class"], "tür")

    def test_poll_frame_uses_the_bus_address(self):
        contact = DryContact(self.ctx, {"where": "99", "oeffner": True})
        self.assertEqual(contact.poll_frames(), ("*#25*399##",))


class TestKeypad(EventsTestCase):
    def test_cen_plus_press(self):
        keypad = Keypad(self.ctx, {"where": "22", "buttons": [1, 2]})
        self.assertEqual(keypad.who, "25")
        keypad.on_own(frame("*25*21#2*22##"))
        self.assertEqual(self.rec.last("myhome/keypad/22/button/2"),
                         json.dumps({"event_type": "press"}))
        self.assertEqual(self.rec.last("myhome/keypad/22/button/2/action"), "press")
        self.assertEqual(self.rec.last("myhome/keypad/22/button/2/press"), "press")
        self.assertEqual(self.rec.last("myhome/keypad/press"), "22/2")
        self.assertEqual(self.rec.last("myhome/keypad/last"), "22/2/press")

    def test_cen_actions(self):
        keypad = Keypad(self.ctx, {"where": "51", "kind": "cen", "buttons": [3]})
        self.assertEqual(keypad.who, "15")
        keypad.on_own(frame("*15*3*51##"))
        keypad.on_own(frame("*15*3#2*51##"))
        self.assertEqual(self.rec.last("myhome/keypad/51/button/3/action"), "long_press")
        self.assertEqual(self.rec.last("myhome/keypad/51/button/3/press"), "press")
        self.assertEqual(self.rec.last("myhome/keypad/51/button/3/long_press"), "long_press")
        self.assertIsNone(self.rec.last("myhome/keypad/51/button/3/release"))
        self.assertEqual(self.rec.last("myhome/keypad/long_press"), "51/3")

    def test_events_are_never_retained_and_payloads_stay_constant(self):
        keypad = Keypad(self.ctx, {"where": "22", "buttons": [1]})
        for _ in range(3):
            keypad.on_own(frame("*25*21#1*22##"))
        self.assertTrue(self.rec.published)
        for topic, _payload, retain in self.rec.published:
            self.assertFalse(retain, f"{topic} is retained but describes an event")
        presses = [p for t, p, _r in self.rec.published if t == "myhome/keypad/22/button/1/press"]
        self.assertEqual(presses, ["press"] * 3)

    def test_unconfigured_buttons_and_foreign_frames_are_ignored(self):
        keypad = Keypad(self.ctx, {"where": "22", "buttons": [1]})
        keypad.on_own(frame("*25*21#7*22##"))
        keypad.on_own(frame("*25*99#1*22##"))
        keypad.on_own(frame("*#25*22##"))
        self.assertEqual(self.rec.published, [])

    def test_one_event_entity_per_button(self):
        keypad = Keypad(self.ctx, {"where": "22", "buttons": [1, 2]})
        entities = keypad.discovery()
        self.assertEqual([oid for _c, oid, _p in entities],
                         ["scs_bridge_25_22_btn1", "scs_bridge_25_22_btn2"])
        payload = entities[0][2]
        self.assertEqual(payload["state_topic"], "myhome/keypad/22/button/1")
        self.assertEqual(payload["event_types"], events_module.EVENT_TYPES)

    def test_the_file_gets_no_who_it_did_not_have(self):
        self.assertNotIn("who", Keypad(self.ctx, {"where": "22"}).to_config())


class TestScenario(EventsTestCase):
    def test_scenes_send_the_scenario_number(self):
        module = Scenario(self.ctx, {"where": "01", "scenarios": [1, 2],
                                     "scenario_names": {1: "Alles aus"}})
        entities = module.discovery()
        self.assertEqual([payload["name"] for _c, _o, payload in entities],
                         ["Alles aus", "Scenario 2"])
        self.assertEqual(list(module.subscriptions()),
                         ["myhome/scene/01/scenario/1/set", "myhome/scene/01/scenario/2/set"])
        module.on_command("myhome/scene/01/scenario/2/set", "ON")
        module.on_command("myhome/scene/01/scenario/9/set", "ON")
        self.assertEqual(self.rec.sent, ["*0*2*01##"])

    def test_scenes_have_no_state(self):
        Scenario(self.ctx, {"where": "01"}).publish_state()
        self.assertEqual(self.rec.published, [])


if __name__ == "__main__":
    unittest.main()
