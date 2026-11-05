"""The supervised link: two sessions, reconnect, keepalive and the bus pacing."""

from __future__ import annotations

import asyncio
import logging
import unittest

from myhome_scs_mqtt.app.own import gateway as gateway_module, messages as msg
from myhome_scs_mqtt.app.own.gateway import BusPacing, Gateway, RepeatLimiter

from .fake_gateway import FakeGateway
from .helpers import FakeClock, wait_until


class TestMeasuredConstants(unittest.TestCase):
    def test_keepalive_stays_well_below_the_idle_timeout(self):
        # WISSEN.md: silent command session closed after ~30 s, 20 s is stable,
        # 30 s broke every minute.
        self.assertEqual(gateway_module.KEEPALIVE_INTERVAL, 20.0)
        self.assertEqual(gateway_module.KEEPALIVE_FRAME, "*#13**0##")

    def test_bus_limit(self):
        self.assertEqual(gateway_module.MAX_FRAMES_PER_SECOND, 5.0)
        self.assertEqual(gateway_module.BUCKET_SIZE, 10.0)

    def test_the_command_reserve(self):
        # Decided on 24.09.2026 (berichte/21-strategie-buslast.md): 1.5 frames
        # per second stay free for commands, so the background may draw 3.5.
        self.assertEqual(gateway_module.COMMAND_RESERVE, 1.5)
        pacing = BusPacing()
        self.assertEqual(pacing.rate - pacing.reserve, 3.5)

    def test_brake_starting_values(self):
        # Decided on 17.09.2026: brake only after 3 troubles within 3 seconds -
        # provisional, fixed after the measurement on real hardware.
        self.assertEqual((gateway_module.BRAKE_THRESHOLD, gateway_module.BRAKE_WINDOW),
                         (3, 3.0))
        self.assertEqual((gateway_module.BRAKE_STEP, gateway_module.BRAKE_RELIEF,
                          gateway_module.BRAKE_MAX), (0.5, 0.1, 2.0))

    def test_reconnect_range(self):
        self.assertEqual((gateway_module.RECONNECT_MIN, gateway_module.RECONNECT_MAX),
                         (5.0, 60.0))


class PacingTestCase(unittest.IsolatedAsyncioTestCase):
    """BusPacing on a fake clock: every wait moves the clock, nothing sleeps."""

    def make(self, **kwargs) -> BusPacing:
        self.clock = FakeClock()
        self.waits: list[float] = []

        async def sleep(seconds: float) -> None:
            self.waits.append(seconds)
            self.clock.advance(seconds)

        return BusPacing(clock=self.clock, sleep=sleep, **kwargs)

    async def release_times(self, pacing: BusPacing, count: int,
                            background: bool = False) -> list[float]:
        times = []
        for _ in range(count):
            await pacing.take(background)
            times.append(self.clock())
        return times

    @staticmethod
    def gaps(times: list[float]) -> list[float]:
        return [round(later - earlier, 9) for earlier, later in zip(times, times[1:])]


