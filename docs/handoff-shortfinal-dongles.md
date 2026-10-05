# Handoff to shortfinal: dongle handling like sizeup's (2026-10-05)

The user asked for shortfinal to get the same dongle recognition sizeup's web header has.
The shortfinal session wasn't running when this was written, so the request is left here.

## What sizeup does (commits 7f128b4..6378ca4 in this repo; reuse freely)

1. **Rows follow what's plugged in.** With `[radio] sdrs = "auto"`, every RTL-SDR plugged in gets
   its own row of switches in the header (sizeup: South 2 / FG1 / Fire/EMS 15 / Off; for
   shortfinal it would be its modes, e.g. ADS-B / airband TWR/DEP/APP / Off). Rows appear and
   disappear within a few seconds as dongles are plugged in or out. The state carries a list of
   `{serial, name, product, mode, present}`; the page rebuilds the rows when that list changes.
2. **A newly seen dongle starts Off.** Nothing opens a dongle until the user picks a mode for it,
   so neither app grabs one the other is using.
3. **Unplugged while assigned:** the row stays, greyed out and labelled "· unplugged" with its
   mode, and the receiver retries every 15 s and resumes when the dongle returns. An idle dongle
   that's unplugged just disappears.
4. **One mode per dongle; a mode on at most one dongle.** Picking a mode that's running on another
   dongle moves it there (the other goes Off), and that button shows a dashed outline with a
   "move it here" tooltip. Assignments persist per serial (`captures/sdr_modes.json`).
5. **Found by exact serial, passed as an index:** `sizeup/radio/sdr.py` uses ctypes librtlsdr
   (`rtlsdr_get_device_count` / `rtlsdr_get_device_usb_strings`), with a 3 s cache for the UI and
   a fresh lookup before every start. All-digit serials are otherwise read as indexes by rtl_fm,
   readsb and gr-osmosdr. trunk-recorder gets a generated `config.runtime.json` with `rtl=<index>`.
6. **Names and ignores:** `sdr_names = { v4 = "V4", adsb = "V3", "00000001" = "V3 #2" }` sets the
   labels (the hover tooltip shows the serial and USB product); `sdr_ignore = [serials]` hides a
   dongle for good.
7. **Switch requests time out after 15 s**, and any refusal is shown next to the switches, so the
   buttons can't get stuck disabled.

Code: `sizeup/radio/sdr.py`; `sizeup/app.py` (`sdrs` property, `_load_modes`, `_dongle`,
`_start_mode` / `_stop_mode` / `set_mode`, `sdr()`); `sizeup/web/static/app.js` (`renderSdr`,
`setSdrMode`); test `tests/test_app.py::test_sdr_rows_follow_the_dongles_plugged_in`.

## Hardware (confirmed by the user, 2026-10-05)

- `v4`: RTL-SDR Blog V4 (shortfinal renamed it from 00000001; EEPROM backup in
  shortfinal/deploy/eeprom-backups/v4-factory.bin).
- `adsb`: RTL-SDR Blog V3. Not an ADS-B-specific dongle; the user removed the LNA from it.
- `00000001`: a second RTL-SDR Blog V3 with its factory serial (all digits). sizeup shows it as "V3 #2".

## Open suggestion: a shared claim file

Both apps now auto-detect the same dongles. Proposal: `~/.local/state/rtl-claims/<serial>.json` =
`{"app", "pid", "mode", "since"}`, written when an app opens a dongle and removed on release; claims
whose pid is gone are ignored. Each UI can then show "in use by sizeup / shortfinal" on that row
instead of failing as busy. Agree a path and format with the sizeup side before implementing.
