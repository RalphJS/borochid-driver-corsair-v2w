"""Hardware sidetone (the headset mixes the mic into its own output).

Untouched until the user sets a level, and optionally switched off when the
driver stops. These frames come from the Void family (they include an ANC
toggle), and a Virtuoso SE was captured rejecting them with status 05, so
every frame's reply status is checked. A rejection marks the feature
unsupported (``sidetone_supported``) instead of failing silently.
"""

from __future__ import annotations

import logging

from borochid_corsair_v2w import protocol
from borochid_corsair_v2w.features import Feature, Setting, boolean, percent
from borochid_corsair_v2w.profile import Link
from borochid_corsair_v2w.protocol import Command

log = logging.getLogger(__name__)


class Unsupported(RuntimeError):
    pass


class Sidetone(Feature):
    def settings_schema(self):
        return {"sidetone": Setting(percent, None), "sidetone_off_on_exit": Setting(boolean, True)}

    def initial_state(self):
        # Untried on the cable (host audio has a sidetone there); through the
        # dongle assumed until the headset says otherwise.
        return {"sidetone_supported": self.driver.link is Link.WIRELESS}

    async def _push(self, level: int) -> None:
        if not self.driver.online:
            return
        if self.setting("sidetone_supported") is False:
            raise Unsupported("this headset does not support hardware sidetone; use the sound card sidetone")
        session, timeout = self.driver.session, self.driver.profile.reply_timeout_s
        for frame in protocol.sidetone(level, self.driver.target):
            status = await session.status(frame, Command.GET, timeout, self.driver.target)
            if status not in (0, None):
                log.warning("%s: headset rejected sidetone (status %02x)", self.driver.channel.ident.uid, status)
                await self.driver.update({"sidetone_supported": False})
                raise Unsupported(f"this headset rejected hardware sidetone (status {status:02x}); use the sound card sidetone")

    async def on_online(self) -> None:
        if (level := self.setting("sidetone")) is not None and self.setting("sidetone_supported") is not False:
            try:
                await self._push(level)
            except Unsupported:
                pass  # already published and logged

    async def apply(self, key: str) -> None:
        if key == "sidetone":
            await self._push(self.setting("sidetone"))

    async def stop(self) -> None:
        if self.setting("sidetone_off_on_exit") and self.setting("sidetone") and self.setting("sidetone_supported"):
            await self._push(0)
