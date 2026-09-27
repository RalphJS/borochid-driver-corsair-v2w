"""Request/reply on top of Borochid's push-based HID channel.

The channel delivers every input report to the driver, which routes
unsolicited ones (buttons, notices) to features and feeds replies here.
Requests are serialised: the device answers whatever was asked last, so two
interleaved queries would read each other's replies.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

from borochid_corsair_v2w.protocol import Endpoint, Reply

log = logging.getLogger(__name__)

T = TypeVar("T")


class Session:
    def __init__(self, write: Callable[[bytes], Any], write_gap: float = 0.003, max_backlog: int = 64):
        self._write = write
        self.write_gap = write_gap
        self._replies: asyncio.Queue[Reply] = asyncio.Queue(max_backlog)
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Hold the device for a multi-frame sequence. Re-entrant per task."""
        task = asyncio.current_task()
        if self._owner is task:
            yield
            return
        async with self._lock:
            self._owner = task
            try:
                yield
            finally:
                self._owner = None

    @property
    def busy(self) -> bool:
        """A request/sequence is in flight, so incoming replies are expected."""
        return self._lock.locked()

    def feed(self, reply: Reply) -> None:
        if self._replies.full():
            self._replies.get_nowait()  # nobody is waiting; keep the newest
        self._replies.put_nowait(reply)

    def drain(self) -> None:
        while not self._replies.empty():
            self._replies.get_nowait()

    async def send(self, *frames: bytes) -> None:
        async with self.exclusive():
            for f in frames:
                await self._write(f)
                # The firmware drops frames that arrive back to back.
                await asyncio.sleep(self.write_gap)

    async def request(self, frame: bytes, parse: Callable[[Reply], T | None], timeout: float) -> T | None:
        """Send ``frame`` and return the first reply ``parse`` accepts, or None."""
        async with self.exclusive():
            self.drain()
            await self.send(frame)
            deadline = time.monotonic() + timeout
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    reply = await asyncio.wait_for(self._replies.get(), remaining)
                except TimeoutError:
                    break
                if (parsed := parse(reply)) is not None:
                    return parsed
            return None

    async def status(self, frame: bytes, command: int, timeout: float) -> int | None:
        """Send a headset command and return the status byte of its reply
        (0 = accepted), or None if the headset did not answer."""
        return await self.request(
            frame, lambda r: r.data[3] if r.source == Endpoint.HEADSET and r.command == command else None, timeout
        )