class TestHardCeiling(PacingTestCase):
    async def test_a_burst_of_ten_passes_at_once(self):
        pacing = self.make()
        for _ in range(10):
            await pacing.take()
        self.assertEqual(self.waits, [])
        await pacing.take()
        self.assertEqual(len(self.waits), 1)
        self.assertAlmostEqual(self.waits[0], 0.2)
        self.assertEqual(pacing.throttled, 1)

    async def test_sustained_rate_is_exactly_five_per_second(self):
        pacing = self.make()
        for _ in range(10):
            await pacing.take()
        started = self.clock()
        for _ in range(50):
            await pacing.take()
        self.assertAlmostEqual(self.clock() - started, 10.0, places=6)

    async def test_an_idle_bucket_holds_no_more_than_its_size(self):
        pacing = self.make()
        self.clock.advance(3600)
        for _ in range(10):
            await pacing.take()
        self.assertEqual(self.waits, [])
        await pacing.take()
        self.assertEqual(len(self.waits), 1)

    async def test_parallel_senders_share_the_limit(self):
        # Two senders must not both wait for the same token and leave together.
        pacing = self.make(size=1.0)
        started = self.clock()

        async def sender():
            for _ in range(10):
                await pacing.take()
                await asyncio.sleep(0)

        await asyncio.gather(sender(), sender())
        self.assertAlmostEqual(self.clock() - started, 19 * 0.2, places=6)

    def test_nonsense_values_are_refused(self):
        for kwargs in ({"rate": 0}, {"size": 0.5}, {"threshold": 0}, {"window": 0},
                       {"step": -1}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    BusPacing(**kwargs)


class TestAdaptiveBrake(PacingTestCase):
    async def test_a_single_refusal_does_not_brake(self):
        # An address that does not exist is refused on every refresh; in the
        # old add-on that alone slowed everything down for minutes.
        pacing = self.make()
        pacing.report(False)
        pacing.report(False)
        self.assertEqual(pacing.gap, 0.0)
        pacing.report(False)
        self.assertEqual(pacing.gap, 0.5)
        self.assertEqual(pacing.refused, 3)

    async def test_troubles_spread_over_time_are_no_pile_up(self):
        pacing = self.make()
        for _ in range(5):
            pacing.report(False)
            self.clock.advance(1.6)            # three of them span 3.2 s > 3 s
        self.assertEqual(pacing.gap, 0.0)
        self.assertEqual(pacing.refused, 5)

    async def test_the_window_edge_still_counts(self):
        pacing = self.make()
        for _ in range(3):
            pacing.report(None)
            self.clock.advance(1.5)            # 0 s, 1.5 s, 3.0 s
        self.assertEqual(pacing.gap, 0.5)

    async def test_silence_counts_even_for_a_probe(self):
        pacing = self.make()
        for _ in range(3):
            pacing.report(None, probe=True)
        self.assertEqual(pacing.gap, 0.5)
        self.assertEqual((pacing.unanswered, pacing.refused), (3, 0))

    async def test_a_refused_probe_never_counts(self):
        # CHANGELOG 1.12.1: "this zone has no fan" is an answer, not overload.
        pacing = self.make()
        for _ in range(20):
            pacing.report(False, probe=True)
        self.assertEqual((pacing.gap, pacing.refused, pacing.unanswered), (0.0, 0, 0))

    async def test_every_further_trouble_widens_the_gap_up_to_the_limit(self):
        pacing = self.make()
        gaps = []
        for _ in range(9):
            pacing.report(False)
            gaps.append(pacing.gap)
        self.assertEqual(gaps, [0.0, 0.0, 0.5, 1.0, 1.5, 2.0, 2.0, 2.0, 2.0])

    async def test_success_narrows_the_gap_again(self):
        pacing = self.make()
        for _ in range(4):
            pacing.report(None)
        self.assertEqual(pacing.gap, 1.0)
        for _ in range(4):
            pacing.report(True)
        self.assertAlmostEqual(pacing.gap, 0.6)
        for _ in range(20):
            pacing.report(True)
        self.assertEqual(pacing.gap, 0.0)       # exactly, no float residue

    async def test_a_refused_probe_neither_brakes_nor_relieves(self):
        pacing = self.make()
        for _ in range(3):
            pacing.report(False)
        pacing.report(False, probe=True)
        self.assertEqual(pacing.gap, 0.5)

    async def test_reset_forgets_the_brake_but_keeps_the_counters(self):
        pacing = self.make()
        for _ in range(4):
            pacing.report(False)
        pacing.reset()
        self.assertEqual(pacing.gap, 0.0)
        # The troubles before the reset are forgotten as well.
        pacing.report(False)
        pacing.report(False)
        self.assertEqual(pacing.gap, 0.0)
        self.assertEqual(pacing.refused, 6)

    async def test_engaging_is_logged_once_escalating_on_debug_only(self):
        pacing = self.make()
        with self.assertLogs(gateway_module._LOGGER, "DEBUG") as logs:
            for _ in range(8):
                pacing.report(False)
        loud = [r.getMessage() for r in logs.records if r.levelno >= logging.INFO]
        self.assertEqual(len(loud), 1, loud)
        self.assertIn("slowing down", loud[0])

    async def test_a_threshold_from_the_options_is_used(self):
        pacing = self.make(threshold=5, window=10.0)
        for _ in range(4):
            pacing.report(False)
            self.clock.advance(2.0)
        self.assertEqual(pacing.gap, 0.0)
        pacing.report(False)                    # five within 8 s
        self.assertEqual(pacing.gap, 0.5)


class TestBrakeMeasuresOverload(PacingTestCase):
    """Refusals of one address are that address's problem, not the bus's.

    Measured on 24.09.2026 (berichte/22-messung-buslast.md): a single entry
    with an address that does not exist put the brake at its maximum, and
    switching a real lamp then took seconds.
    """

    DEAD = "1:12342"

    async def test_one_dead_address_never_brakes(self):
        pacing = self.make()
        for _ in range(20):
            pacing.report(False, address=self.DEAD)
        self.assertEqual(pacing.gap, 0.0)
        self.assertEqual(pacing.refused, 20)        # counted, just not braked

    async def test_two_addresses_in_the_window_are_overload(self):
        pacing = self.make()
        pacing.report(False, address=self.DEAD)
        pacing.report(False, address=self.DEAD)
        self.assertEqual(pacing.gap, 0.0)
        pacing.report(False, address="1:13")
        self.assertEqual(pacing.gap, 0.5)

    async def test_silence_of_a_single_address_still_brakes(self):
        # WISSEN.md "Buslast": silence is the honest overload signal.
        pacing = self.make()
        for _ in range(3):
            pacing.report(None, address=self.DEAD)
        self.assertEqual(pacing.gap, 0.5)

    async def test_an_unknown_address_counts_on_its_own(self):
        pacing = self.make()
        for _ in range(3):
            pacing.report(False)
        self.assertEqual(pacing.gap, 0.5)

    async def test_the_dead_address_does_not_hide_a_real_overload(self):
        pacing = self.make()
        pacing.report(False, address=self.DEAD)
        pacing.report(None, address=self.DEAD)      # silence counts
        pacing.report(False, address=self.DEAD)
        self.assertEqual(pacing.gap, 0.5)

    async def test_the_gap_halves_after_every_quiet_window(self):
        pacing = self.make()
        for _ in range(8):
            pacing.report(False, address="1:%d" % _)
        self.assertEqual(pacing.gap, 2.0)
        self.clock.advance(3.0)
        self.assertEqual(pacing.gap, 1.0)
        self.clock.advance(3.0)
        self.assertEqual(pacing.gap, 0.5)
        self.clock.advance(12.0)                    # four more windows at once
        self.assertEqual(pacing.gap, 0.0)           # 0.03125 is below the floor

    async def test_a_released_brake_lets_the_background_through_again(self):
        pacing = self.make()
        for number in range(8):
            pacing.report(False, address="1:%d" % number)
        self.clock.advance(60.0)                    # a minute without trouble
        self.waits.clear()
        for _ in range(3):
            await pacing.take(True)
        self.assertEqual(self.waits, [])

    async def test_how_long_the_brake_needs_to_let_go_completely(self):
        """From the maximum back to zero - this was the five-minute aftermath.

        Both mechanisms work together on a bus that carries on: every
        acknowledged frame takes 0.1 s off, and every trouble-free window
        halves what is left.  Measured on the fake clock: under ten seconds,
        where the relief alone needed twenty acknowledged frames at one frame
        every two seconds - the five minutes after the scan of 24.09.2026.
        """
        pacing = self.make()
        for number in range(8):
            pacing.report(False, address="1:%d" % number)
        self.assertEqual(pacing.gap, 2.0)
        started = self.clock()
        while pacing.gap > 0:
            await pacing.take(True)
            pacing.report(True, address="1:12")
            self.assertLess(self.clock() - started, 60.0, "the brake never let go")
        # Measured 07.10.2026: 6.5 seconds (criterion from the strategy: 10).
        self.assertLess(self.clock() - started, 10.0, "released after %.2fs"
                        % (self.clock() - started))

    async def test_further_trouble_restarts_the_fading(self):
        pacing = self.make()
        for number in range(8):
            pacing.report(False, address="1:%d" % number)
        self.clock.advance(2.9)                     # not a full window yet
        pacing.report(False, address="1:99")
        self.clock.advance(2.9)
        self.assertEqual(pacing.gap, 2.0)


class TestCeilingAndBrakeTogether(PacingTestCase):
    """What really leaves when both rules act at the same time."""

    async def test_a_brake_above_the_ceiling_rules_and_the_waits_do_not_add_up(self):
        pacing = self.make()
        burst = await self.release_times(pacing, 7, background=True)
        self.assertEqual(self.gaps(burst), [0.0] * 6)   # background bucket empty now
        for _ in range(3):
            pacing.report(False)
        # Within the first window: 0.5 s of brake - not 0.5 s plus 0.2 s of
        # ceiling, two frames a second.  (After a trouble-free window the brake
        # starts to fade, see TestBrakeMeasuresOverload.)
        times = await self.release_times(pacing, 6, background=True)
        self.assertEqual(self.gaps(times), [0.5] * 5)
        self.assertAlmostEqual(5 / (times[-1] - times[0]), 2.0)
        self.assertEqual(pacing.throttled, 0)

    async def test_a_brake_below_the_ceiling_leaves_five_frames_a_second(self):
        # Without the command reserve (it has its own tests below), so that
        # ceiling and brake are the only two rules left.
        pacing = self.make(step=0.1, reserve=0.0)
        for _ in range(3):
            pacing.report(None)
        self.assertEqual(pacing.gap, 0.1)
        times = await self.release_times(pacing, 60, background=True)
        # The full bucket passes at the brake's 0.1 s, then the ceiling holds 0.2 s.
        self.assertEqual(self.gaps(times)[:5], [0.1] * 5)
        self.assertEqual(self.gaps(times)[-20:], [0.2] * 20)
        self.assertAlmostEqual(20 / (times[-1] - times[-21]), 5.0)
        self.assertGreater(pacing.throttled, 0)

    async def test_an_idle_bus_does_not_wait_for_the_brake(self):
        pacing = self.make()
        for _ in range(8):
            pacing.report(False)
        self.assertEqual(pacing.gap, 2.0)
        await pacing.take(True)
        self.clock.advance(5.0)                 # nothing was sent for a while
        self.waits.clear()
        await pacing.take(True)
        self.assertEqual(self.waits, [])


class TestCommandsHaveRightOfWay(PacingTestCase):
    """The brake and the reserve hold back our own housekeeping, never a command.

    Measured on 24.09.2026: one address that does not exist put the brake at
    its maximum, and switching a real lamp then took seconds.
    """

    async def test_the_brake_never_delays_a_command(self):
        pacing = self.make()
        for _ in range(8):
            pacing.report(False)
        self.assertEqual(pacing.gap, 2.0)
        for _ in range(5):
            await pacing.take()
        self.assertEqual(self.waits, [], "a command waited for the brake")

    async def test_the_brake_holds_the_background_back(self):
        pacing = self.make()
        for _ in range(3):
            pacing.report(False)
        times = await self.release_times(pacing, 4, background=True)
        self.assertEqual(self.gaps(times), [0.5] * 3)

    async def test_the_background_leaves_the_reserve_free(self):
        # Sustained background traffic must not use the whole bus: with a
        # reserve of 1.5 it may draw 3.5 frames a second.
        pacing = self.make()
        await self.release_times(pacing, 7, background=True)     # empties its bucket
        times = await self.release_times(pacing, 36, background=True)
        self.assertAlmostEqual(35 / (times[-1] - times[0]), 3.5, places=6)

    async def test_a_command_passes_while_the_background_runs_at_full_tilt(self):
        pacing = self.make()
        for _ in range(7):
            await pacing.take(True)
        # The background bucket is empty, the brake is at its maximum - and
        # still every command goes out without a wait.
        for _ in range(8):
            pacing.report(None)
        self.waits.clear()
        for _ in range(3):
            await pacing.take()
        self.assertEqual(self.waits, [])

    async def test_only_the_hard_ceiling_can_hold_a_command_back(self):
        pacing = self.make()
        for _ in range(10):
            await pacing.take()
        await pacing.take()
        self.assertEqual(len(self.waits), 1)
        self.assertAlmostEqual(self.waits[0], 0.2)

    def test_a_reserve_that_leaves_nothing_is_refused(self):
        for reserve in (5.0, 6.0, -0.5):
            with self.subTest(reserve=reserve):
                with self.assertRaises(ValueError):
                    BusPacing(reserve=reserve)


class TestRepeatLimiter(unittest.TestCase):
    def test_a_recurring_problem_is_logged_once_per_interval(self):
        clock = FakeClock()
        logger = logging.getLogger("tests.repeat")
        limiter = RepeatLimiter(logger, interval=600, clock=clock)
        with self.assertLogs(logger, "DEBUG") as logs:
            for _ in range(5):
                limiter.log("down", logging.WARNING, "gateway unreachable")
                clock.advance(60)
            clock.advance(600)
            limiter.log("down", logging.WARNING, "gateway unreachable")
        loud = [r.getMessage() for r in logs.records if r.levelno == logging.WARNING]
        self.assertEqual(loud[0], "gateway unreachable")
        self.assertEqual(len(loud), 2)
        self.assertIn("4 more times", loud[1])

    def test_the_traceback_goes_with_the_loud_message_only(self):
        clock = FakeClock()
        logger = logging.getLogger("tests.repeat")
        limiter = RepeatLimiter(logger, interval=600, clock=clock)
        with self.assertLogs(logger, "DEBUG") as logs:
            for _ in range(2):
                try:
                    raise RuntimeError("bug")
                except RuntimeError:
                    limiter.log("tick", logging.ERROR, "tick failed", exc_info=True)
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertIsNone(logs.records[1].exc_info)

    def test_reset_makes_the_next_message_loud(self):
        clock = FakeClock()
        logger = logging.getLogger("tests.repeat")
        limiter = RepeatLimiter(logger, interval=600, clock=clock)
        with self.assertLogs(logger, "DEBUG") as logs:
            limiter.log("x", logging.WARNING, "one")
            limiter.reset("x")
            limiter.log("x", logging.WARNING, "two")
        self.assertEqual([r.levelno for r in logs.records], [logging.WARNING] * 2)


class LinkTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fake = FakeGateway(password="12345", mode="none")
        self.port = await self.fake.start()
        self.messages: list[msg.OwnMessage] = []
        self.states: list[bool] = []
        self.links: list[Gateway] = []

    async def asyncTearDown(self) -> None:
        for link in self.links:
            await link.stop()
        await self.fake.stop()

    def make(self, password: str = "12345", **kwargs) -> Gateway:
        options = {"reconnect_min": 0.05, "reconnect_max": 0.4}
        options.update(kwargs)
        link = Gateway("127.0.0.1", self.port, password,
                       on_message=self.messages.append, on_connected=self.states.append,
                       **options)
        self.links.append(link)
        return link


class TestLink(LinkTestCase):
    async def test_two_sessions_make_one_link(self):
        link = self.make()
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        self.assertEqual(sorted(self.fake.sessions),
                         sorted([msg.SESSION_EVENT, msg.SESSION_COMMAND]))
        self.assertEqual(self.states, [True])

    async def test_events_and_answers_reach_the_handler(self):
        link = self.make()
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        await self.fake.emit("*1*1*12##")
        self.assertTrue(await wait_until(lambda: any(m.raw == "*1*1*12##" for m in self.messages)))
        self.assertGreaterEqual(link.received, 1)

    async def test_mh201_answers_on_the_event_session(self):
        # WISSEN.md: *#4*8*0## -> ACK on the command session, the value on the
        # event session.  Only a client with both sessions ever sees it.
        self.fake.answers_on_event_session = True
        self.fake.responses["*#4*8*0##"] = ["*#4*8*0*0269##"]
        link = self.make()
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        self.assertTrue(await link.send("*#4*8*0##"))
        self.assertTrue(await wait_until(
            lambda: any(m.raw == "*#4*8*0*0269##" for m in self.messages)))

    async def test_send_reports_ack_nack_and_silence(self):
        link = self.make()
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        self.fake.nack_frames.add("*1*1*99##")
        self.fake.silent_frames.add("*1*1*98##")
        self.assertIs(await link.send("*1*1*12##"), True)
        self.assertIs(await link.send("*1*1*99##"), False)
        self.assertIsNone(await link.send("*1*1*98##", timeout=0.2))
        self.assertEqual(link.sent, 3)

    async def test_without_link_frames_are_dropped_after_waiting(self):
        await self.fake.stop()
        link = self.make()
        await link.start()
        self.assertIsNone(await link.send("*1*1*12##", wait_ready=0.1))
        self.assertIs(await link.post("*#1*12##", wait_ready=0.1), False)
        self.assertEqual(link.sent, 0)

    async def test_stop_closes_everything(self):
        link = self.make()
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        await link.stop()
        self.assertFalse(link.connected)
        self.assertEqual(self.states, [True, False])
        self.assertTrue(await wait_until(lambda: self.fake.open_connections == 0))
        await link.stop()                         # twice is harmless

    async def test_a_gateway_reboot_is_survived(self):
        link = self.make()
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        self.fake.drop_all()
        self.assertTrue(await wait_until(lambda: self.states == [True, False, True]),
                        self.states)
        self.assertTrue(link.connected)
        self.assertEqual(self.fake.logins, 4)

    async def test_a_broken_callback_does_not_stop_the_link(self):
        def explode(_state):
            raise RuntimeError("bug in the bridge")

        link = Gateway("127.0.0.1", self.port, "12345", on_connected=explode,
                       reconnect_min=0.05, reconnect_max=0.4)
        self.links.append(link)
        with self.assertLogs("myhome_scs_mqtt.app.own.gateway", "ERROR"):
            await link.start()
            self.assertTrue(await wait_until(lambda: link.connected))
        self.assertTrue(await link.send("*1*1*12##"))

    async def test_stop_does_not_warn_about_a_send_that_was_in_flight(self):
        link = self.make()
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        self.fake.silent_frames.add("*1*1*77##")
        sending = asyncio.create_task(link.send("*1*1*77##", timeout=5.0))
        self.assertTrue(await wait_until(lambda: "*1*1*77##" in self.fake.received))
        with self.assertNoLogs("myhome_scs_mqtt.app.own.gateway", "WARNING"):
            await link.stop()
            self.assertIsNone(await sending)


class TestSessionLimit(LinkTestCase):
    async def test_fifth_session_quirk_leads_to_a_clean_reconnect(self):
        # Other clients hold three of the four places of an MH201: the second
        # session of the bridge is accepted and closed at once.
        self.fake.max_sessions = 1
        link = self.make()
        with self.assertLogs("myhome_scs_mqtt.app.own.gateway", "WARNING") as logs:
            await link.start()
            self.assertTrue(await wait_until(lambda: len(self.fake.sessions) >= 1))
            await asyncio.sleep(0.5)
        self.assertFalse(link.connected)
        self.assertTrue(any("four sessions" in r.getMessage() for r in logs.records))

        # A place becomes free: the link comes up by itself.
        self.fake.max_sessions = None
        self.assertTrue(await wait_until(lambda: link.connected, timeout=3.0))
        self.assertTrue(await link.send("*1*1*12##"))


class TestReconnectBackoff(LinkTestCase):
    async def test_delay_doubles_up_to_the_maximum(self):
        await self.fake.stop()
        delays: list[float] = []

        async def sleep(seconds: float) -> None:
            delays.append(seconds)
            await asyncio.sleep(0)

        link = Gateway("127.0.0.1", self.port, "12345", reconnect_min=5.0,
                       reconnect_max=60.0, sleep=sleep)
        self.links.append(link)
        with self.assertLogs("myhome_scs_mqtt.app.own.gateway", "WARNING"):
            await link.start()
            self.assertTrue(await wait_until(lambda: len(delays) >= 14))
        await link.stop()
        # Two sessions retry independently, each 5, 10, 20, 40, 60, 60, ...
        for step in (5.0, 10.0, 20.0, 40.0):
            self.assertEqual(delays.count(step), 2, delays)
        self.assertEqual(set(delays) - {5.0, 10.0, 20.0, 40.0}, {60.0})

    async def test_authentication_errors_retry_slowly_at_once(self):
        self.fake.mode = "hmac"
        delays: list[float] = []

        async def sleep(seconds: float) -> None:
            delays.append(seconds)
            await asyncio.sleep(0.01)

        link = Gateway("127.0.0.1", self.port, "99999", reconnect_min=5.0,
                       reconnect_max=60.0, sleep=sleep)
        self.links.append(link)
        with self.assertLogs("myhome_scs_mqtt.app.own.gateway", "ERROR") as logs:
            await link.start()
            self.assertTrue(await wait_until(lambda: len(delays) >= 2))
        self.assertEqual(delays[:2], [60.0, 60.0])
        self.assertIn("authentication failed", logs.records[0].getMessage())

    async def test_a_lasting_outage_does_not_flood_the_log(self):
        await self.fake.stop()
        attempts: list[float] = []

        async def sleep(seconds: float) -> None:
            attempts.append(seconds)
            await asyncio.sleep(0)

        link = Gateway("127.0.0.1", self.port, "12345", reconnect_min=0.01,
                       reconnect_max=0.01, sleep=sleep)
        self.links.append(link)
        with self.assertLogs("myhome_scs_mqtt.app.own.gateway", "DEBUG") as logs:
            await link.start()
            self.assertTrue(await wait_until(lambda: len(attempts) >= 40))
        warnings = [r for r in logs.records if r.levelno >= logging.WARNING]
        # One line per session, not one per attempt.
        self.assertLessEqual(len(warnings), 2, [r.getMessage() for r in warnings])


class TestKeepalive(LinkTestCase):
    async def test_keepalive_is_sent_periodically(self):
        link = self.make(keepalive_interval=0.1)
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        self.assertTrue(await wait_until(
            lambda: self.fake.received.count(gateway_module.KEEPALIVE_FRAME) >= 3))

    async def test_keepalive_holds_an_idle_session_open(self):
        # The fake closes a command session after 0.4 s without traffic.
        self.fake.idle_timeout = 0.4
        link = self.make(keepalive_interval=0.15)
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        await asyncio.sleep(1.5)
        self.assertEqual(self.fake.logins, 2, "the command session was dropped")
        self.assertTrue(link.connected)

    async def test_without_keepalive_the_idle_timeout_strikes(self):
        # Counter-check of the test above: the fake's timeout is real.
        self.fake.idle_timeout = 0.3
        link = self.make(keepalive_interval=30.0)
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        with self.assertLogs("myhome_scs_mqtt.app.own.gateway", "WARNING"):
            self.assertTrue(await wait_until(lambda: self.fake.logins >= 3, timeout=3.0))

    async def test_keepalive_survives_an_unexpected_bug(self):
        link = self.make(keepalive_interval=0.1)
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        real_request = link._command.request
        calls = []

        async def flaky_request(frame, timeout):
            calls.append(frame)
            if len(calls) == 1:
                raise RuntimeError("simulated bug")
            return await real_request(frame, timeout)

        link._command.request = flaky_request
        with self.assertLogs("myhome_scs_mqtt.app.own.gateway", "ERROR"):
            self.assertTrue(await wait_until(lambda: len(calls) >= 1))
        self.assertTrue(await wait_until(lambda: len(calls) >= 3), "the keepalive task died")

    async def test_a_silent_gateway_gets_a_new_command_session(self):
        self.fake.silent_frames.add(gateway_module.KEEPALIVE_FRAME)
        link = self.make(keepalive_interval=0.1, keepalive_timeout=0.2)
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        with self.assertLogs("myhome_scs_mqtt.app.own.gateway", "WARNING") as logs:
            self.assertTrue(await wait_until(lambda: self.fake.logins >= 3, timeout=3.0))
        self.assertTrue(any("keepalive timed out" in r.getMessage() for r in logs.records))
        self.assertTrue(await wait_until(lambda: link.connected))
        self.assertGreaterEqual(link.unanswered, 1)

    async def test_a_refused_keepalive_is_an_answer(self):
        # A gateway without a clock refuses the keepalive - that proves it lives.
        self.fake.nack_frames.add(gateway_module.KEEPALIVE_FRAME)
        link = self.make(keepalive_interval=0.05, pacing=BusPacing(step=0.3, max_gap=0.3))
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        self.assertTrue(await wait_until(
            lambda: self.fake.received.count(gateway_module.KEEPALIVE_FRAME) >= 5))
        self.assertEqual((link.refused, link.unanswered, link.brake_gap), (0, 0, 0.0))


class TestBusLimitOnTheWire(LinkTestCase):
    async def test_a_burst_is_slowed_down_to_the_limit(self):
        link = self.make()
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        loop = asyncio.get_running_loop()
        started = loop.time()
        for _ in range(20):
            # background=False: the hard ceiling alone, without the slower
            # background bucket (that one is measured in TestCommandsHaveRightOfWay).
            self.assertTrue(await link.post("*#1*12*1##", background=False))
        elapsed = loop.time() - started
        # Ten frames fit in the bucket, the other ten wait 0.2 s each.
        expected = (20 - gateway_module.BUCKET_SIZE) / gateway_module.MAX_FRAMES_PER_SECOND
        self.assertGreater(elapsed, expected * 0.9,
                           "twenty frames left in %.2fs - the limit did nothing" % elapsed)
        self.assertGreater(link.throttled, 0)
        self.assertEqual(link.sent, 20)

    async def test_normal_traffic_is_not_delayed(self):
        link = self.make()
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        loop = asyncio.get_running_loop()
        started = loop.time()
        for _ in range(3):
            await link.post("*#1*12*1##")
        self.assertLess(loop.time() - started, 0.2)


class TestPacingOnTheWire(LinkTestCase):
    """The brake measured where it matters: at the gateway."""

    def arrivals(self, prefix: str) -> list[float]:
        return [at for at, frame in self.fake.timeline if frame.startswith(prefix)]

    async def connected_link(self) -> Gateway:
        link = self.make(pacing=BusPacing(step=0.3, max_gap=0.3))
        await link.start()
        self.assertTrue(await wait_until(lambda: link.connected))
        return link

    async def test_piled_up_refusals_slow_down_the_scan_but_not_commands(self):
        link = await self.connected_link()
        frames = [f"*1*1*6{number}##" for number in range(6)]
        self.fake.nack_frames.update(frames)
        with self.assertLogs("myhome_scs_mqtt.app.own.session", "WARNING"):
            for frame in frames:
                self.assertIs(await link.send(frame, background=True), False)
        self.assertEqual((link.refused, link.unanswered, link.brake_gap), (6, 0, 0.3))
        gaps = PacingTestCase.gaps(self.arrivals("*1*1*6"))
        self.assertTrue(all(gap < 0.2 for gap in gaps[:2]), gaps)       # no brake yet
        self.assertTrue(all(gap > 0.25 for gap in gaps[2:]), gaps)      # 0.3 s from the 3rd on
        # The bus scan posts through the very same brake.
        for _ in range(4):
            self.assertTrue(await link.post("*#1*70##"))
        posted = PacingTestCase.gaps(self.arrivals("*#1*70##"))
        self.assertTrue(all(gap > 0.25 for gap in posted), posted)
        # A command, however, goes out at once - the brake is a protection
        # against our own load, not a reason to delay a key press.
        loop = asyncio.get_running_loop()
        started = loop.time()
        for number in range(3):
            self.assertIs(await link.send(f"*1*1*8{number}##"), True)
        self.assertLess(loop.time() - started, 0.3,
                        "the brake delayed commands: %.2fs" % (loop.time() - started))

    async def test_one_dead_address_on_the_wire_does_not_brake(self):
        # The measured case: one entry in devices.yaml whose address does not
        # exist.  Every round it is refused - and nothing else may suffer.
        link = await self.connected_link()
        dead = ["*1*0*12342##", "*1*1*12342##", "*#1*12342##", "*#1*12342*1##"]
        self.fake.nack_frames.update(dead)
        with self.assertLogs("myhome_scs_mqtt.app.own.session", "WARNING"):
            for _round in range(3):
                for frame in dead:
                    self.assertIs(await link.send(frame, background=True), False)
        self.assertEqual((link.refused, link.brake_gap), (12, 0.0))

    async def test_silence_piling_up_brakes_as_well(self):
        link = await self.connected_link()
        frames = [f"*#1*8{number}##" for number in range(3)]
        self.fake.silent_frames.update(frames)
        with self.assertLogs("myhome_scs_mqtt.app.own.session", "WARNING"):
            for frame in frames:
                self.assertIsNone(await link.send(frame, timeout=0.1))
        self.assertEqual((link.unanswered, link.brake_gap), (3, 0.3))

    async def test_refused_probes_on_the_wire_do_not_brake(self):
        link = await self.connected_link()
        probes = [f"*#4*{zone}*60##" for zone in range(1, 7)]
        self.fake.nack_frames.update(probes)
        for frame in probes:
            self.assertIs(await link.send(frame, quiet=True), False)
        self.assertEqual((link.refused, link.brake_gap), (0, 0.0))

    async def test_a_new_link_starts_without_brake_the_counters_stay(self):
        link = await self.connected_link()
        frames = [f"*1*1*6{number}##" for number in range(4)]
        self.fake.nack_frames.update(frames)
        with self.assertLogs("myhome_scs_mqtt.app.own.session", "WARNING"):
            for frame in frames:
                await link.send(frame)
        self.assertEqual(link.brake_gap, 0.3)
        with self.assertLogs("myhome_scs_mqtt.app.own.gateway", "WARNING"):
            self.fake.drop_all()
            self.assertTrue(await wait_until(lambda: self.states == [True, False, True]),
                            self.states)
        self.assertEqual((link.brake_gap, link.refused), (0.0, 4))


if __name__ == "__main__":
    unittest.main()
