"""Features are independent units the driver composes from the profile.

A feature declares the settings it owns (persisted per device), reacts to
the link going online/offline, to unsolicited device reports, and to state
changes made by other features (lighting recolours the battery zone when the
battery feature reports a new level). Features never talk to each other
directly, only through driver state.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from borochid_corsair_v2w.protocol import Button, Notice

if TYPE_CHECKING:
    from borochid_corsair_v2w.driver import HeadsetDriver

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class SettingError(ValueError):
    pass


def color(value: Any) -> str:
    if not isinstance(value, str) or not _COLOR_RE.match(value):
        raise SettingError("expected a #rrggbb colour")
    return value.lower()


def percent(value: Any) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise SettingError("expected an integer 0-100") from None
    if not 0 <= v <= 100:
        raise SettingError("expected an integer 0-100")
    return v


def boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise SettingError("expected true or false")
    return value


@dataclass(frozen=True)
class Setting:
    parse: Callable[[Any], Any]
    # None means "leave the device alone until the user picks a value".
    default: Any = None


class Feature:
    def __init__(self, driver: HeadsetDriver, config: dict[str, Any]):
        self.driver = driver
        self.config = config

    def settings_schema(self) -> dict[str, Setting]:
        return {}

    def initial_state(self) -> dict[str, Any]:
        return {}

    def actions(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]:
        return {}

    def setting(self, key: str) -> Any:
        return self.driver.state.get(key)

    async def on_online(self) -> None:
        """The headset answered after a handshake: push everything it needs."""

    def on_button(self, event: Button) -> None: ...

    def on_notice(self, notice: Notice) -> None: ...

    async def on_changed(self, keys: set[str]) -> None:
        """State keys changed, by the user or another feature."""

    async def apply(self, key: str) -> None:
        """One of this feature's settings changed."""

    async def run(self) -> None:
        """Optional background loop, started once and cancelled on stop."""

    async def stop(self) -> None:
        """Called while the channel is still open, before the device is released."""
