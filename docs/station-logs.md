# Station logs

The station's journal, in one place with the hub's own logs: CloudWatch Logs in the department's AWS account. Read it
on the hub in **Settings > Station > Logs** (admins).

## How it gets there

```
station journald (sizeup-* units)
  -> sizeup-station: journalctl every 15 s, filter, scrub, cut repeats      sizeup/cloud/logship.py, sizeup/logscrub.py
  -> <state>/logspool/*.json.gz (a batch every ~45 s, 50 MB cap)
  -> POST /api/station/logs (the station token + Access service token)     the link the station already has
  -> hub: filter and scrub again, PutLogEvents                             sizeup/cloud/cwlogs.py
  -> CloudWatch Logs /sizeup/station (Infrequent Access, 30 days)
```

- **No AWS key on the station.** The hub writes with its task role.
- **No third-party log service.** Everything stays in the department's account: county CAD app details never leave it.
- **Scrub, fail closed.** A line with a known credential value (the station token, the Access service token, any
  adapter credential read from the credstore or SSM) is dropped. Credential-shaped text is redacted: `key=value` pairs
  naming a password, token, secret, cookie or session; `Bearer` / `Basic` auth; a URL's `user:password@`; credential
  query parameters; AWS key ids and secrets; JWTs; long mixed-case tokens. Adapter credential names are replaced by
  `[credential]`. A line that still names a password with a value after it is dropped. The hub runs the same scrub
  again before writing.
- **Cheap.** Debug is never sent (level threshold, default info). The same line (numbers ignored) goes 5 times per
  10 min per unit, then one `[repeated N× in 10 min]` line. Batches are gzipped.
- **Offline.** Batches wait on the station's disk (50 MB, oldest dropped first) until the hub and CloudWatch take them.

The sizeup user reads the journal as a member of `systemd-journal` (`install-linux.sh` adds it when it switches
releases). Without it the station says "no journal access" and sends nothing.

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
| Keep (days) | 30 | the log group's retention (the hub sets it) |

## Querying

The Logs view runs one Logs Insights query when asked: source (station, hub, all), unit, level, text, the last
15 min to 7 days, or **±5** around a line. At most 7 days and 200 rows per query, one at a time. Insights bills
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
