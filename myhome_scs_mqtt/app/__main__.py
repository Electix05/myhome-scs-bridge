"""Entry point of the MyHome SCS Bridge add-on: ``python3 -m app``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Callable

from . import __version__
from .bridge.bridge import Bridge
from .settings import DEFAULT_INSTANCE_ID, LOG_LEVELS, Settings, SettingsError

_LOGGER = logging.getLogger("app")

TRACE = 5
# The Supervisor kills an add-on about ten seconds after SIGTERM, and s6-overlay
# in the base image may kill it already 3 s after its own TERM.  Bridge.stop()
# therefore says "offline" first; this is only the outer bound.
STOP_TIMEOUT = 8.0
# Milliseconds in every line: five frames a second are 0.2 s apart, and the
# pace on the bus can only be read from the log with that resolution.
LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"


def configure_logging(level: str) -> None:
    logging.addLevelName(TRACE, "TRACE")
    logging.basicConfig(
        level=LOG_LEVELS.get(level, logging.INFO),
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )


async def main(
    load_settings: Callable[[], Settings] = Settings.load,
    bridge_factory: Callable[[Settings], Bridge] = Bridge,
) -> int:
    """Run the bridge until SIGTERM/SIGINT; the exit code of the add-on."""
    configure_logging("info")
    try:
        # Reads files and may ask the Supervisor - not on the event loop.
        settings = await asyncio.to_thread(load_settings)
    except SettingsError as err:
        _LOGGER.error("%s", err)
        return 1
    logging.getLogger().setLevel(LOG_LEVELS.get(settings.log_level, logging.INFO))
    _LOGGER.info("MyHome SCS Bridge %s starting - gateway %s:%s, MQTT %s:%s",
                 __version__, settings.gateway.host, settings.gateway.port,
                 settings.mqtt.host, settings.mqtt.port)
    # Only when it is not the default: with two instances the log has to say
    # which one is talking, and a single instance needs no extra line.
    if settings.instance_id != DEFAULT_INSTANCE_ID or settings.name_prefix:
        _LOGGER.info("Instance '%s' - topics under %s/, names prefixed with %r",
                     settings.instance_id, settings.mqtt.base_topic, settings.name_prefix)

    bridge = bridge_factory(settings)
    loop = asyncio.get_running_loop()
    stopping: asyncio.Future[None] = loop.create_future()

    def request_stop() -> None:
        if not stopping.done():
            stopping.set_result(None)

    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:  # pragma: no cover - not on every platform
            signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(request_stop))

    runner = asyncio.create_task(bridge.run(), name="bridge")
    try:
        await asyncio.wait({runner, stopping}, return_when=asyncio.FIRST_COMPLETED)
        _LOGGER.info("Shutting down")
        runner.cancel()
        (outcome,) = await asyncio.gather(runner, return_exceptions=True)
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                await bridge.stop()
        except TimeoutError:
            _LOGGER.warning("Shutdown took longer than %.0fs", STOP_TIMEOUT)
    finally:
        for sig in signals:
            try:
                loop.remove_signal_handler(sig)
            except NotImplementedError:  # pragma: no cover - not on every platform
                pass

    if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
        _LOGGER.error("Bridge stopped with an error: %s", outcome, exc_info=outcome)
        return 1
    return 0


def run() -> None:
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:  # pragma: no cover - interrupted before the handlers stood
        sys.exit(0)


if __name__ == "__main__":
    run()
