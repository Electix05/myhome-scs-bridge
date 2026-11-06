"""The fake gateway of the unit tests as a server of its own, for run_e2e.py.

Port 20000 speaks OpenWebNet like a real gateway.  Port 20001 takes one JSON object
per line from the test and answers with one:

    {"emit": "*1*1*11##"}   send a frame to every event session, like the bus would
    {"frames": true}        the frames that arrived on command sessions so far
    {"clear": true}         forget them
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.fake_gateway import FakeGateway  # noqa: E402

OPENWEBNET_PORT = 20000
CONTROL_PORT = 20001
EVERY_INTERFACE = "0.0.0.0"


async def main() -> None:
    gateway = FakeGateway(password="12345", mode="none")
    gateway.answers_on_event_session = True      # like a MH201
    await gateway.start(EVERY_INTERFACE, OPENWEBNET_PORT)

    async def control(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                request = json.loads(line)
                if "emit" in request:
                    await gateway.emit(request["emit"])
                    answer = {"ok": True, "event_sessions": len(gateway.event_writers)}
                elif "frames" in request:
                    answer = {"frames": list(gateway.received)}
                elif "clear" in request:
                    gateway.received.clear()
                    answer = {"ok": True}
                else:
                    answer = {"error": f"unknown request {request!r}"}
                writer.write((json.dumps(answer) + "\n").encode())
                await writer.drain()
        except (ConnectionError, ValueError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(control, EVERY_INTERFACE, CONTROL_PORT)
    print("fake gateway ready", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
