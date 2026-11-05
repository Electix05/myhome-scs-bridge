"""The yardstick: 200 simulated devices on a fake gateway.

Everything the measurement of 24.09.2026 found on the real installation
(``berichte/22-messung-buslast.md``) is reproduced here without any hardware -
a fake gateway on 127.0.0.1, 200 generated devices, real asyncio timing.

The scenarios and their criteria come from ``berichte/21-strategie-buslast.md``:

===========================  ==================================================
Idle                         own bus load stays modest, no burst
Command during the refresh   the frame leaves the queue in under 0.5 s
Command during a scan        under 3 s - sluggish, but it works
Ten dead addresses           silenced after three rounds, commands unaffected
Twenty shutters at once      every one reaches its target, the queue empties
After the scan               the brake is released, the queue is empty
===========================  ==================================================

The test is written to be robust in time: it measures rates and latencies over
short stretches with generous tolerances instead of waiting out a full refresh
round (which at 200 devices takes minutes on purpose).
"""

from __future__ import annotations

import asyncio
import time
import unittest

from myhome_scs_mqtt.app.bridge.background import BackgroundTasks
from myhome_scs_mqtt.app.bridge.registry import Registry
from myhome_scs_mqtt.app.devices.base import Context
from myhome_scs_mqtt.app.own.gateway import BusPacing, Gateway

from .fake_gateway import FakeGateway
from .helpers import wait_until

# 200 devices in the mix of a large installation: mostly lights and shutters,
# a heating zone per room, a few contacts, meters and presence sensors.
LIGHTS = 100
COVERS = 60
ZONES = 20
CONTACTS = 10
METERS = 5
MOTION = 5


def build_devices() -> dict[str, list[dict[str, object]]]:
    """A devices.yaml as a dict - 200 entries with plausible addresses."""
    lights = [{"where": f"{area}{point}", "name": f"Licht {area}-{point}"}
              for area in range(1, 11) for point in range(1, 11)][:LIGHTS]
    covers = [{"where": f"{area}{point}", "name": f"Rollladen {area}-{point}"}
              for area in range(1, 7) for point in range(1, 11)][:COVERS]
    return {
        "lights": lights,
        "covers": covers,
        "climate": [{"where": str(zone), "name": f"Zone {zone}"}
                    for zone in range(1, ZONES + 1)],
        "binary_sensors": [{"where": str(number), "name": f"Kontakt {number}"}
                           for number in range(1, CONTACTS + 1)],
        "energy": [{"where": f"5{number}", "name": f"Zähler {number}"}
                   for number in range(1, METERS + 1)],
        "motion_sensors": [{"where": f"20{number}", "name": f"Melder {number}"}
                           for number in range(1, MOTION + 1)],
    }


