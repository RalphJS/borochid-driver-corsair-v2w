"""Battery level and charge state.

A late heartbeat reply has the same shape as a battery reply and carries
the headset's PID (2621, "262%"), so a refresh takes several samples and
keeps the last valid one. Charge state has
its own query, which also sees wall chargers and power banks (a USB bus scan
only sees a cable to this computer).

The headset pushes a notice whenever level or charge state changes, so the
feature is event-driven: it reads once when the headset comes online and
then follows notices. ``poll_s`` is a slow safety net. Below ``low_percent``
and not charging it stops entirely: waking a nearly flat headset makes it
beep.
"""

from __future__ import annotations

import asyncio
import logging

from borochid_corsair_v2w import protocol
from borochid_corsair_v2w.features import Feature
from borochid_corsair_v2w.protocol import Command, Notice, Op

log = logging.getLogger(__name__)


class Battery(Feature):
    @property
    def poll_s(self) -> float:
        return float(self.config.get("poll_s", 1800))

    @property
    def low_percent(self) -> int:
        return int(self.config.get("low_percent", 15))

    @property
    def samples(self) -> int:
        return int(self.config.get("samples", 3))

    def initial_state(self):
        return {"battery": None, "charging": None}

    async def refresh(self) -> int | None:
        if not self.driver.online:
            raise RuntimeError("headset is offline")
        session, timeout = self.driver.session, self.driver.profile.reply_timeout_s
        level = None
        for _ in range(self.samples):
            reading = await session.request(
                protocol.request(Op.BATTERY, self.driver.target),
                lambda r: protocol.battery_percent(r.word()) if r.answers(Command.SET, self.driver.target) else None,
                timeout,
            )
            if reading is not None:
                level = reading
        charging = await session.request(
            protocol.request(Op.CHARGE, self.driver.target),
            lambda r: protocol.charge_state(r.word()) if r.answers(Command.SET, self.driver.target) else None,
            timeout * 2,
        )
        changes = {}
        if level is not None:
            changes["battery"] = level
        if charging is not None:
            changes["charging"] = charging
        await self.driver.update(changes)
        return level

    def _polling_worthwhile(self) -> bool:
        level, charging = self.setting("battery"), self.setting("charging")
        return level is None or level >= self.low_percent or bool(charging)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_s)
            if self.driver.online and self._polling_worthwhile():
                try:
                    await self.refresh()
                except (OSError, RuntimeError) as e:
                    log.debug("battery poll skipped: %s", e)

    async def on_online(self) -> None:
        await self.refresh()

    def on_notice(self, notice: Notice) -> None:
        if notice.op == Op.BATTERY and (level := protocol.battery_percent(notice.value)) is not None:
            self.driver.spawn(self.driver.update({"battery": level}))
        elif notice.op == Op.CHARGE and (charging := protocol.charge_state(notice.value)) is not None:
            self.driver.spawn(self.driver.update({"charging": charging}))
