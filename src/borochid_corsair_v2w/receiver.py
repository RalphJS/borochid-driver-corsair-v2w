"""Borochid driver for Corsair V2W dongles: the receiver itself.

The dongle is a device of its own (as in iCUE); the headset behind it is
another, which this driver announces to the service (``Driver.pair``) while
the headset answers. Link states, published as ``link``:

    standby ─ no headset linked. Some dongles announce this by
              re-enumerating as another product; the service keeps the
              entry and the driver sends nothing.
    offline ─ dongle present, headset not answering (off or out of range).
    online ─ handshake done and the headset answers: it is paired.

The dongle stays on the bus when the headset is off, so writes always
succeed; only a reply proves the headset is there. While online a keep-alive
(which the firmware needs anyway) checks that every ``keepalive_s``.

While offline the driver sleeps until the dongle reports something
unsolicited (a status notice or button event), then re-handshakes at once: a
power-cycled headset has forgotten software mode, and its features then
re-apply their state as it is paired again. A probe backing off from
``probe_s`` to ``probe_max_s`` is the safety net for firmware that announces
nothing.

Software mode survives closing the device, and while it is held the mic
button is dead unless the host handles it. ``stop()`` therefore always
returns the headset (and, as a repair path, the receiver) to hardware mode.
The service stops the paired headset first, so its features can still
undo their own settings.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from borochid.service.channels import PairedChannel
from borochid.service.drivers import Driver, DriverError

from borochid_corsair_v2w import protocol
from borochid_corsair_v2w.profile import Link, Profile, ProfileError
from borochid_corsair_v2w.protocol import Button, Endpoint, Mode, Notice, Op
from borochid_corsair_v2w.session import Session

log = logging.getLogger(__name__)

HEADSET_SLOT = "headset"


class ReceiverDriver(Driver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.profile = Profile.from_manifest(self.manifest.raw)
            self.link = self.profile.link_for(self.channel.ident.pid)
        except ProfileError as e:
            raise DriverError(f"{self.manifest.id}: {e}") from e
        if self.link is Link.WIRED:
            raise DriverError(f"{self.manifest.id}: a headset on its cable is the headset's, not the dongle's")
        self.session = Session(self.channel.write, self.profile.write_gap_s)
        self.online = False
        self.headset: PairedChannel | None = None
        self._software_mode = False
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self.state = {"link": str(self.link), "online": False, "firmware": None}

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._reap)

    def _reap(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("%s: background task failed: %s", self.channel.ident.uid, exc, exc_info=exc)

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        if self.link is Link.STANDBY:
            return  # nothing linked, nothing to talk to
        await self._read_firmware()
        await self._connect()
        self._spawn(self._supervise())

    async def _read_firmware(self) -> None:
        reply = await self.session.request(
            protocol.request(Op.FIRMWARE, Endpoint.RECEIVER),
            lambda r: r if r.answers(protocol.Command.SET, Endpoint.RECEIVER) else None,
            self.profile.reply_timeout_s,
        )
        if reply is not None and len(reply.data) >= 7:
            self.publish({"firmware": ".".join(str(b) for b in reply.data[4:7])})

    async def _handshake(self) -> None:
        s = self.session
        async with s.exclusive():
            await s.send(protocol.request(Op.FIRMWARE, Endpoint.RECEIVER), protocol.request(Op.HEARTBEAT, Endpoint.RECEIVER))
            if self.profile.receiver_software_mode:
                await s.send(protocol.set_mode(Endpoint.RECEIVER, Mode.SOFTWARE))
            await s.send(protocol.set_mode(Endpoint.HEADSET, Mode.SOFTWARE))
            self._software_mode = True
            await asyncio.sleep(0.02)
            s.drain()  # replies to the frames above are not needed
            await s.send(protocol.request(Op.HEARTBEAT))

    async def _alive(self, timeout: float | None = None) -> bool:
        """Only a reply from the headset counts: the dongle answers on its own."""
        reply = await self.session.request(
            protocol.request(Op.BATTERY),
            lambda r: True if r.source == Endpoint.HEADSET else None,
            timeout or self.profile.reply_timeout_s,
        )
        return bool(reply)

    async def _connect(self) -> None:
        async with self.session.exclusive():
            await self._handshake()
            alive = await self._alive(self.profile.first_contact_s)
        await (self._go_online() if alive else self._go_offline())

    async def _go_online(self) -> None:
        if self.online and self.headset is not None:
            return  # a reconnect while it answered: still the same headset
        log.info("%s: headset online", self.channel.ident.uid)
        self.online = True
        self.publish({"link": "online", "online": True})
        self.headset = self.pair(HEADSET_SLOT, shared=self.session)

    async def _go_offline(self) -> None:
        if self.online:
            log.info("%s: headset stopped answering", self.channel.ident.uid)
        self.online = False
        self.headset = None
        self.unpair(HEADSET_SLOT)
        self.publish({"link": "offline", "online": False})

    async def _supervise(self) -> None:
        p = self.profile
        backoff = p.probe_s
        while True:
            try:
                if self.online:
                    backoff = p.probe_s
                    await asyncio.sleep(p.keepalive_s)
                    async with self.session.exclusive():
                        await self.session.send(protocol.request(Op.HEARTBEAT))
                        alive = await self._alive()
                    if not alive:
                        await self._go_offline()
                else:
                    await self._wait_for_device(backoff)
                    await self._connect()
                    self._wake.clear()  # anything that arrived during the probe was ours
                    if not self.online:
                        backoff = min(backoff * 2, p.probe_max_s)
            except OSError as e:
                log.info("%s: supervisor stopping: %s", self.channel.ident.uid, e)
                return

    async def _wait_for_device(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
            log.debug("%s: woken by the dongle", self.channel.ident.uid)
        except TimeoutError:
            log.debug("%s: safety probe after %.0f s", self.channel.ident.uid, timeout)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.headset = None
        self.unpair(HEADSET_SLOT)
        if not self._software_mode:
            return
        with contextlib.suppress(OSError):
            # Hand the mic button (and dongle LED) back to the firmware.
            # Ignored errors: on unplug the device node is already gone.
            await self.session.send(protocol.set_mode(Endpoint.RECEIVER, Mode.HARDWARE))
            await asyncio.sleep(0.05)
            await self.session.send(protocol.set_mode(Endpoint.HEADSET, Mode.HARDWARE))
        self._software_mode = False

    # -- device input ------------------------------------------------------------

    def on_data(self, data: bytes) -> None:
        msg = protocol.classify(data)
        if not self.online and (isinstance(msg, (Button, Notice)) or not self.session.busy):
            # Unsolicited traffic while the headset is off is our cue to probe.
            log.debug("%s: unsolicited while offline: %s", self.channel.ident.uid, data[:8].hex(" "))
            if isinstance(msg, (Button, Notice)):
                self._wake.set()
        if isinstance(msg, (Button, Notice)):
            if self.headset is not None:
                self.headset.deliver(data)  # the headset's buttons and status
        else:
            self.session.feed(msg)

    # -- actions ---------------------------------------------------------------

    async def invoke(self, action: str, params: dict[str, Any]) -> Any:
        if action == "reconnect" and self.link is not Link.STANDBY:
            await self._connect()
            return self.state["link"]
        raise DriverError(f"unknown action {action!r}")
