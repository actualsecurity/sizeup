# trunk-recorder for Fire/EMS 15

Westchester County P25 Phase II (system A66 / WACN BEE00), South simulcast site. Records only
talkgroups 307 (Fire/EMS 15) and 301 (60 Control) as one WAV + JSON per call into
`captures/p25/`, which sizeup ingests (`[p25] capture_dir`).

- `center` 477.19375 MHz at 2.4 MS/s covers the control-channel candidates 476.075 / 476.1125 /
  476.2125 / 476.2375 and the voice channels 478.1875 / 478.3125 (span 2.24 MHz, at the edge of one
  RTL-SDR). Bench check 2026-10-01 on the Mac's V4: 476.2375 +28 dB, 476.1125 +24 dB, 476.075
  +21 dB continuous carriers.
- `modulation: qpsk` = CQPSK/LSM for the simulcast site.
- `device`: the dongle serial (bench: `rtl=00000001`; deployment: `rtl=fire-p25`).

Run from this directory (paths are relative to it):

```sh
~/.local/src/trunk-recorder/build/trunk-recorder --config=config.json
```

On a one-dongle bench, stop sizeup's paging/fireground receivers first (they use the same dongle).

## Who is talking, live (sizeup's "talking" highlight)

sizeup reads trunk-recorder's output as it comes: "Unit ID set via Control Channel" names each new
talker mid-call, and the "Transmission src" lines name everyone when a call ends. trunk-recorder
logs no unit for a call's *first* transmission, so a one-transmission call shows its talker only when
it ends. To name the first talker at the grant, enable the unit script (forks a tiny shell per unit
event on the system):

```json
"systems": [{ ..., "unitScript": "./unit-event.sh" }]
```
