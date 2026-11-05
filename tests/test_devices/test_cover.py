"""WHO=2 shutters: position command, closed loop, slats and bus congestion."""

from __future__ import annotations

import unittest

from myhome_scs_mqtt.app.devices import cover as cover_module
from myhome_scs_mqtt.app.devices.cover import Cover

from ..helpers import Recorder, frame

UP, DOWN, STOP = "*2*1*21##", "*2*2*21##", "*2*0*21##"
ASK = "*#2*21*10##"


class CoverTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rec = Recorder()
        self.ctx = self.rec.context()

    def make(self, **config) -> Cover:
        return Cover(self.ctx, dict({"where": "21", "name": "Shutter"}, **config))

    def last(self, suffix: str) -> str | None:
        return self.rec.last(f"myhome/cover/21/{suffix}")


class TestDiscovery(CoverTestCase):
    def test_a_basic_actuator_has_no_slider_until_it_reports(self):
        cover = self.make()
        payload = cover.discovery()[0][2]
        self.assertEqual(payload["device_class"], "shutter")
        self.assertNotIn("position_topic", payload)
        self.assertNotIn("tilt_command_topic", payload)

        # Answer format observed on an F461: state 10, position 15.
        cover.on_own(frame("*#2*21*10*10*15*001*0##"))
        self.assertEqual(self.rec.rediscovered, [cover])
        payload = cover.discovery()[0][2]
        self.assertEqual(payload["position_topic"], "myhome/cover/21/position")
        self.assertEqual(payload["set_position_topic"], "myhome/cover/21/position/set")
        self.assertEqual(self.last("position"), "15")

    def test_position_control_off_and_native(self):
        self.assertNotIn("set_position_topic",
                         self.make(position_feedback=True, position_control="off")
                         .discovery()[0][2])
        self.assertIn("set_position_topic", self.make(position_control="native").discovery()[0][2])

    def test_slats_only_for_blinds(self):
        payload = self.make(where="23", tilt_time=1.0).discovery()[0][2]
        self.assertEqual(payload["tilt_command_topic"], "myhome/cover/23/tilt/set")
        self.assertEqual(payload["tilt_status_topic"], "myhome/cover/23/tilt")

    def test_unknown_device_class_is_dropped(self):
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "WARNING"):
            payload = self.make(device_class="Rollladen").discovery()[0][2]
        self.assertNotIn("device_class", payload)

    def test_followup_until_the_position_is_known(self):
        cover = self.make()
        self.assertEqual(cover.followup_frames(), (ASK,))
        self.assertEqual(cover.poll_frames(), ("*#2*21##", ASK))
        cover.on_own(frame("*#2*21*10*10*15*001*0##"))
        self.assertEqual(cover.followup_frames(), ())
        self.assertEqual(cover.poll_frames(), (ASK,))
        self.assertTrue(cover.to_config()["position_feedback"])


