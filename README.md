# sizeup

Fire dispatch situational display for Port Chester FD firehouses: the sibling of
[shortfinal](../shortfinal) (ADS-B + KHPN ATC), applied to our own calls.

- **Paging (South 2):** decode the two-tone alert (PCFD, Rye Brook first), transcribe the voice page.
- **60 Control Fire/EMS 15:** transcribe dispatcher and responding-unit traffic.
- **Fireground 1:** transcribe PCFD / Rye Brook fireground traffic while the call is ongoing.
- **Map:** zoom to the incident location and show the incident type.

See `docs/PLAN.md`.

```sh
/opt/homebrew/bin/python3.14 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```
