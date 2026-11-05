"""The background of the bridge: outbox, sender, refresh, poller and ticker."""

from __future__ import annotations

import asyncio
import unittest

from myhome_scs_mqtt.app.bridge import background as background_module
from myhome_scs_mqtt.app.bridge.background import BackgroundTasks, Outbox
from myhome_scs_mqtt.app.bridge.registry import Registry
from myhome_scs_mqtt.app.devices.base import Entity
from myhome_scs_mqtt.app.own import gateway as gateway_module
from myhome_scs_mqtt.app.own.gateway import BusPacing, Gateway

from .fake_gateway import FakeGateway
from .helpers import FakeClock, Recorder, wait_until


class StubGateway:
    """Records what the sender hands over; answers from a table."""

    def __init__(self, results: dict | None = None) -> None:
        self.connected = True
        self.sent = 0
        self.received = 0
        self.throttled = 0
        self.refused = 0
        self.unanswered = 0
        self.brake_gap = 0.0
        self.frames: list[tuple[str, bool]] = []
        # The lane every frame came from, in the same order as `frames`.
        self.lanes: list[bool] = []
        self.results = results or {}
        self.gate = asyncio.Event()
        self.gate.set()

    async def send(self, frame: str, *, quiet: bool = False,
                   background: bool = False) -> bool | None:
        await self.gate.wait()
        # A real gateway always waits for its answer; let the loop breathe.
        await asyncio.sleep(0)
        self.frames.append((frame, quiet))
        self.lanes.append(background)
        self.sent += 1
        result = self.results.get(frame, True)
        if isinstance(result, BaseException):
            raise result
        return result


def registry_with(definitions: dict | None = None) -> Registry:
    registry = Registry(Recorder().context())
    registry.build(definitions or {}, [])
    return registry


class TestOutbox(unittest.IsolatedAsyncioTestCase):
    async def drain(self, outbox: Outbox) -> list[str]:
        """Every frame, the foreground lane first - what two senders do."""
        frames = []
        for background in (False, True):
            while outbox.qsize(background):
                frames.append((await outbox.get(background)).frame)
        return frames

    async def test_foreground_first_each_lane_in_order(self):
        outbox = Outbox()
        outbox.put("*#1*31##", background=True)
        outbox.put("*#1*32##", background=True)
        outbox.put("*1*1*12##")
        outbox.put("*1*0*13##")
        self.assertEqual(await self.drain(outbox),
                         ["*1*1*12##", "*1*0*13##", "*#1*31##", "*#1*32##"])

    async def test_the_background_lane_is_bounded(self):
        outbox = Outbox(max_background=3)
        with self.assertLogs(background_module._LOGGER, "WARNING") as logs:
            for where in range(40, 45):
                outbox.put(f"*#1*{where}##", background=True)
        self.assertEqual(outbox.dropped, 2)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(await self.drain(outbox), ["*#1*42##", "*#1*43##", "*#1*44##"])

    async def test_commands_are_never_dropped(self):
        outbox = Outbox(max_background=3)
        for index in range(1000):
            outbox.put(f"*1*{index % 2}*12##")
        self.assertEqual(outbox.qsize(), 1000)
        self.assertEqual(outbox.dropped, 0)

    async def test_a_waiting_request_is_not_queued_twice(self):
        # While the gateway is away, sensors keep asking every few seconds.
        outbox = Outbox()
        for _ in range(100):
            outbox.put("*#1*1010*7##")
            outbox.put("*#2*21*10##", background=True)
        self.assertEqual(outbox.qsize(), 2)
        # Commands are not requests: two clicks are two frames.
        outbox.put("*2*1*21##")
        outbox.put("*2*1*21##")
        self.assertEqual(outbox.qsize(), 4)

    async def test_a_foreground_request_overtakes_its_background_twin(self):
        outbox = Outbox()
        for where in range(30, 40):
            outbox.put(f"*#2*{where}*10##", background=True)
        outbox.put("*#2*39*10##")                     # a drive needs this one now
        first = await outbox.get()
        self.assertEqual((first.frame, first.background), ("*#2*39*10##", False))
        self.assertEqual(outbox.qsize(), 9)

    async def test_the_background_lane_can_be_emptied_commands_stay(self):
        # After a bus scan: the queued requests are minutes old, the scan has
        # just asked everything itself (measured 24.09.2026: 40 of them ran on).
        outbox = Outbox()
        for where in range(30, 40):
            outbox.put(f"*#1*{where}##", background=True)
        outbox.put("*1*1*12##")
        self.assertEqual(outbox.clear_background(), 10)
        self.assertEqual(outbox.qsize(), 1)
        self.assertEqual((await outbox.get()).frame, "*1*1*12##")
        self.assertEqual(outbox.clear_background(), 0)

    async def test_get_waits_for_a_frame(self):
        outbox = Outbox()
        waiting = asyncio.create_task(outbox.get())
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())
        outbox.put("*1*1*12##")
        self.assertEqual((await asyncio.wait_for(waiting, 1)).frame, "*1*1*12##")


class SenderTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.runners: list[asyncio.Task] = []

    async def asyncTearDown(self) -> None:
        for runner in self.runners:
            runner.cancel()
        await asyncio.gather(*self.runners, return_exceptions=True)

    def start(self, gateway, registry: Registry | None = None, **kwargs) -> BackgroundTasks:
        options = {"poll_interval": 0, "tick_interval": 3600}
        options.update(kwargs)
        tasks = BackgroundTasks(gateway, registry or registry_with(), **options)
        self.runners.append(asyncio.create_task(tasks.run()))
        return tasks


class TestSender(SenderTestCase):
    async def test_the_sender_does_not_pace_by_itself(self):
        # Tempo and brake belong to the gateway's BusPacing alone; a pause in
        # here would add up with it.  Trouble does not slow the queue down.
        gateway = StubGateway({"*#1*41##": None, "*#1*42##": False, "*#1*43##": False})
        tasks = self.start(gateway)
        loop = asyncio.get_running_loop()
        started = loop.time()
        for where in range(40, 60):
            tasks.queue_frame(f"*#1*{where}##", background=True)
        self.assertTrue(await wait_until(lambda: len(gateway.frames) == 20, interval=0.005))
        self.assertLess(loop.time() - started, 0.5)
        self.assertFalse(hasattr(tasks, "backoff"))

    async def test_the_latency_of_every_frame_is_logged_on_debug(self):
        clock = FakeClock()
        gateway = StubGateway({"*#1*42##": False})
        gateway.gate.clear()
        tasks = self.start(gateway, clock=clock)
        with self.assertLogs(background_module._LOGGER, "DEBUG") as logs:
            tasks.queue_frame("*#1*42##")
            clock.advance(1.5)
            gateway.gate.set()
            self.assertTrue(await wait_until(lambda: any("sent" in r.getMessage()
                                                         for r in logs.records)))
        line = next(r.getMessage() for r in logs.records if "sent" in r.getMessage())
        self.assertEqual(line, "sent *#1*42## after 1.50s in the queue, 0.00s until: refused")
        self.assertEqual(logs.records[0].levelname, "DEBUG")

    async def test_a_command_queued_last_overtakes_the_queued_requests(self):
        # Each lane has its own sender, so the background may have one frame in
        # flight - but never more than that in front of a command.
        gateway = StubGateway()
        gateway.gate.clear()
        tasks = self.start(gateway)
        tasks.queue_frame("*#1*41##", background=True)
        tasks.queue_frame("*#1*42##", background=True)
        tasks.queue_frame("*1*1*12##")
        gateway.gate.set()
        self.assertTrue(await wait_until(lambda: len(gateway.frames) == 3))
        sent = [frame for frame, _quiet in gateway.frames]
        self.assertLess(sent.index("*1*1*12##"), sent.index("*#1*42##"), sent)

    async def test_probes_are_sent_quietly(self):
        gateway = StubGateway()
        tasks = self.start(gateway)
        tasks.queue_frame("*#4*1*11##", probe=True, background=True)
        tasks.queue_frame("*#4*1*0##", background=True)
        self.assertTrue(await wait_until(lambda: len(gateway.frames) == 2))
        self.assertEqual(gateway.frames, [("*#4*1*11##", True), ("*#4*1*0##", False)])

    async def test_a_command_overtakes_the_refresh(self):
        # Measured on the real installation: 42 seconds behind the refresh.
        gateway = StubGateway()
        gateway.gate.clear()
        tasks = self.start(gateway)
        for number in range(20):
            tasks.queue_frame(f"*#1*{30 + number}##", background=True)
        await asyncio.sleep(0.05)               # the sender holds the first one
        tasks.queue_frame("*2*1*21##")
        gateway.gate.set()
        self.assertTrue(await wait_until(lambda: len(gateway.frames) == 21))
        self.assertLess([f for f, _q in gateway.frames].index("*2*1*21##"), 2)

    async def test_an_unexpected_error_does_not_stop_the_queue(self):
        gateway = StubGateway({"*1*1*12##": RuntimeError("bug")})
        tasks = self.start(gateway)
        with self.assertLogs(background_module._LOGGER, "ERROR"):
            tasks.queue_frame("*1*1*12##")
            tasks.queue_frame("*1*1*13##")
            self.assertTrue(await wait_until(lambda: len(gateway.frames) == 2))

    async def test_queue_depth_is_what_entities_see(self):
        gateway = StubGateway()
        gateway.gate.clear()
        tasks = self.start(gateway)
        for where in range(10, 16):
            tasks.queue_frame(f"*1*1*{where}##")
        self.assertTrue(await wait_until(lambda: tasks.queue_depth() == 5))


