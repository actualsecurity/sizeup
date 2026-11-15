# Dongle claims shared with shortfinal

sizeup and the sibling app shortfinal can run on the same machine and see the same RTL-SDR dongles. Each records
which dongle it has open in a shared directory, and neither opens a dongle the other holds. This file describes
the protocol as sizeup implements it (`sizeup/radio/claims.py`). The format was agreed with shortfinal on
2026-10-05.

This file began as a handoff note to the shortfinal session (2026-10-05 to 2026-10-08). Its "open suggestion", a
shared claim file, is what is described here. The dongle handling it listed is now in
[deploy/station/README.md](../deploy/station/README.md#dongles-get-a-job-by-themselves).

## The claim file

| | |
|---|---|
| Directory | `~/.local/state/rtl-claims/` of the user the app runs as. On the Linux station every service that writes a claim (sizeup-station, trunk-recorder, ADS-B, AIS) sets `HOME=/var/lib/sizeup`, so it is `/var/lib/sizeup/.local/state/rtl-claims/`. |
| File | `<serial>.json`: one per dongle, named by its exact USB serial. |
| Content | `{"app": "sizeup", "pid": 1234, "mode": "FG1", "since": 1760000000.0}` |
| `app` | Who holds it: `sizeup` or the other app's name. |
| `pid` | The process that has the dongle open. It decides whether the claim is live. |
| `mode` | What the dongle is doing, as text for the other app's UI. |
| `since` | When it was claimed, in epoch seconds. |

## The rules

1. **Claim before opening.** An app writes the file before it opens the dongle and deletes it when it lets go.
2. **Written atomically.** A temporary file in the same directory (`.<serial>.<pid>.tmp`), then a rename.
3. **A live claim by the other app is never taken.** The dongle is left alone, by hand and automatically.
4. **A claim is live while its pid exists.** `kill(pid, 0)`: "no such process" means stale; "not permitted" means
   someone else's process, which counts as alive.
5. **A stale claim is ignored, and either app may delete it.** sizeup deletes one when it reads it.
6. **An app deletes only its own claim.**
7. **A file that can't be read or parsed is no claim.** A claim that can't be written doesn't stop sizeup from
   running: the dongle itself is the real lock.

To hand a dongle from one app to the other, switch it **Off** in the app that holds it. The other app can then
pick it.

## Who writes claims in sizeup

All of them write `"app": "sizeup"`. The service processes run with the system python3 and carry their own
stdlib copy of the same format.

| Process | `mode` | Code |
|---|---|---|
| sizeup itself (`rtl_fm` channels, and trunk-recorder where it is sizeup's child) | the job's name: `FG1`, `South 2`, `HPN Tower`, `Fire/EMS 15` ... | `sizeup/radio/claims.py`, `sizeup/app.py` |
| the trunk-recorder service on the station, for the county system | `Fire/EMS 15` | `sizeup/radio/trunkrecd.py` |
| the trunk-recorder service, for another P25 system | the source's job: `MRRS 770`, `MRRS 773` | `sizeup/radio/trunkrecd.py` |
| a reception test, on the dongle it was lent | `<system> test`, then the job the dongle gets back | `sizeup/radio/reception.py` |
| the ADS-B decoder (`sizeup-adsb`) | `adsb` | `sizeup/radio/adsbd.py` |
| the AIS decoder (`sizeup-ais`) | `ais` | `sizeup/radio/aisd.py` |

**The trunk-recorder service's claims count as sizeup's own.** On the station trunk-recorder runs under its own
service and holds the claim under that service's pid, so the claim stays right while `sizeup-station` restarts.
sizeup knows the service's pid (`claims.helpers`) and treats its claims as follows:

- never shown as "in use by another app";
- never overwritten: `claim()` succeeds for the same mode (the service runs it for sizeup) and fails for any
  other mode (the service has not let go of the dongle yet);
- removed only by the service.

Any other sizeup process's claim (the ADS-B or AIS decoder's) is treated like another app's: the dongle is held.

## What a held dongle looks like

- Settings > Radios and the station page: the dongle's row says it is held, and by which app and mode.
- Auto-assign skips it. Picking a job for it by hand is refused.
- The reception test greys it out ([reception-test.md](reception-test.md)).
- `sizeup-station status`: the `p25:` and `adsb:` lines say `held`; the AIS decoder's status says `held`.
- `sizeup-station sdr-serial` asks you to switch the dongle Off in the other app and waits. It never stops or
  kills the other app.

## Check it by hand

```sh
ls ~/.local/state/rtl-claims/                       # on the station: /var/lib/sizeup/.local/state/rtl-claims/
cat ~/.local/state/rtl-claims/<serial>.json
ps -p <pid>                                         # no such process: the claim is stale
```

## Dongle names on the bench Mac (2026-10-05)

`[radio] sdr_names` in `deploy/station/config.station.toml` still uses these serials.

| Serial | Dongle |
|---|---|
| `v4` | RTL-SDR Blog V4. shortfinal renamed it from `00000001`; its factory EEPROM backup is in shortfinal's `deploy/eeprom-backups/`. |
| `adsb` | RTL-SDR Blog V3. Not an ADS-B-specific dongle. |
| `00000001` | A second RTL-SDR Blog V3 on its factory serial, shown as "V3 #2". |

Every dongle should have a unique serial that is not all digits:
[Two dongles, one serial](../deploy/station/README.md#two-dongles-one-serial).