class TestNativePosition(CoverTestCase):
    def test_the_measured_command_form(self):
        # WISSEN.md: *#2*05*#11#001#1*50## - second "#1", no leading zeros.
        cover = Cover(self.ctx, {"where": "05"})
        cover.on_command(cover.set_position_topic, "50")
        cover.on_command(cover.set_position_topic, "5")
        cover.on_command(cover.set_position_topic, "0")
        cover.on_command(cover.set_position_topic, "100")
        self.assertEqual(self.rec.sent, ["*#2*05*#11#001#1*50##", "*#2*05*#11#001#1*5##",
                                         "*#2*05*#11#001#1*0##", "*#2*05*#11#001#1*100##"])

    def test_inverted_position(self):
        cover = self.make(invert_position=True)
        cover.set_position(30)
        self.assertEqual(self.rec.sent, ["*#2*21*#11#001#1*70##"])

    def test_a_native_drive_is_not_polled_and_ends_on_arrival(self):
        # Measured: 0.1 s command, 0.1 s "27 %, moving", 11.1 s "50 %, stopped".
        cover = self.make()
        cover.set_position(50)
        self.rec.clear()
        cover.on_own(frame("*#2*21*10*11*27*001*0##"))
        for _ in range(11):
            self.rec.advance(1.0)
            cover.tick()
        self.assertEqual(self.rec.sent, [], "a native drive was polled")
        cover.on_own(frame("*#2*21*10*10*50*001*0##"))
        cover.on_own(frame("*2*0*21##"))
        self.assertIsNone(cover._native_target)
        self.assertEqual(self.last("position"), "50")
        self.assertEqual(self.last("state"), "open")

    def test_an_actuator_that_ignores_the_command_is_driven_by_hand(self):
        # "Eine Quittung beweist nichts": no movement within 3 s -> closed loop.
        cover = self.make()
        cover.set_position(40)
        self.rec.clear()
        self.rec.advance(cover_module.NATIVE_GRACE - 0.5)
        cover.tick()
        self.assertEqual(self.rec.sent, [])
        self.rec.advance(1.0)
        with self.assertLogs(cover_module._LOGGER, "INFO"):
            cover.tick()
        self.assertEqual(self.rec.sent, [ASK])
        self.assertIsNone(cover._native_target)
        # The fresh position arrives and the closed loop starts the motor.
        cover.on_own(frame("*#2*21*10*10*80*001*0##"))
        self.assertEqual(self.rec.sent, [ASK, DOWN, ASK])

    def test_a_queued_command_is_given_time_to_leave(self):
        # The grace period counts from the moment the frame is on the bus.
        cover = self.make()
        cover.set_position(40)
        self.rec.depth = 9
        for _ in range(10):
            self.rec.advance(1.0)
            cover.tick()
        self.assertIsNotNone(cover._native_target, "the closed loop jumped in too early")
        self.rec.depth = 0
        self.rec.advance(cover_module.NATIVE_GRACE + 0.1)
        with self.assertLogs(cover_module._LOGGER, "INFO"):
            cover.tick()
        self.assertIsNone(cover._native_target)

    def test_a_queue_that_never_empties_has_an_outer_limit(self):
        cover = self.make()
        cover.set_position(40)
        self.rec.depth = 9
        self.rec.advance(cover_module.NATIVE_LIMIT - 1)
        cover.tick()                            # still waiting for the queue
        self.assertIsNotNone(cover._native_target)
        # Past the limit the queue no longer excuses silence: after one more
        # grace period the closed loop takes over, although frames still wait.
        self.rec.advance(cover_module.NATIVE_GRACE + 1)
        with self.assertLogs(cover_module._LOGGER, "INFO"):
            cover.tick()
        self.assertIsNone(cover._native_target)

    def test_reversing_does_not_end_the_drive(self):
        cover = self.make()
        cover.set_position(60)
        cover.on_own(frame("*#2*21*10*10*22*001*0##"))    # short halt to turn round
        cover.on_own(frame("*2*0*21##"))
        self.assertIsNotNone(cover._native_target)
        cover.on_own(frame("*2*1*21##"))
        self.rec.clear()
        for _ in range(3):
            self.rec.advance(1.0)
            cover.tick()
        self.assertEqual(self.rec.sent, [])
        cover.on_own(frame("*#2*21*10*10*60*001*0##"))
        cover.on_own(frame("*2*0*21##"))
        self.assertIsNone(cover._native_target)