class Counting(Entity):
    section = "test"
    needs_tick = True

    def __init__(self, context, config, fail: bool = False) -> None:
        super().__init__(context, config)
        self.ticks = 0
        self.polled = 0
        self.fail = fail

    def discovery(self):
        return []

    def tick(self) -> None:
        self.ticks += 1
        if self.fail:
            raise RuntimeError("broken entity")

    def poll_frames(self):
        self.polled += 1
        if self.fail:
            raise RuntimeError("broken entity")
        return (f"*#1*{self.where}##",)


class TestSilencingDeadAddresses(SenderTestCase):
    """An address the gateway keeps refusing drops out of the refresh.

    Measured on 24.09.2026: one entry with an address that does not exist was
    refused in every round and slowed the whole installation down.
    """

    DEAD = "*#1*12342##"
    ALIVE = "*#1*12##"

    def build(self, dead_after: int = 3):
        recorder = Recorder()
        context = recorder.context()
        context.dead_after = dead_after
        registry = Registry(context)
        registry.build({"lights": [{"where": "12"}, {"where": "12342"}]}, [])
        return recorder, registry, registry.entities[1]

    async def send_refusals(self, tasks, gateway, times: int, probe: bool = False) -> None:
        for number in range(1, times + 1):
            tasks.queue_frame(self.DEAD, probe=probe, background=True)
            self.assertTrue(await wait_until(lambda n=number: len(gateway.frames) == n))

    async def test_three_refusals_silence_the_address(self):
        recorder, registry, dead = self.build()
        gateway = StubGateway({self.DEAD: False})
        tasks = self.start(gateway, registry)
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "WARNING"):
            await self.send_refusals(tasks, gateway, 3)
        self.assertTrue(dead.silenced)
        self.assertEqual(recorder.last("myhome/light/12342/reachable"), "offline")

    async def test_a_silenced_address_drops_out_of_the_refresh(self):
        _recorder, registry, dead = self.build()
        gateway = StubGateway({self.DEAD: False})
        tasks = self.start(gateway, registry, refresh_pace=0, auto_discover=lambda: False)
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "WARNING"):
            await self.send_refusals(tasks, gateway, 3)
        gateway.frames.clear()
        await tasks.refresh_all()
        self.assertTrue(await wait_until(lambda: tasks.queue_depth() == 0))
        await asyncio.sleep(0.05)               # let the last frame through
        sent = [frame for frame, _quiet in gateway.frames]
        self.assertIn(self.ALIVE, sent)
        self.assertFalse([frame for frame in sent if "12342" in frame], sent)
        self.assertTrue(dead.silenced)

    async def test_a_refused_probe_never_silences_anything(self):
        # "Does this lamp know a colour?" - a refusal is an answer.
        _recorder, registry, dead = self.build()
        gateway = StubGateway({self.DEAD: False})
        tasks = self.start(gateway, registry)
        await self.send_refusals(tasks, gateway, 6, probe=True)
        self.assertFalse(dead.silenced)

    async def test_an_answer_brings_the_address_back(self):
        recorder, registry, dead = self.build()
        gateway = StubGateway({self.DEAD: False})
        tasks = self.start(gateway, registry)
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "WARNING"):
            await self.send_refusals(tasks, gateway, 3)
        gateway.results[self.DEAD] = True
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "INFO"):
            tasks.queue_frame(self.DEAD, background=True)
            self.assertTrue(await wait_until(lambda: not dead.silenced, 2))
        self.assertEqual(recorder.last("myhome/light/12342/reachable"), "online")

    async def test_without_the_option_nothing_is_silenced(self):
        _recorder, registry, dead = self.build(dead_after=0)
        gateway = StubGateway({self.DEAD: False})
        tasks = self.start(gateway, registry)
        await self.send_refusals(tasks, gateway, 6)
        self.assertFalse(dead.silenced)

    async def test_a_busy_round_does_not_multiply_one_refusal_into_three(self):
        # The very first round of a plain light asks two different things
        # (status and level).  A gateway that refuses in rows must not make
        # that look like two refusals in a row of one address.
        _recorder, registry, dead = self.build()
        level = "*#1*12342*1##"
        gateway = StubGateway({self.DEAD: False, level: False})
        tasks = self.start(gateway, registry, refresh_pace=0, auto_discover=lambda: False)
        await self.refresh_round(tasks)
        self.assertEqual(len(gateway.frames), 4, gateway.frames)   # two lights, two frames each
        self.assertFalse(dead.silenced, "one busy round already silenced the address")
        with self.assertLogs("myhome_scs_mqtt.app.devices.base", "WARNING"):
            for _ in range(2):                      # two more rounds, one refusal each
                await self.refresh_round(tasks)
        self.assertTrue(dead.silenced)

    async def refresh_round(self, tasks) -> None:
        await tasks.refresh_all()
        self.assertTrue(await wait_until(lambda: tasks.queue_depth() == 0))
        await asyncio.sleep(0.05)                   # let the last frame through


