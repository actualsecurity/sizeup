# Running this Mac as the station

The station is today's sizeup (SDRs, whisper, Kokoro, its own web UI) plus a link that forwards
everything it hears to the hub in the cloud. It only makes outbound HTTPS requests: no port
forwarding, no inbound firewall rule. Architecture and failure behaviour: [docs/cloud.md](../../docs/cloud.md).

## 1. The token

One random token, known to the hub and the station:

```sh
openssl rand -hex 32
```

On the hub it is `SIZEUP_STATION_TOKEN` (an SSM parameter injected into the task). On the station,
put it in the environment of the sizeup process, not in `config.toml`:

```sh
export SIZEUP_STATION_TOKEN=...            # e.g. in the launchd plist's EnvironmentVariables
```

## 2. `config.toml`

Add a `[cloud]` section to the station's existing config:

```toml
[cloud]
role = "station"
hub_url = "https://sizeup.example.org"     # the hub's public hostname (Cloudflare Tunnel)
# outbox = "state/outbox.sqlite"           # default; survives restarts
# outbox_max_mb = 300                      # queued audio cap: oldest clips dropped first, never transcripts
```

If Cloudflare Access protects `/api/station/*` with a **service token** (recommended, see
docs/cloud.md), give the station its id and secret, again through the environment:

```sh
export SIZEUP_ACCESS_CLIENT_ID=....access
export SIZEUP_ACCESS_CLIENT_SECRET=...
```

(or `access_client_id` / `access_client_secret` in `[cloud]`). With an Access *bypass* policy on
that path instead, leave them out: the bearer token is then the only gate.

## 3. Start it and check

```sh
python -m sizeup --config config.toml
```

The log says `hub link: connected to https://...`. In the station's monitor the header gets a
**Hub** row (connected / offline since 12:03, and how many events are waiting); the station's
`/cad` shows a banner: the CAD of record is the hub. On the hub, the **Station** row says
"connected, last seen 2 s ago", followed by this station's FG1 / South 2 / Fire/EMS 15 / STT /
Voice rows.

If the hub or the internet is down, nothing is lost: pages, transmissions, transcripts and radio
IDs wait in `state/outbox.sqlite` and go up in order when the hub is reachable again (also after a
restart of either side). Keep the Mac's clock on network time: the hub warns when the two clocks
differ by more than 5 s.

To stop forwarding, remove the `[cloud]` section (back to all-in-one). Deleting
`state/outbox.sqlite` throws away whatever hadn't gone up yet.