class TestClosedLoop(CoverTestCase):
    def make_software(self) -> Cover:
        cover = self.make(position_control="software")
        cover.on_own(frame("*#2*21*10*10*15*001*0##"))
        self.rec.clear()
        return cover

    def test_the_direction_comes_from_a_fresh_position(self):
        cover = self.make_software()
        cover.set_position(40)
        self.assertEqual(self.rec.sent, [ASK], "must ask before choosing the direction")
        cover.on_own(frame("*#2*21*10*10*65*001*0##"))   # somebody moved it meanwhile
        self.assertEqual(self.rec.sent, [ASK, DOWN, ASK])

    def test_polls_while_driving_and_stops_on_target(self):
        cover = self.make_software()
        cover.set_position(40)
        cover.on_own(frame("*#2*21*10*10*15*001*0##"))
        self.assertEqual(self.rec.sent, [ASK, UP, ASK])
        for position in ("20", "28", "36"):
            self.rec.advance(1.0)
            cover.tick()
            cover.on_own(frame(f"*#2*21*10*11*{position}*001*0##"))
        self.assertNotIn(STOP, self.rec.sent)
        self.rec.advance(1.0)
        cover.tick()
        cover.on_own(frame("*#2*21*10*11*39*001*0##"))
        self.assertEqual(self.rec.sent[-1], STOP)
        self.assertIsNone(cover._target)

    def test_end_positions_cost_one_frame(self):
        cover = self.make_software()
        cover.set_position(100)
        cover.set_position(0)
        self.assertEqual(self.rec.sent, [UP, DOWN])

    def test_a_silent_gateway_uses_the_stored_position(self):
        cover = self.make_software()
        cover.set_position(40)
        self.rec.advance(cover_module.PENDING_TIMEOUT + 0.1)
        cover.tick()
        self.assertEqual(self.rec.sent[:3], [ASK, UP, ASK])

    def test_a_busy_bus_does_not_end_a_drive(self):
        # Thirty shutters at once fill the queue for a good ten seconds; a
        # skipped poll is no missing answer.  Measured before the fix: stopped
        # at 90 instead of 50 %.
        cover = self.make_software()
        cover.set_position(40)
        cover.on_own(frame("*#2*21*10*10*15*001*0##"))
        self.rec.clear()
        self.rec.depth = cover_module.MAX_QUEUE_FOR_POLLING + 1
        for _ in range(int(cover_module.STALL_TIMEOUT) + 5):
            self.rec.advance(1.0)
            cover.tick()
        self.assertEqual(self.rec.sent, [], "polled although the bus is congested")
        self.assertIsNotNone(cover._target, "the drive was given up for a busy bus")

    def test_a_really_stalled_drive_is_stopped_on_a_free_bus(self):
        cover = self.make_software()
        cover.set_position(40)
        cover.on_own(frame("*#2*21*10*10*15*001*0##"))
        with self.assertLogs(cover_module._LOGGER, "WARNING"):
            for _ in range(int(cover_module.STALL_TIMEOUT) + 2):
                self.rec.advance(1.0)
                cover.tick()
        self.assertIsNone(cover._target)
        self.assertEqual(self.rec.sent[-1], STOP)

    def test_even_a_busy_bus_ends_after_the_drive_timeout(self):
        cover = self.make_software()
        cover.set_position(40)
        cover.on_own(frame("*#2*21*10*10*15*001*0##"))
        self.rec.depth = cover_module.MAX_QUEUE_FOR_POLLING + 1
        self.rec.advance(cover_module.DRIVE_TIMEOUT + 1)
        with self.assertLogs(cover_module._LOGGER, "WARNING"):
            cover.tick()
        self.assertIsNone(cover._target)

    def test_a_manual_command_cancels_a_running_drive(self):
        cover = self.make_software()
        cover.set_position(20)
        cover.on_own(frame("*#2*21*10*10*80*001*0##"))
        cover.on_command(cover.command_topic, "STOP")
        self.assertIsNone(cover._target)
        stops = self.rec.sent.count(STOP)
        cover.on_own(frame("*#2*21*10*11*21*001*0##"))
        self.assertEqual(self.rec.sent.count(STOP), stops, "a second stop was sent")


class TestTracking(CoverTestCase):
    def test_movement_started_elsewhere_is_followed(self):
        cover = self.make()
        cover.on_own(frame("*2*1*21##"))            # wall switch: moving up
        self.assertEqual(self.last("state"), "opening")
        self.rec.advance(1.0)
        cover.tick()
        self.assertEqual(self.rec.sent, [ASK])
        cover.on_own(frame("*#2*21*10*11*30*001*0##"))
        self.assertEqual(self.last("position"), "30")
        cover.on_own(frame("*#2*21*10*10*35*001*0##"))
        self.rec.clear()
        self.rec.advance(1.0)
        cover.tick()
        self.assertEqual(self.rec.sent, [], "still polling after the stop")

    def test_polling_backs_off_when_the_bus_is_busy(self):
        cover = self.make(where="77")
        cover.on_own(frame("*2*1*77##"))
        self.rec.depth = cover_module.MAX_QUEUE_FOR_POLLING + 1
        cover.tick()
        self.assertEqual(self.rec.sent, [])
        self.rec.depth = 0
        cover.tick()
        self.assertEqual(self.rec.sent, ["*#2*77*10##"])

    def test_tracking_gives_up_eventually(self):
        cover = self.make()
        cover.on_own(frame("*2*2*21##"))
        self.rec.advance(cover_module.TRACKING_TIMEOUT + 1)
        cover.tick()
        self.rec.clear()
        cover.tick()
        self.assertEqual(self.rec.sent, [])

    def test_state_codes_of_dimension_10(self):
        cover = self.make()
        for position in ("03", "10", "17"):
            cover.on_own(frame(f"*#2*21*10*11*{position}*001*0##"))
        self.assertEqual((self.last("state"), self.last("position")), ("opening", "17"))
        cover.on_own(frame("*#2*21*10*10*21*001*0##"))
        self.assertEqual((self.last("state"), self.last("position")), ("open", "21"))
        cover.on_own(frame("*#2*21*10*12*05*001*0##"))
        self.assertEqual(self.last("state"), "closing")
        cover.on_own(frame("*#2*21*10*10*0*001*0##"))
        self.assertEqual(self.last("state"), "closed")