class TestSharedAddressIsNeverSilencedByItself(SenderTestCase):
    """A presence sensor shares its bus address with a light on purpose.

    WISSEN.md "Lichtmanagement (BMSE)": the sensors sit on the WHO of the
    lighting.  From the address alone the two cannot be told apart, so a
    refusal there must not silence the other one - a lamp that switches
    perfectly well would otherwise show up as unavailable.
    """

    LUX = "*#1*12*6##"

    def build(self):
        recorder = Recorder()
        context = recorder.context()
        context.dead_after = 3
        registry = Registry(context)
        registry.build({"lights": [{"where": "12"}],
                        "motion_sensors": [{"where": "12"}]}, [])
        light = next(e for e in registry.entities if e.section == "lights")
        sensor = next(e for e in registry.entities if e.section == "motion_sensors")
        return registry, light, sensor

    async def test_a_refused_sensor_query_leaves_the_light_alone(self):
        registry, light, sensor = self.build()
        gateway = StubGateway({self.LUX: False})
        tasks = self.start(gateway, registry)
        for number in range(1, 4):
            tasks.queue_frame(self.LUX, background=True)
            self.assertTrue(await wait_until(
                lambda n=number: gateway.frames.count((self.LUX, False)) == n))
        self.assertFalse(light.silenced, "the sensor's refusal silenced the light too")
        self.assertFalse(sensor.silenced, "a shared address must never silence itself")


