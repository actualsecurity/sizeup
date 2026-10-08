# Running this Mac as the station

The station is today's sizeup (SDRs, whisper, Kokoro, its own web UI) plus a link that forwards
everything it hears to the hub in the cloud. It only makes outbound HTTPS requests: no port
forwarding, no inbound firewall rule. Architecture and failure behaviour: [docs/cloud.md](../../docs/cloud.md).

## 1. The secrets, in the Keychain

One random token, known to the hub and the station:

```sh
openssl rand -hex 32
```

On the hub it is `SIZEUP_STATION_TOKEN` (an SSM parameter injected into the task). On the station,
put it in the login Keychain of the user that runs sizeup (`-w` with no value asks for it, so it
stays out of the shell history):

```sh
security add-generic-password -a sizeup -s sizeup-station-token -w
```

If Cloudflare Access protects `/api/station/*` with a **service token** (recommended, see
docs/cloud.md), add its id and secret the same way:

```sh
security add-generic-password -a sizeup -s sizeup-access-id -w
security add-generic-password -a sizeup -s sizeup-access-secret -w
```

With an Access *bypass* policy on that path instead, leave them out: the bearer token is then the
only gate. The service token is the station, never a person: on the hub it is never taken for an
operator (who is at the console comes from the email they signed in with, docs/cloud.md "Who is
signed in").

Each secret is looked up in this order: the environment (`SIZEUP_STATION_TOKEN`,
`SIZEUP_ACCESS_CLIENT_ID`, `SIZEUP_ACCESS_CLIENT_SECRET`), then `[cloud]` in `config.toml`
(`token`, `access_client_id`, `access_client_secret`), then the Keychain. Other service names:
`[cloud] keychain_prefix = "pcfd"` (pcfd-station-token, ...) or `keychain_token`,
`keychain_access_id`, `keychain_access_secret` one by one. The values are never logged or shown:
the page says only "set (Keychain)" / "set (env)" / "missing".

## 2. Turn it on: Settings → System → Station

On the station's own console (`/cad` → Settings → System), the **Station** card:

* **Forward to hub** on / off. On: what this station hears also goes to the hub, which is then the
  CAD of record (the console shows a banner saying so). Off: all-in-one again. Radios,
  speech-to-text and the voice keep running either way; nothing is restarted.
* **Hub URL**: `https://...` (the hub's public hostname, Cloudflare Tunnel).
* The status line: Connected / Offline since 12:03 / N waiting / not sending (and why, e.g. no
  token), with the last error as its tooltip.
* Where each secret came from, or that it is missing (and the `security` command to add it).
* **Test connection**: the hub's `/healthz`, then its station path, with the token and the Access
  headers. "OK: hub reached, token accepted", or the HTTP code and what it means.

Each change is in the Settings audit log (System) with the Operator's name. The choice is kept in
`state/cloud.json` and survives restarts; from the first change there it wins over `config.toml`.
A missing token after a restart: add it to the Keychain, then press Test connection (the link
starts as soon as it has one; meanwhile events queue).

## 3. `config.toml` (optional)

Nothing in `config.toml` is needed. A `[cloud]` section only sets the default until the switch is
first used, and the outbox:

```toml
[cloud]
role = "station"                           # on by default (leave out: off by default)
hub_url = "https://sizeup.example.org"     # the URL the card starts with
# outbox = "state/outbox.sqlite"           # default; survives restarts
# outbox_max_mb = 300                      # queued audio cap: oldest clips dropped first, never transcripts
```

## 4. Check

The log says `hub link: connected to https://...`. In the station's monitor the header gets a
**Hub** row (connected / offline since 12:03, and how many events are waiting); the station's
`/cad` shows the "Hub connected" pill and the banner. On the hub, the **Station** row says
"connected, last seen 2 s ago", followed by this station's FG1 / South 2 / Fire/EMS 15 / STT /
Voice rows.

If the hub or the internet is down, nothing is lost: pages, transmissions, transcripts and radio
IDs wait in `state/outbox.sqlite` and go up in order when the hub is reachable again (also after a
restart of either side). Turning forwarding off keeps what is already queued (it goes up first when
it's turned back on); what is heard while it's off stays on this machine. Keep the Mac's clock on
network time: the hub warns when the two clocks differ by more than 5 s. Deleting
`state/outbox.sqlite` throws away whatever hadn't gone up yet.