class LoadTestCase(unittest.IsolatedAsyncioTestCase):
    """Registry, background tasks and a real gateway against the fake one.

    No MQTT and no web interface: what is measured here is the bus.
    """

    poll_interval = 900
    refresh_pace = 0.5

    def pacing(self) -> BusPacing:
        """The real bus limit - every latency below is measured against it."""
        return BusPacing()

    async def asyncSetUp(self) -> None:
        self.fake = FakeGateway(mode="none")
        port = await self.fake.start()
        self.published: list[tuple[str, str]] = []
        self.link = Gateway("127.0.0.1", port, "", reconnect_min=0.05, reconnect_max=0.2,
                            pacing=self.pacing())
        await self.link.start()
        self.assertTrue(await wait_until(lambda: self.link.connected, 5))

        loop = asyncio.get_running_loop()
        self.context = Context(
            base_topic="myhome",
            discovery_prefix="homeassistant",
            availability_topic="myhome/bridge/availability",
            publish=lambda topic, payload, retain=True, qos=0: self.published.append(
                (topic, str(payload))),
            send=self.queue_command,
            queue_depth=lambda: self.tasks.queue_depth(),
            call_later=loop.call_later,
            bridge_availability="myhome/bridge/lwt",
            dead_after=3,
        )
        self.registry = Registry(self.context)
        self.registry.build(build_devices(), [])
        self.tasks = BackgroundTasks(self.link, self.registry,
                                     poll_interval=self.poll_interval,
                                     refresh_pace=self.refresh_pace,
                                     auto_discover=lambda: False, tick_interval=1.0)
        self.runner = asyncio.create_task(self.tasks.run())

    async def asyncTearDown(self) -> None:
        self.runner.cancel()
        await asyncio.gather(self.runner, return_exceptions=True)
        await self.link.stop()
        await self.fake.stop()

    def queue_command(self, frame: str) -> None:
        """What entities send goes in as foreground, like in the bridge."""
        self.tasks.queue_frame(frame)

    # ------------------------------------------------------------- helpers

    def entity(self, section: str, where: str):
        for entity in self.registry.entities:
            if entity.section == section and entity.where == where:
                return entity
        raise AssertionError(f"no {section} at {where}")

    def arrival(self, frame: str) -> float | None:
        for at, sent in self.fake.timeline:
            if sent == frame:
                return at
        return None

    async def latency_of(self, frame: str, timeout: float = 10.0) -> float:
        """Queue one command and return how long it took to reach the gateway."""
        queued = time.monotonic()
        self.tasks.queue_frame(frame)
        self.assertTrue(await wait_until(lambda: frame in self.fake.received, timeout),
                        f"{frame} never reached the gateway")
        arrived = self.arrival(frame)
        self.assertIsNotNone(arrived)
        # The fake gateway stamps with the loop clock, which is the same
        # monotonic clock asyncio runs on.
        return arrived - queued


class TestTheInstallationIsBuilt(LoadTestCase):
    async def test_two_hundred_devices(self):
        self.assertEqual(len(self.registry.device_entities()), 200)
        self.assertEqual(len(self.registry.topics), len(set(self.registry.topics)))

    def test_what_one_refresh_round_costs(self):
        """How many frames one round asks - the basis for the pace.

        Counted from the code, not guessed: every entity is asked for its poll
        frames twice, because a relay light asks for its level only in the
        first round.
        """
        first = [frame for entity in self.registry.entities for frame in entity.poll_frames()]
        steady = [frame for entity in self.registry.entities for frame in entity.poll_frames()]
        self.assertEqual((len(first), len(steady)), (465, 365))

    def test_what_one_round_costs_on_the_measured_installation(self):
        """The 40 devices of 24.09.2026: where the burst of 60 a minute came from.

        18 lights, 9 shutters, 5 heating zones, one presence sensor and 7
        contacts.  Counted from the code:

        * light: 2 frames in the first round (status and level), 1 afterwards
        * shutter without position feedback: 2 (status and dimension 10)
        * heating zone: 6 (status, temperature, setpoint, valves, two probes)
        * presence sensor: 2 (lux and presence)
        * contact: 0 - it costs nothing until it is assigned
          (``DryContact.configured``)

        That is 86 frames in the first round and 68 afterwards.  At the fixed
        pace of 0.5 s one round therefore ran for 34 seconds at two frames a
        second, no matter how long ``poll_interval`` was - exactly the burst in
        berichte/22-messung-buslast.md, point 1.
        """
        registry = Registry(self.context)
        registry.build({
            "lights": [{"where": f"1{point}"} for point in range(1, 10)]
            + [{"where": f"2{point}"} for point in range(1, 10)],
            "covers": [{"where": f"3{point}"} for point in range(1, 10)],
            "climate": [{"where": str(zone)} for zone in range(1, 6)],
            "motion_sensors": [{"where": "201"}],
            "binary_sensors": [{"where": str(number)} for number in range(1, 8)],
        }, [])
        self.assertEqual(len(registry.device_entities()), 40)
        first = [frame for entity in registry.entities for frame in entity.poll_frames()]
        steady = [frame for entity in registry.entities for frame in entity.poll_frames()]
        self.assertEqual((len(first), len(steady)), (86, 68))
        self.assertEqual(18 * 2 + 9 * 2 + 5 * 6 + 2 + 7 * 0, len(first))
        self.assertEqual(len(steady) * 0.5, 34.0)           # seconds at the old pace