class TestRefresh(SenderTestCase):
    def registry(self) -> Registry:
        return registry_with({"lights": [{"where": "12"}, {"where": "13"}],
                              "climate": [{"where": "2"}]})

    async def test_the_refresh_trickles_instead_of_bursting(self):
        gateway = StubGateway()
        gateway.gate.clear()                    # nothing leaves: watch the queue
        tasks = BackgroundTasks(gateway, self.registry(), poll_interval=0, refresh_pace=0.05)
        times: list[float] = []
        loop = asyncio.get_running_loop()
        original_put = tasks.outbox.put

        def put(frame, probe=False, background=False, countable=True):
            times.append(loop.time())
            original_put(frame, probe=probe, background=background, countable=countable)

        tasks.outbox.put = put
        tasks.start_refresh()
        await asyncio.sleep(0.01)
        self.assertLessEqual(tasks.queue_depth(), 1, "the refresh emptied itself in one go")
        self.assertTrue(await wait_until(lambda: not tasks.refreshing, 5))
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        self.assertGreater(len(gaps), 5)
        self.assertGreater(min(gaps), 0.04)

    async def test_what_the_refresh_asks(self):
        gateway = StubGateway()
        gateway.gate.clear()
        tasks = BackgroundTasks(gateway, self.registry(), poll_interval=0, refresh_pace=0)
        await tasks.refresh_all()
        queued = [await tasks.outbox.get(True) for _ in range(tasks.queue_depth())]
        frames = [item.frame for item in queued]
        self.assertEqual(frames[0], "*#1*0##")          # feeds the auto discovery
        for expected in ("*#1*12##", "*#1*12*1##", "*#1*13##", "*#4*2*0##", "*#4*2*12##"):
            self.assertIn(expected, frames)
        self.assertTrue(all(item.background for item in queued))
        probes = {item.frame for item in queued if item.probe}
        self.assertEqual(probes, {"*#4*2*11##", "*#4*2*60##"})

    async def test_without_auto_discovery_no_general_request(self):
        tasks = BackgroundTasks(StubGateway(), self.registry(), poll_interval=0, refresh_pace=0,
                                auto_discover=lambda: False)
        await tasks.refresh_all()
        self.assertNotIn("*#1*0##", [(await tasks.outbox.get(True)).frame
                                     for _ in range(tasks.queue_depth())])

    async def test_no_second_refresh_while_one_runs(self):
        tasks = BackgroundTasks(StubGateway(), self.registry(), poll_interval=0, refresh_pace=0.05)
        tasks.start_refresh()
        first = tasks._refresh_task
        tasks.start_refresh()
        self.assertIs(tasks._refresh_task, first)
        await tasks.stop_refresh()
        self.assertFalse(tasks.refreshing)

    async def test_a_failing_refresh_is_logged(self):
        registry = registry_with()
        registry.register(Counting(registry.context, {"where": "1"}, fail=True))
        tasks = BackgroundTasks(StubGateway(), registry, poll_interval=0, refresh_pace=0)
        with self.assertLogs(background_module._LOGGER, "ERROR"):
            tasks.start_refresh()
            await asyncio.sleep(0.05)

    async def test_a_broken_entity_does_not_stop_the_refresh_of_the_others(self):
        registry = registry_with()
        good_before = Counting(registry.context, {"where": "1"})
        bad = Counting(registry.context, {"where": "2"}, fail=True)
        good_after = Counting(registry.context, {"where": "3"})
        for entity in (good_before, bad, good_after):
            registry.register(entity)
        tasks = BackgroundTasks(StubGateway(), registry, poll_interval=0, refresh_pace=0,
                                auto_discover=lambda: False)
        with self.assertLogs(background_module._LOGGER, "ERROR") as logs:
            await tasks.refresh_all()
            await tasks.refresh_all()            # a lasting fault is not repeated loudly
        self.assertEqual(len([r for r in logs.records if r.levelname == "ERROR"]), 1)
        self.assertEqual((good_after.polled, bad.polled), (2, 2))
        frames = [(await tasks.outbox.get(True)).frame for _ in range(tasks.queue_depth())]
        self.assertEqual(frames, ["*#1*1##", "*#1*3##"])      # requests are not queued twice


