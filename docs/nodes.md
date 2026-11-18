# Nodes: one registry, roles by hardware (design)

Status: design, no code yet (2026-11-17). Replaces the two separate registries, Settings > Station > **Stations**
(SDR stations, `sz1` tokens: #555, #660) and Settings > Station > **STT workers** (GPU workers, `sw1` tokens: #651,
#649, #733, and #715 for stations' whisper in the pool), with **one registry of nodes**. The admin gives each node
roles to suit its hardware.

Examples (invented names):

| node | hardware | roles |
|---|---|---|
| `st-north` | mini PC, 5 SDRs, 4 cores | radio-receive (its channels), feed-adapter (priority 1), stt-light (low classes), adsb |
| `st-south` | mini PC, 4 SDRs, 4 cores | radio-receive (overlapping channels), feed-adapter (priority 2), stt-light |
| `gpu-1` | the admin's gaming PC, 12 GB GPU | stt-heavy (every class), infer (intel's local model), later training |

The hub is not a node. It runs Amazon Transcribe and Polly and is the scheduler, the lease authority and the place
everything lands. AWS is the last fallback in the tables below, not a node.

## 1. Concepts

### Node

| field | what |
|---|---|
| `id` | `[a-z0-9][a-z0-9_-]{0,39}`, the same shape as station and worker ids today. One namespace for every node |
| `name` | free text, at most 60 characters |
| credentials | **kept separate by kind**, as today. `sz1.<id>.<secret>` is the **station agent** (`sizeup --role station`: radio, adapters, the station's whisper). `sw1.<id>.<secret>` is the **worker client** (`tools/stt_worker`: GPU whisper, LLM). A node has one or both. Each is stored hash-only in its own table (`stations`, `stt_workers`), and each has its own Rotate (10 min grace) and Revoke. The status token stays derived from `sz1` |
| `kind` | derived from the credentials, never set: `station` (sz1), `worker` (sw1), `station+worker` (both, e.g. a GPU box that also gets an SDR) |
| roles | set by the admin (below), with per-role options |
| capabilities | reported by the node (below). They advise the admin, and the scheduler checks them at run time |

Storage: a new hub.sqlite table `nodes (id, name, roles JSON, options JSON, created, created_by, revoked)`, made by the
first write (never at the hub's start: `tests/test_startup_writes.py`). `stations.id` and `stt_workers.id` stay the
credential's id and equal the node id. #660's rename and adoption of the shared token's station (aliases, history
moves) moves the node row with it through `on_rename`.

### Capabilities (reported, never trusted for security)

Reported in hello and whenever they change, not on every state post. Load is the exception: it goes on every post.

| key | from | example |
|---|---|---|
| `cpu` | both | `{cores: 4, load1: 1.2, pct: 38}` |
| `mem`, `disk` | both | `{total_gb: 16, free_gb: 9}`, `{clips_free_gb: 220}` |
| `gpu` | worker | `{name, vram_gb: 12, vram_free_gb: 7}`, or none |
| `sdrs` | station | today's `App.sdr()`: serial, tuner, state (ok / out / held / off), job, group |
| `groups` | station | antenna groups (#609/#702/#710), as today |
| `whisper` | both | `{model, up, threads}`, plus the queue's `behind_live` (#715) for a station |
| `llm` | worker | `{model, version}` or `""` (stopped while gaming), as #733's `models` |
| `adapter` | station | `{installed: [source ids], configured: bool}`: never a credential, never a value |
| `state` | worker | `online / gaming / paused` (sttpool.STATES) |
| `net` | both | the hub round trip (ms) and the upload rate measured on the outbox / STT input |
| `build` | both | git sha (stations follow the hub's build; the worker client reports its own) |

### Roles

| role | needs | count | options (per node) |
|---|---|---|---|
| `radio-receive` | sz1, at least one SDR | many | **channel set**: each job (channel or talkgroup system) with coverage `exclusive`, `overlap` or `failover` (§2.4); antenna groups stay on the node |
| `feed-adapter` | sz1, `adapter.installed` and `configured` | **exactly one active** | its place in the role's **priority list** (one list for the cluster, not per node) |
| `stt-light` | sz1, whisper up | many | **whisper in pool** (default **off** on a new node; the migration carries #715's global switch to each existing station); classes allowed (only non-pre-alert ones); `max_s` |
| `stt-heavy` | sw1, `whisper` reported | many | classes allowed: Pages/Fire-EMS/firegrounds, Airband, Police alert, Other main channels, Re-runs, Context talkgroups, Shed clips (today's `sttpool` classes); `fast` (may take the pre-alert classes, still gated by #651's measured speed); `max_s` |
| `infer` | sw1, `llm` reported | many | **LLM jobs** on/off (default on, #733); models allowed (default: any the node serves) |
| `adsb` / `ais` | sz1, a dongle for it | many | the receiver dongle (today's `set_adsb_receiver`) |
| `training` | sw1, GPU | one at a time | later: not designed here |

Rules:
- The admin assigns roles; the hub never assigns them itself. A role whose needs the node's capabilities don't meet
  can still be ticked, so a box can be set up ahead of time. It shows as "waiting", and the scheduler skips the node.
- A role is a permission and a duty at once. It opens the node's routes (§3), and it is the only way work reaches the
  node.
- Role changes are written to the Settings audit like every other admin change.

### Health per node

Each node gets one row on Settings > Station > Health, and the same row in the Nodes pane:

| part | shown |
|---|---|
| link | last seen, build, update state, refusals (id_mismatch, revoked) |
| load | CPU %, load, disk free; worker: gaming / paused / online |
| radio-receive | channels running / assigned, dongles ok / out, channels with no node now (cluster alarm) |
| feed-adapter | `active (gen 14)` / `standby #2` / `waiting`, the source's health (today's hysteresis) |
| stt-light / stt-heavy | STT model, jobs/h, p50 claim→text, losses, cooldown |
| infer | LLM model, jobs/h, invalid, cooldown |
| adsb / ais | messages/s |

Cluster alarms (Health, pushed like today's): a node offline with a role, no eligible feed-adapter node, a channel
with no node, a lease that flaps (3 moves in 1 h).

## 2. Scheduling

The hub's leader routes everything. A standby routes nothing: the fence, as in `sttworkers.WorkerApi`.

### 2.1 Per job type

| job | eligible nodes | order | fallback | today's code |
|---|---|---|---|---|
| Radio capture | `radio-receive` with the channel in its set | coverage mode (§2.4) | the next node in a `failover` list | `App` jobs, antenna groups |
| STT, pre-alert classes (fast) | `stt-heavy` with the class and `fast` | idle and quick (#651 gate) | AWS, then the capturing station's whisper | `sttpool` |
| STT, live / re-run / low | `stt-heavy` with the class | GPU first | idle `stt-light` after a 1 s deferral (#715; never an AWS-engine channel's clip), then AWS by priority and budget, then the capturing station's whisper | `sttpool` + #715 |
| STT, context / shed | `stt-heavy`, then `stt-light` | GPU, then the idle station | marked shed, and the catch-up runs it | #715 |
| Infer (LLM JSON) | `infer` on, `llm` up, not gaming, no pre-alert lease held | priority, then age | none: rules only (any cloud LLM stays off) | `cloud/infer.py` |
| Feed adapter | `feed-adapter`, adapter installed and configured | priority list | the next node in the list (§2.2) | station only, one station |
| ADS-B / AIS | `adsb` / `ais` | all run | the others; the hub merges by ICAO hex / MMSI | one station |

What stays as it is: the pool's classes, budgets, leases, heartbeats (6 s, lost at 12 s), `MAX_LEASES`, cooldowns,
the infer lane's preemption and the "nothing is ever dropped" fallbacks. The registry changes only **which nodes are
in the pool's lists**: today's "every active worker, every class on" becomes "the nodes whose role and options allow
this class". `stt-light` keeps #715's gates (own queue empty, CPU < 85 %, the hub says the pool takes stations).
`max_s` caps the clip length a node is offered.

### 2.2 Exactly-one roles: the feed-adapter lease

The county CAD feed adapter must have **at most one poller** in the cluster. It uses a shared account, it is polled
gently, and its log-ons are counted. The design reuses the leader lock's shape (`sizeup/leaderlock.py`): a generation
counter, self-fencing with a safety margin, and a grace period for the taker. The difference is that the hub is the
arbiter, so the lease lives in hub.sqlite (replicated) and not in S3.

| step | rule |
|---|---|
| grant | the first eligible node in the priority list (online, role on, adapter installed and configured) gets `{role, holder, gen+1, expires_at}` in its state answer. The node starts the adapter only after it has received a grant |
| renew | every state post (about 5 s) renews it. TTL **90 s**, longer than a hot/cold hub handoff (35 to 60 s) |
| self-fence | the holder stops the adapter **75 s** (TTL − 15 s) after the last renew it **sent** that was answered, on the monotonic clock |
| take over | the hub grants the next node only after `expires_at + 15 s`. A hub that took over from a replica takes `expires_at = max(stored, its start + TTL)` |
| fail over on | the node unheard or past its lease, the role removed or the node revoked (at once), the node reporting the adapter not installed or not configured, the adapter's reports stale 3 min while the node is heard (a hung thread), the admin's **Move** |
| never fail over on | the source itself being bad (`backing_off` / `failing` from the far side: every node would see the same), or the adapter **stopped after refused log-ons** (an account problem: a second node would only add refused log-ons; a person resets it, as #301 says) |
| failback | none automatic (each move is one log-on on the shared account). Move by the admin, or see open question 2 |
| events | `source_*` events carry `gen`. They are accepted from any generation (an old holder's outbox draining after a partition is real data, deduped by the source's own ids), but **health and the acting primary come only from the current generation** |
| while it moves | paging is the acting primary (the existing source-priority fallback). The gap is at most about 105 s plus one log-on |

The same lease table holds any later exactly-one role (`training` at a time), keyed by role.

### 2.3 Many-node roles

Many-node roles (radio-receive, stt-*, infer, adsb, ais) take no lease. Each job is leased per item (STT and infer
already work this way), and capture runs on every node assigned to it.

### 2.4 Radio coverage and dedupe

Each channel (a conventional channel, a P25 system's talkgroup set, paging) has one coverage mode, set in the channel
set:

| mode | runs on | dedupe | HA |
|---|---|---|---|
| `exclusive` | one node | none | none: the channel is dark while that node is (a cluster alarm) |
| `failover` | the first healthy node of a ranked list | none | the hub sends the next node `set_radio_role` when the first is offline for 60 s, and moves it back when that node has been back 10 min |
| `overlap` | every listed node | yes | any copy will do |

Dedupe for `overlap`:
- **Same transmission:**
  - P25: system, talkgroup, source unit and call start within ±1 s;
  - conventional: channel, start within ±1 s and length within ±1 s;
  - paging: tone pair within ±2 s.
- **Nothing waits.** The first copy makes the radio log row, and a page its incident, at once. Later copies attach to
  that row (`copies: [{node, score}]`). They never make a second row or a second incident.
- **Best copy:** each node scores its own copy (P25 decode errors, FM SNR and squelch margin, a cut-off end). The hub
  picks the STT copy after a hold window:
  - pre-alert classes: 0 s. The first copy is transcribed. A copy that scores much better arriving later triggers one
    re-run.
  - others: 2 s.
  Only the chosen copy is sent as STT input (#671), so the cluster spends STT capacity once per transmission.
- **HA copies:** the others stay on their nodes for 24 h. If the chosen node's audio never arrives, the hub asks
  another node for its copy (job `send_clip`).
- Antenna groups stay a node's own business: the channel set says which jobs a node should run, and its groups pick
  the dongle. A job not in the node's set is never started, by a person or by auto-assign.

Prerequisite: per-node state, outbox cursors and targeted jobs (multi-station design A2 to A4). Until they land, the
hub still takes one station (`multi_station_off`).

## 3. Security

### Route allow-lists (deny by default)

The hub resolves the bearer to `(node, kind)` before it reads the body (today's `_principal` and
`WorkerRegistry.resolve`), then checks the route against this table. Roles are read on each request, like token rows,
so a role removed or a node revoked counts from its next request.

| route family | sz1 always | sz1 + role | sw1 + role |
|---|---|---|---|
| `/api/station/hello`, `state`, `logs`, `metrics`; `GET status` (status token) | yes | | never |
| `/api/station/events` (pages, transmissions, re-runs, alarms) | | radio-receive | never |
| `/api/station/events` `source_*` | | feed-adapter (`gen` checked for health) | never |
| `/api/station/jobs` (long poll) | yes (the jobs for that node and its roles only) | | never |
| `/api/station/aircraft` / `vessels` | | adsb / ais | never |
| `/api/station/stt/hello`, `state`, `claim`, `jobs/<lease>/*` | | stt-light, whisper in pool on | stt-heavy |
| `/api/station/stt/infer/*` | **never** (403, #733) | never | infer, LLM jobs on |
| `/api/station/train/*` (later) | never | never | training |

Invariants (each one gets a test):
- **Prompts go only to nodes that hold `sw1` and `infer`.** This generalises #733. A station's identity never
  receives a prompt, whatever its roles.
- **Pre-alert clips (the fast classes, police alert among them) go only to nodes that hold `stt-heavy` with `fast`
  and that class.** A station's whisper never gets one (#715).
- **The feed's data goes only to the hub.** It goes into no node's job, clip or prompt unless intel's own setting
  allows it (default off).
- A `sw1` credential opens no `/api/station/` route outside `stt/`, and a `sz1` credential opens no `infer` or
  `train` route.
- A test lists every `/api/station/*` route and fails when one has no row in the table (fail closed, like #581's
  `need`).
- The payload's id must be the token's (`id_mismatch`, as today).

### Tokens, Access, rotation

- Tokens: unchanged formats, hash-only, shown once, 10 min Rotate grace, Revoke immediate (`on_revoke` falls back
  what the node holds). **Revoke node** revokes every credential it has and drops its leases. Revoking one credential
  leaves the node's other kind working.
- Cloudflare Access (the edge layer; the hub's table above is the real check):
  - **one service token per node per credential kind**;
  - an Access application for `/api/station/stt/*`, whose policy admits workers' tokens and the tokens of stations
    with stt-light;
  - the wider `/api/station/*` application admits station tokens only.

  Revoking a node in sizeup doesn't touch Cloudflare, so the pane's revoke confirm names the service token to revoke
  there too (an operator step, as today).
- Feed-adapter credentials stay on the node (systemd credstore, SSM read key). Every node in the priority list needs
  its own setup, and the hub never holds or relays them (`adapterssm.py`, `adapterpass.py` unchanged).

## 4. UI: Settings > Station > Nodes

This one tab replaces the Stations and STT workers tabs. Health and Logs stay.

```
Nodes                                                        [Add node]   STT pool [on]
 st-north   station        radio · feed (active) · stt-light · adsb      online  12:41
 st-south   station        radio · feed (#2) · stt-light                 online  12:41
 gpu-1      worker         stt-heavy · infer                             gaming  12:40
```

Row expanded:

```
 4 cores · load 1.2 · 9/16 GB · 220 GB free · 38 ms
 SDR 5: 4 ok · 1 out          whisper small.en · up
 Roles   [x] Radio receive   9 channels  [Edit]
         [x] Feed adapter    #1 of 2     [Move]
         [x] STT light       pool [ ]  Context · Shed  · max 60 s
         [ ] STT heavy       ⓘ needs a worker token
         [ ] Infer           ⓘ needs a worker token
         [x] ADS-B           dongle 0005
 Tokens  station sz1 · rotated 3 d   [Rotate] [Revoke]     worker  [Add]
                                                                     [Revoke node]
```

Concise-UI rules: one line per fact and numbers over prose. A role the hardware can't run is greyed, and the reason is
in its ⓘ hover, never in the panel. Tokens show once in a copy box. Confirms only for what affects a live node: Move,
Revoke, removing the last radio node of a channel. The feed-adapter priority list is ordered in place (drag, or
↑/↓). The STT pool's master **On** stays at the top, as today.

## 5. Migration: small PRs, today's panes working until the switch

| # | PR | owner | depends on | old panes |
|---|---|---|---|---|
| 1 | **Registry + capabilities.** `nodes` table, `NodeRegistry` over the two credential tables (a node per existing station or worker id; an id used by both becomes one `station+worker` node), `caps` in station state / worker hello, kept in memory and on Health. No behaviour change | main session (registry), native session (wire contract: `caps`, `roles`, `roles_rev`, `lease` fields; unknown fields ignored both ways; an old client sends none, which counts as unknown) | none | unchanged |
| 2 | **STT and infer roles.** Each worker gets `stt-heavy` with today's global classes copied as its options, and `infer` from its "LLM jobs" switch (`stt_llm`). Each station gets `stt-light` with #715's switch. The pool's eligibility comes from roles and options. Route table for `stt/*` and `infer/*`, with the invariant tests | speech-to-text session | 1, #715 merged | the STT workers pane reads and writes the roles (same controls) |
| 3 | **Per-node state, cursors, jobs** (A2 to A4). `multi_station_off` lifted | main session | 1 | the Stations pane lists per-node state |
| 4 | **Radio-receive role.** Channel sets with coverage modes, `set_radio_role` job, antenna groups respected, `exclusive` / `failover` first | radio session | 3 | Settings > Radios unchanged; channel sets edited on the node |
| 5 | **Overlap dedupe.** Copies, best-copy choice, HA copies, `send_clip` | radio session (capture, score), speech-to-text session (one STT input per transmission) | 4 | none |
| 6 | **Feed-adapter exactly-one.** Lease table, grant in the state answer, runner gate in `sizeup/sources/runner.py`, `gen` on `source_*`, Move, Health | main session (hub, runner), native session (contract review) | 3 | the Adapter tab unchanged |
| 7 | **Nodes pane** beside the old tabs, behind a Settings > System switch (admins only), with Health rows per node | main session | 2, 4, 6 | both still there |
| 8 | **Switch.** The Nodes pane becomes the default; the old tabs and `/api/cad/stations`, `/api/cad/stt-workers` stay one release as aliases | main session; review by the speech-to-text session | 7 | hidden |
| 9 | **Remove** the old panes, aliases and the global per-class pool switches | main session | 8 + one release | gone |

Steps 2, 3 and 4 can run in parallel once step 1 is in. The CLIs (`python -m sizeup.cloud.stations`, the workers'
equivalent) keep working through step 8. A `nodes` CLI with the same leader-lease check replaces them in step 9.

## 6. Open questions for the admin

1. If the hub is unreachable for more than 75 s, should the feed adapter **stop** (strict, no chance of two pollers;
   proposed) or keep polling?
2. Should the adapter **fail back** to priority 1 when it returns? Proposed: never on its own; the admin presses Move.
3. Adapter credentials: should each node in the list get its own SSM read key (proposed: revocable one by one) or
   share one?
4. Dedupe: is a hold of 2 s for non-pre-alert classes and 24 h of HA copies on the nodes OK?
5. Should the GPU box run the station agent too (an SDR on it: one `station+worker` node), or stay worker-only?
6. Training (later): may audio and transcripts leave the hub for a node, and where should models be kept (S3)?
7. Should the tab be called **Nodes**, or keep the name **Stations**?
