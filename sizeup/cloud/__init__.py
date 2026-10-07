"""sizeup in two places ([cloud] in the config, docs/cloud.md).

    station (the Mac with the SDRs, whisper and Kokoro)          hub (the cloud: web UI + CAD, no radios)
      App, as all-in-one, plus StationLink (station.py)  ──HTTPS──>  HubApp + Hub (hub.py)
        finished pages / transmissions / transcripts /    events    the same App code applies them:
        radio IDs  -> outbox.sqlite (outbox.py) -> in order,         monitor state, CAD comms, 60 Control
        batched, retried                                             pages -> pending CAD events
        talking now + radio/SDR/STT/voice status (latest wins) -> state
        <- long-poll GET /api/station/jobs: dispatch voice renders, SDR / talkgroup switches, re-runs

The station only ever connects out; every request carries the station's bearer token.
"""