class TestTheRefreshPace(SenderTestCase):
    """One round spreads over half the poll interval instead of bursting.

    Measured on 24.09.2026: 40 devices make 68 requests, and at the fixed pace
    of 0.5 s they went out as a 34-second burst of two frames a second - no
    matter whether the interval was 60 or 300 seconds.  That burst showed up as
    "60 sent, 180 received per minute" out of nowhere.
    """

    def tasks(self, poll_interval: float, **kwargs) -> BackgroundTasks:
        return BackgroundTasks(StubGateway(), registry_with(),
                              poll_interval=poll_interval, **kwargs)

    def test_a_round_takes_half_the_interval(self):
        tasks = self.tasks(300)
        # The measured installation: 68 requests over 150 s instead of 34 s.
        self.assertAlmostEqual(tasks.refresh_pace(68), 150 / 68)
        self.assertAlmostEqual(tasks.refresh_pace(68) * 68, 150.0)

    def test_the_floor_holds_for_a_big_installation(self):
        # 200 devices, 365 requests: half of 300 s would be 0.41 s per frame -
        # faster than the bus limit allows, so the floor wins.
        tasks = self.tasks(300)
        self.assertEqual(tasks.refresh_pace(365), background_module.REFRESH_PACE)
        # With a longer interval the round stretches as it should.
        self.assertAlmostEqual(self.tasks(900).refresh_pace(365), 450 / 365)

    def test_the_cap_holds_for_a_small_installation(self):
        tasks = self.tasks(3600)
        self.assertEqual(tasks.refresh_pace(10), background_module.MAX_REFRESH_PACE)

    def test_without_polling_the_plain_pace_is_used(self):
        self.assertEqual(self.tasks(0).refresh_pace(50), background_module.REFRESH_PACE)
        self.assertEqual(self.tasks(300).refresh_pace(0), background_module.REFRESH_PACE)

    async def test_the_frames_really_go_out_at_that_pace(self):
        gateway = StubGateway()
        gateway.gate.clear()                        # watch the queue, not the bus
        registry = registry_with({"lights": [{"where": str(10 + n)} for n in range(10)]})
        tasks = BackgroundTasks(gateway, registry, poll_interval=20,
                                auto_discover=lambda: False)
        pace = tasks.refresh_pace(20)                # 10 lights, 2 frames each
        self.assertAlmostEqual(pace, 0.5)
        loop = asyncio.get_running_loop()
        started = loop.time()
        task = asyncio.create_task(tasks.refresh_all())
        self.addCleanup(task.cancel)
        self.assertTrue(await wait_until(lambda: tasks.queue_depth() >= 3, 5))
        elapsed = loop.time() - started
        self.assertGreater(elapsed, 2 * pace * 0.8, "the round burst out at once")

    async def test_a_round_that_cannot_be_stretched_says_so(self):
        # A hint for the user: 200 devices at poll_interval 300 cannot be spread
        # any further - the interval itself is too short for that many devices.
        registry = registry_with({"lights": [{"where": str(10 + n)} for n in range(10)]})
        tasks = BackgroundTasks(StubGateway(), registry, poll_interval=1,
                               auto_discover=lambda: False)
        with self.assertLogs(background_module._LOGGER, "INFO") as logs:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(tasks.refresh_all(), 0.1)
        self.assertIn("longer interval", logs.output[0])


class TestPoller(SenderTestCase):
    async def test_refreshes_only_while_connected_and_not_scanning(self):
        gateway = StubGateway()
        scanning = {"now": False}
        tasks = self.start(gateway, registry_with({"lights": [{"where": "12"}]}),
                           poll_interval=0.05, refresh_pace=0.01,
                           is_scanning=lambda: scanning["now"])
        self.assertTrue(await wait_until(lambda: ("*#1*12##", False) in gateway.frames))

        gateway.connected = False
        # A refresh that already runs may still finish; nothing new starts.
        self.assertTrue(await wait_until(lambda: not tasks.refreshing and not tasks.queue_depth()))
        await asyncio.sleep(0.05)                 # the last frame leaves the sender
        gateway.frames.clear()
        await asyncio.sleep(0.2)
        self.assertEqual(gateway.frames, [])

        gateway.connected = True
        scanning["now"] = True
        await asyncio.sleep(0.2)
        self.assertEqual(gateway.frames, [])


class TestTicker(SenderTestCase):
    async def test_entities_tick_and_a_broken_one_does_not_stop_the_rest(self):
        registry = registry_with()
        good = Counting(registry.context, {"where": "1"})
        bad = Counting(registry.context, {"where": "2"}, fail=True)
        registry.register(bad)
        registry.register(good)
        gateway = StubGateway()
        with self.assertLogs(background_module._LOGGER, "DEBUG") as logs:
            tasks = self.start(gateway, registry, tick_interval=0.02)
            self.assertTrue(await wait_until(lambda: good.ticks >= 5))
        loud = [r for r in logs.records if r.levelname == "ERROR"]
        self.assertEqual(len(loud), 1, "a broken tick must not flood the log")
        self.assertGreaterEqual(bad.ticks, 5)

        gateway.sent = 120
        gateway.refused, gateway.unanswered, gateway.brake_gap = 7, 2, 0.5
        self.assertTrue(await wait_until(lambda: tasks.bus_load()["sent_per_minute"] > 0))
        load = tasks.bus_load()
        for key in ("sent", "received", "sent_per_minute", "received_per_minute", "queue",
                    "throttled", "dropped"):
            self.assertIn(key, load)
        self.assertEqual((load["refused"], load["unanswered"], load["brake_gap"]), (7, 2, 0.5))


