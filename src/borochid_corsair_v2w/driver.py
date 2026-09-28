"""Borochid driver for Corsair V2W headsets: the headset itself.

The headset is reached two ways, each its own connection to the service:

    wired ─ USB cable. The headset speaks V2W on the dongle's endpoint
            (0x08, replies from source 00), so it works as through the
            dongle: this driver puts it in software mode, keeps it alive
            and hands it back to hardware mode on stop.
    online ─ through its dongle: the dongle's driver (receiver.py) keeps the
             radio link and announces the headset while it answers, so this
             driver only exists while the headset is there. Both share one
             request/reply session: the device answers whatever was asked
             last, whoever asked.

The headset's own ID (a data resource, the same over the cable and through
the dongle) is read on either connection, so the service shows one headset
and its settings follow it.

In software mode the mic button is forwarded and the LEDs are the host's.
Through the dongle, the dongle's driver sets it and hands the headset back
when it goes; features only undo their own settings on stop (sidetone off
on exit).
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
from borochid_corsair_v2w.protocol import Button, Command, Endpoint, Mode, Notice, Op, Resource
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
        shared = getattr(self.channel, "shared", None)
        if self.link is Link.WIRELESS and not isinstance(shared, Session):
            raise DriverError(f"{self.manifest.id}: a wireless headset is reached through its dongle's driver")
        if self.link is Link.STANDBY:
            raise DriverError(f"{self.manifest.id}: standby is the dongle's, not the headset's")
        self.session = shared if isinstance(shared, Session) else Session(self.channel.write, self.profile.write_gap_s)
        # Where the headset listens, and who answers for it: on the cable, the
        # dongle's endpoint (replies come from source 00, which is the same
        # Endpoint value).
        self.target = Endpoint.HEADSET if self.link is Link.WIRELESS else Endpoint.RECEIVER
        self.online = False
        self._software_mode = False
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
        # Before the features come online, so they push the headset's own settings.
        await self._identify()
        if self.link is Link.WIRED:
            await self._take_over()
            self.spawn(self._keep_alive())
        self.online = True
        await self.update({"link": str(self.link) if self.link is Link.WIRED else "online", "online": True})
        for f in self.features:
            try:
                await f.on_online()
            except (OSError, RuntimeError) as e:
                log.warning("%s: %s failed to come online: %s", self.channel.ident.uid, type(f).__name__, e)
        for f in self.features:
            if type(f).run is not Feature.run:
                self.spawn(f.run())

    async def _take_over(self) -> None:
        """On the cable: what the dongle's driver does through the dongle."""
        async with self.session.exclusive():
            await self.session.send(protocol.set_mode(self.target, Mode.SOFTWARE))
            self._software_mode = True
            await asyncio.sleep(0.02)
            self.session.drain()
            await self.session.send(protocol.request(Op.HEARTBEAT, self.target))

    async def _keep_alive(self) -> None:
        """Software mode needs the heartbeat, on the cable too."""
        while True:
            await asyncio.sleep(self.profile.keepalive_s)
            try:
                await self.session.send(protocol.request(Op.HEARTBEAT, self.target))
            except OSError as e:
                log.info("%s: keep-alive stopping: %s", self.channel.ident.uid, e)
                return

    async def _identify(self) -> None:
        try:
            async with self.session.exclusive():
                *frames, read, close = protocol.read_resource(self.target, Resource.HEADSET_ID)
                await self.session.send(*frames)
                device_id = await self.session.request(
                    read, lambda r: protocol.headset_id(r.data) if r.answers(Command.READ, self.target) else None,
                    # The first thing asked once paired: radio replies are slow then (~600 ms).
                    self.profile.first_contact_s,
                )
                await self.session.send(close)
        except OSError as e:
            log.warning("%s: reading the headset ID failed: %s", self.channel.ident.uid, e)
            return
        if device_id is None:
            log.info("%s: the headset did not report its ID", self.channel.ident.uid)
            return
        log.info("%s: headset %s", self.channel.ident.uid, device_id)
        await self.identify(device_id)

    async def settings_reloaded(self) -> None:
        """Settings the headset's other connection kept: shown, and pushed if online."""
        changes = {key: self._stored(key, setting) for key, (_, setting) in self._settings.items()}
        changed = [k for k, v in changes.items() if self.state.get(k) != v]
        await self.update(changes)
        if self.online:
            for key in changed:
                try:
                    await self._settings[key][0].apply(key)
                except RuntimeError as e:
                    log.warning("%s: applying %s failed: %s", self.channel.ident.uid, key, e)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if not self.online:
            return
        # Still in software mode. Through the dongle, its driver hands the
        # headset back after this (the service stops a paired device before
        # its receiver); on the cable, this driver does.
        with contextlib.suppress(Exception):
            for f in self.features:
                await f.stop()
        self.online = False
        if self._software_mode:
            with contextlib.suppress(OSError):  # on unplug the node is already gone
                await self.session.send(protocol.set_mode(self.target, Mode.HARDWARE))
            self._software_mode = False

    # -- device input ------------------------------------------------------------

    def on_data(self, data: bytes) -> None:
        # Through the dongle, its driver feeds replies to the shared session
        # and hands over only the rest.
        msg = protocol.classify(data)
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
                await feature.apply(key)
            except RuntimeError as e:
                raise DriverError(str(e)) from None
            return value
        if action in self._actions:
            try:
                return await self._actions[action](params)
            except RuntimeError as e:
                raise DriverError(str(e)) from None
        raise DriverError(f"unknown action {action!r}")
