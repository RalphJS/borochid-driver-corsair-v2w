"""Borochid driver for Corsair V2W headsets.

Link states, published as ``link``:

    standby ─ dongle present, headset switched off. Some dongles announce
              this by re-enumerating as another product; the service keeps
              the entry and the driver sends nothing.
    wired ─ USB cable: no V2W at all; only host audio applies.
    offline ─ dongle present, headset not answering (off or out of range).
    online ─ handshake done and the headset answers.

The dongle stays on the bus when the headset is off, so writes always
succeed; only a reply proves the headset is there. While online a keep-alive
(which the firmware needs anyway) checks that every ``keepalive_s``.

While offline the driver sleeps until the dongle reports something
unsolicited (a status notice or button event), then re-handshakes at once: a
power-cycled headset has forgotten software mode, and every feature then
re-applies its state. A probe backing off from ``probe_s`` to
``probe_max_s`` is the safety net for firmware that announces nothing.

Software mode survives closing the device, and while it is held the mic
button is dead unless the host handles it. ``stop()`` therefore always
returns the headset (and, as a repair path, the receiver) to hardware mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from borochid.service.drivers import Driver, DriverError

from borochid_corsair_v2w import protocol
from borochid_corsair_v2w.features import Feature, Setting, SettingError
from borochid_corsair_v2w.features.battery import Battery
from borochid_corsair_v2w.features.lighting import Lighting
from borochid_corsair_v2w.features.mic_button import MicButton
from borochid_corsair_v2w.features.sidetone import Sidetone
from borochid_corsair_v2w.profile import Link, Profile, ProfileError
from borochid_corsair_v2w.protocol import Button, Endpoint, Mode, Notice, Op
from borochid_corsair_v2w.session import Session

log = logging.getLogger(__name__)

# Lighting first: the handshake darkens the LEDs, so repaint before anything slower.
FEATURE_TYPES: dict[str, type[Feature]] = {
    "lighting": Lighting,
    "battery": Battery,
    "sidetone": Sidetone,
    "mic_button": MicButton,
}


class HeadsetDriver(Driver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.profile = Profile.from_manifest(self.manifest.raw)
            self.link = self.profile.link_for(self.channel.ident.pid)
        except ProfileError as e:
            raise DriverError(f"{self.manifest.id}: {e}") from e
        self.session = Session(self.channel.write, self.profile.write_gap_s)
        self.online = False
        self._software_mode = False
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

        self.features = [FEATURE_TYPES[name](self, self.profile.features[name]) for name in FEATURE_TYPES if name in self.profile.features]
        self._settings: dict[str, tuple[Feature, Setting]] = {}
        self._actions: dict[str, Any] = {}
        for f in self.features:
            for key, setting in f.settings_schema().items():
                if key in self._settings:
                    raise DriverError(f"setting {key!r} claimed by two features")
                self._settings[key] = (f, setting)
            self._actions.update(f.actions())

        self.state = {"link": str(self.link), "wireless": self.link is Link.WIRELESS, "online": False}
        for f in self.features:
            self.state.update(f.initial_state())
        for key, (_, setting) in self._settings.items():
            self.state[key] = self._stored(key, setting)

    def _stored(self, key: str, setting: Setting) -> Any:
        if key not in self.settings:
            return setting.default
        try:
            return setting.parse(self.settings[key])
        except SettingError:
            log.warning("ignoring invalid stored %s=%r", key, self.settings[key])
            return setting.default

    # -- context API for features -----------------------------------------------

    async def update(self, changes: dict[str, Any]) -> None:
        changed = {k for k, v in changes.items() if self.state.get(k, object()) != v}
        if not changed:
            return
        self.publish({k: changes[k] for k in changed})
        for f in self.features:
            await f.on_changed(changed)

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._reap)
        return task

    def _reap(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("%s: background task failed: %s", self.channel.ident.uid, exc, exc_info=exc)

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        if self.link in (Link.WIRED, Link.STANDBY):
            return  # a plain USB audio device on a cable, or nothing linked
        await self._connect()
        self.spawn(self._supervise())
        for f in self.features:
            if type(f).run is not Feature.run:
                self.spawn(f.run())

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
        log.info("%s: headset online", self.channel.ident.uid)
        self.online = True
        await self.update({"link": "online", "online": True})
        for f in self.features:
            try:
                await f.on_online()
            except (OSError, RuntimeError) as e:
                log.warning("%s: %s failed to come online: %s", self.channel.ident.uid, type(f).__name__, e)

    async def _go_offline(self) -> None:
        if self.online:
            log.info("%s: headset stopped answering", self.channel.ident.uid)
        self.online = False
        await self.update({"link": "offline", "online": False})
        for f in self.features:
            await f.on_offline()

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
        if not self._software_mode:
            return
        with contextlib.suppress(Exception):
            for f in self.features:
                await f.stop()
        with contextlib.suppress(OSError):
            # Hand the mic button (and dongle LED) back to the firmware.
            # Ignored errors: on unplug the device node is already gone.
            await self.session.send(protocol.set_mode(Endpoint.RECEIVER, Mode.HARDWARE))
            await asyncio.sleep(0.05)
            await self.session.send(protocol.set_mode(Endpoint.HEADSET, Mode.HARDWARE))
        self._software_mode = False

    # -- device input ------------------------------------------------------------

    def on_data(self, data: bytes) -> None:
        if self.link is not Link.WIRELESS:
            return
        msg = protocol.classify(data)
        if not self.online and (isinstance(msg, (Button, Notice)) or not self.session.busy):
            # Unsolicited traffic while the headset is off is our cue to probe.
            log.debug("%s: unsolicited while offline: %s", self.channel.ident.uid, data[:8].hex(" "))
            if isinstance(msg, (Button, Notice)):
                self._wake.set()
        if isinstance(msg, Button):
            for f in self.features:
                f.on_button(msg)
        elif isinstance(msg, Notice):
            for f in self.features:
                f.on_notice(msg)
        else:
            self.session.feed(msg)

    # -- actions ---------------------------------------------------------------

    async def invoke(self, action: str, params: dict[str, Any]) -> Any:
        if action == "reconnect":
            await self._connect()
            return self.state["link"]
        if action.startswith("set_") and (key := action[4:]) in self._settings:
            feature, setting = self._settings[key]
            try:
                value = setting.parse(params.get("value"))
            except SettingError as e:
                raise DriverError(f"{key}: {e}") from None
            self.settings[key] = value
            self.save_settings()
            await self.update({key: value})
            try:
                await feature.apply(key)  # a no-op while offline; re-applied on reconnect
            except RuntimeError as e:
                raise DriverError(str(e)) from None
            return value
        if action in self._actions:
            try:
                return await self._actions[action](params)
            except RuntimeError as e:
                raise DriverError(str(e)) from None
        raise DriverError(f"unknown action {action!r}")
