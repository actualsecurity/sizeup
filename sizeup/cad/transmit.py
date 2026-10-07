"""Transmitters: how a rendered dispatch reaches the air.

M1 has no transmit path. NullTransmitter is the only one sizeup will build: it logs what would
have gone out and reports transmitted=False, and the console plays the audio locally. The radio
interface comes in M2, after the hardware is installed and the department's FCC authorization
covers it (docs/cad-plan.md, "Transmitting: hardware and rules").
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_KEY_S = 60.0   # M2: hard limit on one key-up, whatever the audio length


@dataclass
class TxResult:
    transmitted: bool
    detail: str


class Transmitter:
    name = "transmitter"
    enabled = False

    def transmit(self, wav: Path | None, channel: str, text: str) -> TxResult:
        raise NotImplementedError


class NullTransmitter(Transmitter):
    """Transmit off: nothing is keyed. Logs what would have gone out."""
    name = "null"
    enabled = False

    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self.sent: list[tuple[str, str, float]] = []   # (channel, text, seconds): what would have gone out

    def transmit(self, wav: Path | None, channel: str, text: str) -> TxResult:
        secs = 0.0
        if wav is not None:
            try:
                with wave.open(str(wav)) as w:
                    secs = w.getnframes() / w.getframerate()
            except (OSError, EOFError, wave.Error):
                pass
        self.sent.append((channel, text, secs))
        self.log(f"TRANSMIT OFF: would have keyed {channel or 'no channel'} for {secs:.1f} s: {text}")
        return TxResult(False, "transmit is off: logged and played locally only")


class AudioPttTransmitter(Transmitter):
    """M2, not built yet. Plays the dispatch WAV into a radio's mic input through a USB sound-card
    interface and holds PTT for exactly the audio's length:

      - audio out: a named output device (CM108-class interface, or a commercial dispatch
        interface) wired to the radio's accessory port, levels set once on the bench;
      - PTT: the interface's GPIO (CM108 HID) or a serial port's RTS/DTR line;
      - key-up only on an operator's button press, with a preview first; a hard maximum key time
        (MAX_KEY_S) that drops PTT whatever the audio is doing; PTT released on any error, on
        shutdown and when the process dies (the line is asserted by us, never latched);
      - channel select (FG1 / the Port Chester repeater) is the radio's, set on the radio;
      - every key-up logged with start, length and channel.

    Refuses to construct until then: nothing in M1 opens an audio device, a serial port or GPIO.
    """
    name = "audio-ptt"

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("AudioPttTransmitter is M2: no radio interface in this version")


def make_transmitter(transmit: bool, log: Callable[[str], None] = print) -> Transmitter:
    """The one place a transmitter is chosen. M1: always NullTransmitter (config.load refuses
    transmit = true, so this is belt and braces)."""
    if transmit:
        raise ValueError("transmit is not available in this version (M1): no radio interface")
    return NullTransmitter(log)
