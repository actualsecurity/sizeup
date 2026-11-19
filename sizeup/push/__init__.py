"""Pushes to the mobile app (#563, docs/mobile-api.md section 4): Amazon SNS mobile push through the stdlib SigV4
(`sizeup/aws.py`), so the outbound guard (`sizeup/outbound.py`) applies.

    sns.py       the SNS client (CreatePlatformEndpoint / SetEndpointAttributes / DeleteEndpoint / Publish) and the
                 three platform applications (APNs sandbox, APNs production, FCM) read from optional environment
                 variables: none set = push refuses (fail closed)
    devices.py   the registered devices: its own lazily created table in hub.sqlite
    content.py   one message -> the payload (`{kind, id, title, body}` and nothing more), scrubbed like ntfy's
    service.py   PushService: registration, and the fan-out `notify.Pushes` calls beside the ntfy path
    wire.py      the hub's one-line hook (sizeup/app.py make_server)

Device pushes never touch ntfy topics or the protected-topic machinery (sizeup/notify.py)."""