class TestAgainstTheFakeGateway(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fake = FakeGateway(mode="none")
        port = await self.fake.start()
        # reserve=0: this class measures the ceiling and the brake; what the
        # command reserve does is measured in TestCommandsDuringBackgroundLoad.
        self.link = Gateway("127.0.0.1", port, "", reconnect_min=0.05, reconnect_max=0.2,
                            pacing=BusPacing(step=0.3, max_gap=0.3, reserve=0.0))
        await self.link.start()
        self.assertTrue(await wait_until(lambda: self.link.connected))
        self.registry = registry_with({"lights": [{"where": "12"}], "climate": [{"where": "2"}]})
        self.tasks = BackgroundTasks(self.link, self.registry, poll_interval=0, refresh_pace=0.02)
        self.runner = asyncio.create_task(self.tasks.run())

    async def asyncTearDown(self) -> None:
        self.runner.cancel()
        await asyncio.gather(self.runner, return_exceptions=True)
        await self.link.stop()
        await self.fake.stop()

    async def test_commands_reach_the_bus(self):
        self.tasks.queue_frame("*1*1*12##")
        self.assertTrue(await wait_until(lambda: "*1*1*12##" in self.fake.received))

    async def test_piled_up_refusals_brake_refused_probes_do_not(self):
        probes = [f"*#4*{zone}*11##" for zone in range(1, 6)]
        self.fake.nack_frames.update(probes)
        for frame in probes:
            self.tasks.queue_frame(frame, probe=True, background=True)
        self.assertTrue(await wait_until(lambda: probes[-1] in self.fake.received))
        await asyncio.sleep(0.1)
        self.assertEqual((self.link.refused, self.link.brake_gap), (0, 0.0))
        self.assertEqual(self.tasks.bus_load()["brake_gap"], 0.0)

        refusals = [f"*#1*7{number}##" for number in range(3)]
        self.fake.nack_frames.update(refusals)
        with self.assertLogs("myhome_scs_mqtt.app.own.session", "WARNING"):
            for frame in refusals:
                self.tasks.queue_frame(frame, background=True)
            self.assertTrue(await wait_until(lambda: self.link.brake_gap == 0.3))
        load = self.tasks.bus_load()
        self.assertEqual((load["refused"], load["brake_gap"]), (3, 0.3))

    async def test_the_real_send_rate_while_ceiling_and_brake_act(self):
        # A scan burst empties the bucket - from then on the ceiling holds
        # 0.2 s per frame.  Refusals pile up and the brake wants 0.3 s.
        # Measured at the gateway: the stricter rule wins, the waits do not
        # add up to 0.5 s.
        for _ in range(10):
            self.assertTrue(await self.link.post("*#1*90##"))
        frames = [f"*#1*5{number}##" for number in range(8)]
        self.fake.nack_frames.update(frames)
        with self.assertLogs("myhome_scs_mqtt.app.own.session", "WARNING"):
            for frame in frames:
                # Background: only that lane waits for the brake at all.
                self.tasks.queue_frame(frame, background=True)
            # Counted once the refusal is back, a moment after the frame arrived.
            self.assertTrue(await wait_until(lambda: self.link.refused == 8, 10))
        arrivals = [at for at, frame in self.fake.timeline if frame in frames]
        gaps = [later - earlier for earlier, later in zip(arrivals, arrivals[1:])]
        self.assertTrue(all(0.15 < gap < 0.27 for gap in gaps[:2]), gaps)    # ceiling
        self.assertTrue(all(0.27 < gap < 0.45 for gap in gaps[2:]), gaps)    # brake
        rate = len(gaps[2:]) / sum(gaps[2:])
        self.assertAlmostEqual(rate, 1 / 0.3, delta=0.5)

    async def test_a_refresh_reaches_the_gateway(self):
        self.tasks.start_refresh()
        for frame in ("*#1*0##", "*#1*12##", "*#4*2*0##", "*#4*2*12##", "*#4*2*19##"):
            self.assertTrue(await wait_until(lambda f=frame: f in self.fake.received, 5), frame)


class TestCommandsDuringBackgroundLoad(unittest.IsolatedAsyncioTestCase):
    """The blockade of 24.09.2026 as a test (berichte/22-messung-buslast.md).

    A queue full of braked background requests must not delay a command.  With
    one sender for both lanes and a brake that holds back everything, this test
    fails - the command then waits for the brake of the frame in front of it.
    """

    async def asyncSetUp(self) -> None:
        self.fake = FakeGateway(mode="none")
        port = await self.fake.start()
        # A brake of a whole second: clearly longer than the latency a command
        # may have, and reached after three refusals.
        self.link = Gateway("127.0.0.1", port, "", reconnect_min=0.05, reconnect_max=0.2,
                            pacing=BusPacing(step=1.0, max_gap=1.0))
        await self.link.start()
        self.assertTrue(await wait_until(lambda: self.link.connected))
        self.tasks = BackgroundTasks(self.link, registry_with(), poll_interval=0,
                                     tick_interval=3600)
        self.runner = asyncio.create_task(self.tasks.run())

    async def asyncTearDown(self) -> None:
        self.runner.cancel()
        await asyncio.gather(self.runner, return_exceptions=True)
        await self.link.stop()
        await self.fake.stop()

    def arrival(self, frame: str) -> float:
        return next(at for at, sent in self.fake.timeline if sent == frame)

    async def test_a_command_gets_through_while_the_background_is_braked(self):
        dead = [f"*#1*9{number:02d}##" for number in range(40)]
        self.fake.nack_frames.update(dead)
        with self.assertLogs("myhome_scs_mqtt.app.own.session", "WARNING"):
            for frame in dead:
                self.tasks.queue_frame(frame, background=True)
            self.assertTrue(await wait_until(lambda: self.link.brake_gap >= 1.0, 10))
        self.assertGreater(self.tasks.queue_depth(), 20, "the queue emptied too fast")

        loop = asyncio.get_running_loop()
        queued = loop.time()
        self.tasks.queue_frame("*1*1*12##")
        self.assertTrue(await wait_until(lambda: "*1*1*12##" in self.fake.received, 5))
        latency = self.arrival("*1*1*12##") - queued
        self.assertLess(latency, 0.4, "the command needed %.2fs" % latency)

    async def test_a_command_gets_through_even_when_the_background_falls_silent(self):
        # One command session, no frame id: the sender holds its lock until the
        # answer arrives.  A background frame the gateway never answers must
        # therefore give up quickly (BACKGROUND_TIMEOUT), otherwise a command
        # waits the full COMMAND_TIMEOUT of six seconds behind it.
        silent = [f"*#1*8{number:02d}##" for number in range(10)]
        self.fake.silent_frames.update(silent)
        for frame in silent:
            self.tasks.queue_frame(frame, background=True)
        await asyncio.sleep(0.05)               # the sender waits in request()

        loop = asyncio.get_running_loop()
        queued = loop.time()
        self.tasks.queue_frame("*1*1*12##")
        self.assertTrue(await wait_until(lambda: "*1*1*12##" in self.fake.received, 10))
        latency = self.arrival("*1*1*12##") - queued
        # Measured 24.09.2026: 1.95 s - the command waits for the timeout of
        # the silent frame in front of it and not a moment longer, so with the
        # six seconds a command gets it would be six.
        self.assertLess(latency, gateway_module.BACKGROUND_TIMEOUT + 0.5,
                        "the command waited behind a silent background frame: %.2fs" % latency)

    async def test_the_background_keeps_running_after_the_command(self):
        # The command must not cost the background its place either.
        self.tasks.queue_frame("*#1*31##", background=True)
        self.tasks.queue_frame("*1*1*12##")
        self.assertTrue(await wait_until(
            lambda: {"*#1*31##", "*1*1*12##"} <= set(self.fake.received), 5))


if __name__ == "__main__":
    unittest.main()
