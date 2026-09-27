"""RGB zones. The device takes every zone in one frame, so any change
(a colour, a brightness, the battery level, the mute state) rewrites all of
them from current state."""

from __future__ import annotations

from typing import Any

from borochid_corsair_v2w import protocol
from borochid_corsair_v2w.features import Feature, Setting, color, percent

RGB = tuple[int, int, int]
OFF: RGB = (0, 0, 0)


def parse_rgb(value: str) -> RGB:
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def scaled(rgb: RGB, brightness: int) -> RGB:
    return tuple(c * brightness // 100 for c in rgb)  # type: ignore[return-value]


def battery_colour(level: int | None) -> RGB:
    """What the battery LED shows: green, amber, red; dark when unknown."""
    if level is None:
        return OFF
    if level >= 80:
        return (0, 255, 0)
    if level >= 20:
        return (255, 255, 0)
    return (255, 0, 0)


class Lighting(Feature):
    def __init__(self, driver, config):
        super().__init__(driver, config)
        self.zones = driver.profile.zones
        self._last: list[RGB] | None = None
        # LED init is answered with status 03 when repeated: once per connection.
        self._leds_ready = False
        audio = driver.host.audio if driver.host else None
        if audio is not None and any(z.kind == "mic" for z in self.zones):
            audio.on_mic_muted(lambda _muted: self.refresh())

    def settings_schema(self) -> dict[str, Setting]:
        schema: dict[str, Setting] = {}
        for z in self.zones:
            if z.kind in ("color", "mic"):
                schema[f"{z.id}_color"] = Setting(color, z.default)
                schema[f"{z.id}_brightness"] = Setting(percent, 100)
            if z.kind == "mic":
                schema[f"{z.id}_muted_color"] = Setting(color, z.muted_default)
        return schema

    def _mic_muted(self) -> bool:
        audio = self.driver.host.audio if self.driver.host else None
        return bool(audio and audio.mic_muted)

    def colours(self) -> list[RGB]:
        out = []
        for z in self.zones:
            if z.kind == "battery":
                out.append(battery_colour(self.setting("battery")))
            elif z.kind == "mic" and self._mic_muted():
                out.append(parse_rgb(self.setting(f"{z.id}_muted_color")))
            else:
                out.append(scaled(parse_rgb(self.setting(f"{z.id}_color")), self.setting(f"{z.id}_brightness")))
        return out

    async def refresh(self, force: bool = False) -> None:
        if not self.driver.online:
            return
        colours = self.colours()
        if colours == self._last and not force:
            return
        frames = [protocol.rgb(colours)] if self._leds_ready else [protocol.led_init(), protocol.rgb(colours)]
        await self.driver.session.send(*frames)
        self._leds_ready = True
        self._last = colours

    async def on_online(self) -> None:
        # The handshake leaves the LEDs dark; repaint straight away.
        self._leds_ready = False
        await self.refresh(force=True)

    async def on_offline(self) -> None:
        self._last = None
        self._leds_ready = False

    async def on_changed(self, keys: set[str]) -> None:
        if "battery" in keys:
            await self.refresh()

    async def apply(self, key: str) -> None:
        await self.refresh()

    def actions(self) -> dict[str, Any]:
        return {}