class TestSlats(CoverTestCase):
    def blind(self) -> Cover:
        return Cover(self.ctx, {"where": "23", "tilt_time": 1.0})

    def test_slats_are_turned_with_a_short_impulse(self):
        blind = self.blind()
        blind.on_command(blind.set_tilt_topic, "100")      # 50 -> 100: half a turn
        self.assertEqual(self.rec.sent, ["*2*1*23##"])
        self.assertEqual(self.rec.last("myhome/cover/23/tilt"), "100")
        self.rec.advance(0.4)
        self.assertEqual(self.rec.sent, ["*2*1*23##"])
        self.rec.advance(0.2)
        self.assertEqual(self.rec.sent, ["*2*1*23##", "*2*0*23##"])
        self.rec.advance(cover_module.TILT_SETTLE + 0.1)
        self.assertEqual(self.rec.sent[-1], "*#2*23*10##")

    def test_the_stop_button_of_the_slat_row_stops(self):
        blind = self.blind()
        blind.on_command(blind.set_tilt_topic, "STOP")
        self.assertEqual(self.rec.sent, ["*2*0*23##"])

    def test_an_overtaken_impulse_does_not_stop_the_next_command(self):
        blind = self.blind()
        blind.on_command(blind.set_tilt_topic, "100")
        blind.on_command(blind.command_topic, "OPEN")
        self.rec.advance(2.0)
        self.assertEqual(self.rec.sent, ["*2*1*23##", "*2*1*23##"])

    def test_a_tilt_impulse_is_not_reported_as_travel(self):
        blind = self.blind()
        blind.on_command(blind.set_tilt_topic, "0")
        blind.on_own(frame("*2*2*23##"))                   # the echo of the impulse
        self.assertNotEqual(self.rec.last("myhome/cover/23/state"), "closing")

    def test_travelling_puts_the_slats_in_their_end_position(self):
        blind = self.blind()
        blind.on_own(frame("*2*2*23##"))
        self.assertEqual(self.rec.last("myhome/cover/23/tilt"), "0")
        blind.on_own(frame("*2*1*23##"))
        self.assertEqual(self.rec.last("myhome/cover/23/tilt"), "100")

    def test_a_tiny_change_needs_no_impulse(self):
        blind = self.blind()
        blind.set_tilt(55)
        self.assertEqual(self.rec.sent, [])
        self.assertEqual(self.rec.last("myhome/cover/23/tilt"), "55")


class TestBasicActuator(CoverTestCase):
    def test_without_feedback_a_full_travel_is_assumed(self):
        cover = self.make()
        cover.on_own(frame("*2*1*21##"))
        cover.on_own(frame("*2*0*21##"))
        self.assertEqual(self.last("state"), "open")
        cover.on_own(frame("*2*2*21##"))
        cover.on_own(frame("*2*0*21##"))
        self.assertEqual(self.last("state"), "closed")

    def test_travel_time_estimates_the_position(self):
        cover = self.make(travel_time=20)
        self.assertEqual(cover.position, 50)
        cover.on_own(frame("*2*1*21##"))
        self.rec.advance(5.0)
        cover.tick()
        self.assertEqual(self.last("position"), "75")
        self.rec.advance(10.0)
        cover.tick()
        self.assertEqual((self.last("position"), self.last("state")), ("100", "open"))


class TestCommands(CoverTestCase):
    def test_open_close_stop(self):
        cover = self.make()
        for payload in ("OPEN", "close", " STOP "):
            cover.on_command(cover.command_topic, payload)
        self.assertEqual(self.rec.sent, [UP, DOWN, STOP])

    def test_garbage_is_refused_with_a_warning(self):
        cover = self.make(tilt_time=1.0)
        with self.assertLogs(cover_module._LOGGER, "WARNING"):
            cover.on_command(cover.command_topic, "UP")
            for payload in ("abc", "nan", "inf", ""):
                cover.on_command(cover.set_position_topic, payload)
                cover.on_command(cover.set_tilt_topic, payload)
        self.assertEqual(self.rec.sent, [])

    def test_positions_are_clamped(self):
        cover = self.make()
        cover.on_command(cover.set_position_topic, "150")
        cover.on_command(cover.set_position_topic, "-3")
        cover.on_command(cover.set_position_topic, "42.7")
        self.assertEqual(self.rec.sent, ["*#2*21*#11#001#1*100##", "*#2*21*#11#001#1*0##",
                                         "*#2*21*#11#001#1*42##"])


if __name__ == "__main__":
    unittest.main()
