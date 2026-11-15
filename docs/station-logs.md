# Station logs

The station's journal, in one place with the hub's own logs: CloudWatch Logs in the department's AWS account. Read it
on the hub in **Settings > Station > Logs** (admins). Code: `sizeup/cloud/logread.py`, `sizeup/cloud/logship.py`,
`sizeup/logscrub.py`, `sizeup/cloud/cwlogs.py`. Issue: #494.

On the station PC itself the journal is read as before: `sizeup-station logs -f`
([deploy/station/README.md](../deploy/station/README.md#the-sizeup-station-command)).

## How it gets there

```
station journald (sizeup-* units)
  -> sizeup-logread (user sizeup-logs, in systemd-journal, no network)     sizeup/cloud/logread.py
     -> /var/lib/sizeup-logs/*.jsonl (only the sizeup-* units' entries, every 10 s, 20 MB cap)
  -> sizeup-station: filter, leave the adapter's lines, scrub, cut repeats  sizeup/cloud/logship.py, sizeup/logscrub.py
  -> <state>/logspool/*.json.gz (a batch every ~45 s or at 5000 lines / 512 KB; 50 MB cap)
  -> POST /api/station/logs (the station token + Access service token)     the link the station already has
  -> hub: filter and scrub again, PutLogEvents                             sizeup/cloud/cwlogs.py
  -> CloudWatch Logs /sizeup/station (Infrequent Access, 30 days)
```

- **No AWS key on the station.** The hub writes with its task role.
- **No third-party log service.** Everything stays in the department's account: county CAD app details never leave it.
- **Only the reader reads the journal.** `sizeup-logread` runs as its own user and group (`sizeup-logs`, in `systemd-journal`)
  with no network, and hands over only the `sizeup-*` units' entries. The sizeup user (the web UI, the adapter) is
  not in `systemd-journal`: it never sees sshd, sudo or anything else in the journal.
- **The adapter's own lines stay on the station** unless an admin turns on *Ship the adapter's own lines* (with a
  warning): lines naming an installed adapter (entry point, module, distribution, source id or label, read at
  runtime, without case; and its hosts: the host of any URL or host name among its non-secret credentials), the
  runner's `incident sources:` lines, a traceback's exception line after a withheld frame, `sizeup-update` lines naming
  its distribution, `basicConfig`-style `LEVEL:logger:` lines, a third-party Python logger's `[log name]` lines and traceback
  frames in site-packages. Counted as "adapter" in the drop counts.
- **Scrub, fail closed** (on the station, where every credential is known). Text is NFKC-normalised and zero-width
  characters removed first. A line with a known credential value is dropped: the station token, the Access service
  token and the status token (from the environment and the config), and every secret field of the adapter's
  credentials (a password, token, key, PIN: not a user name or a region) from the credstore (short ones as whole
  words) or SSM, in any of its forms (upper and lower case, hex, URL-encoded, JSON-, HTML- or repr-escaped, base64 at
  any alignment, so the base64 of `user:password` is caught whatever the user), checked on the whole line before it is
  cut. Cyrillic and Greek lookalike letters and `\xNN` escapes are mapped to ASCII before the key words are matched. The whole value is redacted
  for `key=value` / `key: value` / JSON pairs naming a password, pw, p, pin, pass, token, secret, key, cookie, session,
  auth or an SSM `Value`; a credential's name used as a key; `-p`, `--password`, `-u`, `--user` on a command line;
  Bearer / Basic / Digest auth; `user:password@host`; `user:password` and `('user', 'password')` after login or
  credential words; credential query parameters; AWS key ids and secrets; JWTs; long mixed-case tokens. Adapter
  credential names become `[credential]`. A password-like word still followed by anything is dropped. A scrub that
  fails drops its line and the shipping goes on.
- **The hub's pass** runs the same patterns again before writing, and on every row it shows, with only the hub's own
  values (its tokens and the secrets in its environment): a second pattern pass, not a second copy of the station's.
- **Cheap.** Debug is never sent (level threshold, default info). The same line (numbers ignored) goes 5 times per
  10 min per unit, then one `[repeated N× in 10 min]` line. Batches are gzipped.
- **Offline.** Batches wait on the station's disk (50 MB, oldest dropped first) until the hub and CloudWatch take them.

## On the station

| What | Where | Notes |
|---|---|---|
| The reader | `sizeup-logread.service`, `/usr/local/lib/sizeup/logread.py`, the system python3 | User and group `sizeup-logs`, in `systemd-journal`. No network (`PrivateNetwork`), no access to `/var/lib/sizeup`, `/etc/sizeup`, `/etc/credstore` or the releases. |
| The hand-off | `/var/lib/sizeup-logs/*.jsonl` (directory 2770, owner `sizeup-logs`, group `sizeup`) | Read every 10 s from the reader's journal cursor; the first read takes the last 10 min. At most 20 MB waiting: past it the reader writes nothing and keeps its cursor, so nothing is lost while `sizeup-station` is down. After 6 h of that the oldest files go. `status.json` there says `ok`, `full` or `no journal`. |
| The spool | `/var/lib/sizeup/state/logspool/*.json.gz` | Scrubbed batches waiting for the hub. 50 MB, oldest dropped first. |
| Rejected batches | `/var/lib/sizeup/state/logspool/rejected/` | A batch the hub will never take (HTTP 400, 413, 415 or 422). Set aside, said once in the log, never retried. The last 5 are kept. |
| Off | `SIZEUP_LOG_SHIP=off` in `/etc/sizeup/station.env`, or the setting below | The hand-off files are deleted unread. |

`install-linux.sh` creates the user, the directory and the service when it switches to a release that has them. An
update restarts the reader only when `logread.py` or its unit changed; it carries on from its cursor. Without the
reader the station says "no journal" and sends nothing.

Checks on the station: `systemctl status sizeup-logread`, `journalctl -u sizeup-logread`, and
`cat /var/lib/sizeup-logs/status.json`. The shipper's counts (sent, waiting, dropped and why) are on the hub in
Settings > Station > Logs.

## Set up

1. `bash 25-station-logs.sh` in the department's setup scripts (sizeup-aws): the log group and the task role's
   `station-logs` policy. No deploy and no task definition change.
2. Until then the hub says **Not configured**, and the station keeps its spool and tries again every 10 minutes.

## Settings (Station > Logs > Shipping)

| setting | default | what |
|---|---|---|
| Ship station logs | on | off: the station reads nothing (also `SIZEUP_LOG_SHIP=off` in `station.env`) |
| Least level | info | debug, info, warning, error |
| Only these units | all | e.g. `sizeup-station, sizeup-update` |
| Never these units | none | e.g. `sizeup-ais` |
| Ship the adapter's own lines | off | asks first: they can hold county CAD details |
| Keep (days) | 30 | the log group's retention: 7, 14, 30, 60, 90, 120, 150, 180 or 365 days (the hub sets it) |

## Querying

The Logs view runs one Logs Insights query when asked: source (station, hub, all), unit, level, text, the last
15 min to 7 days, or **±5** (minutes) around a line. At most 7 days and 200 rows per query from the view (the API
takes up to 500), one query at a time. Insights bills
$0.005 per GB scanned; a day of station logs is a few MB.

The hub's own group (`/ecs/sizeup-hub`) is kept 7 days (step 1).

## Grafana (optional, later)

A self-hosted Grafana can read the same groups with its built-in **CloudWatch** data source. Give it its own IAM
user or role with read-only `logs:StartQuery`, `logs:GetQueryResults`, `logs:StopQuery`, `logs:DescribeLogGroups` and
`logs:GetLogEvents` on `/sizeup/station` and `/ecs/sizeup-hub`, region `us-east-1`. Use the Logs query mode with the
same Insights syntax, for example:

```
fields @timestamp, unit, level, msg | filter level in ["warning", "error"] | sort @timestamp desc | limit 200
```

Keep Grafana inside the department (never a hosted Grafana service): the logs can name the county's systems.