class TestTheCeilingOfThisTestFile(LoadTestCase):
    """How fast the fake gateway can answer at all.

    Everything in this file is measured against real asyncio timing, so it is
    worth knowing where the limit of the test setup itself is: with the bus
    pacing lifted, this is how many request/acknowledge exchanges per second
    two sockets on 127.0.0.1 manage.
    """

    def pacing(self) -> BusPacing:
        return BusPacing(rate=1000.0, size=1000.0, reserve=100.0)

    async def test_how_many_exchanges_a_second_the_fake_manages(self):
        loop = asyncio.get_running_loop()
        started = loop.time()
        for number in range(100):
            self.assertIs(await self.link.send(f"*#1*{number}##", background=True), True)
        rate = 100 / (loop.time() - started)
        # Measured 07.10.2026: about 3100 a second.  Anything above a few
        # hundred means the numbers below are the add-on's own pacing, not the
        # test setup struggling.
        self.assertGreater(rate, 500.0, "%.1f exchanges a second" % rate)


class TestIdleLoad(LoadTestCase):
    """What 200 devices cost when nobody touches anything."""

    def round_sizes(self) -> tuple[int, int]:
        """Frames of the first and of every later round."""
        first = [frame for entity in self.registry.entities for frame in entity.poll_frames()]
        steady = [frame for entity in self.registry.entities for frame in entity.poll_frames()]
        return len(first), len(steady)

    def test_the_round_fits_the_interval(self):
        first, steady = self.round_sizes()
        pace = self.tasks.refresh_pace(steady)
        per_minute = 60.0 / pace
        # The criteria from the strategy: no peak above 60 frames a minute while
        # the round runs, and under 30 on average over the interval.
        self.assertLessEqual(per_minute, 60.0,
                             "%.1f frames a minute while the round runs" % per_minute)
        average = steady * 60.0 / self.poll_interval
        self.assertLess(average, 30.0, "%.1f frames a minute on average" % average)
        # And the round really spreads over about half the interval.
        self.assertAlmostEqual(steady * pace, 0.5 * self.poll_interval, delta=1.0)
        # Only the very first round after a start is a little denser: it asks
        # the 100 relay lights for their level once (62 a minute, once).
        self.assertEqual(round(60.0 / self.tasks.refresh_pace(first)), 62)

    def test_the_keepalive_is_all_that_happens_without_a_refresh(self):
        # Nothing is polled unless a refresh is started: no hidden traffic.
        self.assertEqual(self.link.sent, 0)
        self.assertEqual(self.tasks.queue_depth(), 0)


class TestCommandLatency(LoadTestCase):
    """How long a key press waits - the number that was unusable before."""

    LAMP = "*1*1*11##"

    async def test_a_command_during_the_refresh_is_under_half_a_second(self):
        self.tasks.start_refresh()
        self.assertTrue(await wait_until(lambda: self.link.sent >= 3, 10))
        latency = await self.latency_of(self.LAMP)
        self.assertLess(latency, 0.5, "the command needed %.2fs" % latency)

    async def test_a_command_while_the_whole_round_waits_in_the_queue(self):
        # The worst case of the measurement: the queue full of requests.
        for number in range(400, 700):
            self.tasks.queue_frame(f"*#1*{number}##", background=True)
        self.assertGreater(self.tasks.queue_depth(), 200)
        latency = await self.latency_of(self.LAMP)
        self.assertLess(latency, 0.5, "the command needed %.2fs" % latency)

    async def test_a_command_during_a_bus_scan_is_under_three_seconds(self):
        # The scan posts its probes exactly as Bridge._probe does: as
        # background, without waiting for the answer.
        async def scan() -> None:
            for number in range(1000, 1400):
                await self.link.post(f"*#1*{number}##", background=True)
                await asyncio.sleep(0.3)

        scanner = asyncio.create_task(scan())
        self.addCleanup(scanner.cancel)
        self.assertTrue(await wait_until(lambda: self.link.sent >= 3, 10))
        latency = await self.latency_of(self.LAMP)
        self.assertLess(latency, 3.0, "the command needed %.2fs" % latency)


