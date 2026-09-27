"""The physical mic-mute button.

In software mode (which lighting and battery need) the firmware stops acting
on the button and forwards presses instead, so the host must do the muting.
The button sends key down and key up with no state, so each down edge
toggles. Muting goes through the service's audio service, which mutes at the
PipeWire layer and plays the feedback tone the firmware no longer does."""

from __future__ import annotations

import logging

from borochid_corsair_v2w.features import Feature
from borochid_corsair_v2w.protocol import Button

log = logging.getLogger(__name__)


class MicButton(Feature):
    def __init__(self, driver, config):
        super().__init__(driver, config)
        self.audio = driver.host.audio if driver.host else None
        if self.audio is None:
            log.warning(
                "%s: mic_button needs the audio service (manifest 'audio' section); the button will do nothing",
                driver.manifest.id,
            )

    def on_button(self, event: Button) -> None:
        if event.down and self.audio is not None:
            self.driver.spawn(self.audio.toggle_mic_mute())