class TestDeadAddresses(LoadTestCase):
    """Ten addresses that do not exist must not cost anything.

    Three full rounds of 365 frames at the real five frames a second would run
    for minutes, so this class lifts the limit: what matters here is the
    bookkeeping over whole rounds.  What the *real* limit does to a command is
    measured in :class:`TestCommandLatency`.
    """

    poll_interval = 0            # no poller, and the round runs at full tilt
    refresh_pace = 0.0

    def pacing(self) -> BusPacing:
        # Ten addresses that do not exist are refusals from ten different
        # sources, so the brake engages by design (the strategy counts
        # refusals from at least two addresses as overload) and slows every
        # round down until they are silenced.  That is correct, but at the real
        # 0.5 s per step three rounds of 365 frames would run for minutes:
        # here the steps are small, what the brake does in seconds is measured
        # in test_gateway.py.
        return BusPacing(rate=200.0, size=200.0, reserve=20.0, step=0.01, max_gap=0.05)

    def dead_zones(self) -> list:
        return [self.entity("climate", str(number)) for number in range(1, 11)]

    async def round(self) -> None:
        await self.tasks.refresh_all()
        self.assertTrue(await wait_until(lambda: self.tasks.queue_depth() == 0, 60))
        await asyncio.sleep(0.05)

    async def test_ten_dead_zones_are_silenced_after_three_rounds(self):
        dead = self.dead_zones()
        for entity in dead:
            self.fake.nack_frames.update(entity.poll_frames())
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "WARNING"):
            for number in range(3):
                await self.round()
                if number < 2:
                    self.assertFalse(any(entity.silenced for entity in dead),
                                     "silenced before three rounds were over")
        self.assertTrue(all(entity.silenced for entity in dead),
                        [entity.where for entity in dead if not entity.silenced])
        # A zone asks six things: status, temperature, setpoint, valves and two
        # probes (fan, humidity).  The refusal of a probe is an answer and is
        # never counted, so four of the six show up in the counter - but only
        # the first of each round counted towards silencing, otherwise one
        # round would have been enough.
        self.assertEqual(self.link.refused, 10 * 4 * 3)
        # And nothing stays braked once they are quiet.
        self.assertEqual(self.link.brake_gap, 0.0)

    async def test_a_silenced_zone_drops_out_of_the_next_round(self):
        zone = self.entity("climate", "1")
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "WARNING"):
            for _ in range(3):
                zone.note_refusal()
        self.fake.received.clear()
        await self.round()
        asked = [frame for frame in self.fake.received if frame.startswith("*#4*1*")]
        self.assertEqual(asked, [], "a silenced zone was asked again")


class TestManyShutters(LoadTestCase):
    """Twenty shutters to a middle position - the queue must empty again."""

    async def test_twenty_shutters_reach_their_target(self):
        covers = [self.entity("covers", f"{area}{point}")
                  for area in range(1, 3) for point in range(1, 11)]
        self.assertEqual(len(covers), 20)
        for cover in covers:
            cover.on_command(cover.command_topic, "OPEN")
        # Waiting for the frames, not for the queue: the queue is empty the
        # moment the sender picks the last frame up, a breath before it lands.
        self.assertTrue(await wait_until(
            lambda: len([f for f in self.fake.received if f.startswith("*2*1*")]) == 20, 30),
            self.fake.received)
        self.assertEqual(self.tasks.queue_depth(), 0)
        for cover in covers:
            self.assertIn(f"*2*1*{cover.where}##", self.fake.received)


if __name__ == "__main__":
    unittest.main()
