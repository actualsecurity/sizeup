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

Sent under the key `capabilities` in hello, and again whenever they change (by content hash, §5). Load is the
exception: it goes on every post, as a few numbers.

| key | from | example |
|---|---|---|
| `cpu` | both | `{cores: 4, load1: 1.2, pct: 38}` |
| `mem`, `disk` | both | `{total_gb: 16, free_gb: 9}`, `{clips_free_gb: 220}` |
| `gpu` | worker | `{name, vram_gb: 12, vram_free_gb: 7}`, or none |
| `sdrs` | station | today's `App.sdr()`: serial, tuner, state (ok / out / held / off), job, group, `held_by_other_app` (the dongle claims shared with another app) |
| `groups` | station | antenna groups (#609/#702/#710), as today |
| `channels` | station | **per-channel running state**: `running` / `waiting` (no dongle) / `out` (a dongle streaming nothing, #703/#710; a county dongle with no decode, #723) / `held` (by another app) / `shed` (to a higher job of its group) / `not_in_set` / `local` (a person's override, §2.4) |
| `whisper` | both | `{model, up, threads}`, plus the queue's `behind_live` (#715) for a station |
| `llm` | worker | `{model, version}` or `""` (stopped while gaming), as #733's `models` |
| `adapter` | station | `{installed: [source ids], configured: bool}`: never a credential, never a value |
| `state` | worker | `online / gaming / paused` (sttpool.STATES) |
| `net` | both | the hub round trip (ms) and the upload rate measured on the outbox / STT input |
| `build` | both | git sha (stations follow the hub's build; the worker client reports its own) |

Speed is **measured on the hub, never reported**: the pool's p50 claim→text, the real-time factor and #651's pre-alert
"quick" gate come from the hub's own timings. A node-reported speed is never trusted. The one exception is a station's
own pace, which it sends as `max_s` on each claim (below); that can only lower what it is offered.

### Roles

| role | needs | count | options (per node) |
|---|---|---|---|
| `radio-receive` | sz1, at least one SDR | many | **channel set**: each job (channel or talkgroup system) with coverage `exclusive`, `overlap` or `failover` (§2.4); antenna groups stay on the node |
| `feed-adapter` | sz1, `adapter.installed` and `configured` | **exactly one active** | its place in the role's **priority list** (one list for the cluster, not per node) |
| `stt-light` | sz1, whisper up | many | **whisper in pool** (default **off** on a new node; the migration carries #715's global switch to each existing station); classes allowed (only non-pre-alert ones); `max_s`, an upper bound only (below) |
| `stt-heavy` | sw1, `whisper` reported | many | classes allowed: Pages/Fire-EMS/firegrounds, Airband, Police alert, Other main channels, Re-runs, Context talkgroups, Shed clips (today's `sttpool` classes); `fast` (may take the pre-alert classes; below); `max_s` |
| `infer` | sw1, `llm` reported | many | **LLM jobs** on/off (default on, #733); models allowed (default: any the node serves, from #755's `models`) |
| `adsb` / `ais` | sz1, a dongle for it | many | the receiver dongle (today's `set_adsb_receiver`) |
| `training` | sw1, GPU | one at a time | later: not designed here |

Rules:
- The admin assigns roles; the hub never assigns them itself. A role whose needs the node's capabilities don't meet
  can still be ticked, so a box can be set up ahead of time. It shows as "waiting", and the scheduler skips the node.
- A role is a permission and a duty at once. It opens the node's routes (§3), and it is the only way work reaches the
  node.
- Role changes are written to the Settings audit like every other admin change.

`stt-light` keeps **every gate of #715**. A station claims only while all of these hold:
- its own queue is empty;
- it has had a **90 s quiet spell with none of its own traffic** (clips it sent to AWS count as traffic);
- its CPU is under 85 % (from `/proc/stat`);
- the hub says the pool takes stations.

On top of the gates:
- **Pre-emption:** an own URGENT, HIGH or LIVE clip aborts the remote decode, closes the connection and releases the
  lease.
- **Never its own clips back:** each event carries `origin_station`, and #725 covers catch-up rows.
- Every station decode is **greedy**.
- **`max_s`:** the length offered is `min(admin max_s, MAX_DECODE_S / measured rtf)`. The station measures its pace and
  sends `max_s` on each claim, as it does today.

`fast` on `stt-heavy` can only **narrow** #651's measured gate, never bypass it. That gate is the 20 min window plus a
probe every 3 min. Existing workers migrate with `fast` on, which is what they do today: every worker may take
pre-alert classes, gated only by speed.

### Health per node

Each node gets one row on Settings > Station > Health, and the same row in the Nodes pane:

| part | shown |
|---|---|
| link | last seen, build, update state, refusals (id_mismatch, revoked), clock skew against the hub (`hub.skew`) |
| load | CPU %, load, disk free; worker: gaming / paused / online |
| radio-receive | channels running / assigned, dongles ok / out / held, local overrides |
| feed-adapter | `active (gen 14)` / `standby #2` / `waiting`, the source's health (today's hysteresis) |
| stt-light / stt-heavy | STT model, jobs/h, p50 claim→text, losses, cooldown |
| infer | LLM model, jobs/h, invalid, cooldown |
| adsb / ais | messages/s |

Cluster alarms (Health, pushed like today's):
- a node offline with a role;
- no eligible feed-adapter node;
- a channel **running** on no node (built from running state, not from assignment);
- a lease that flaps (3 moves in 1 h).

## 2. Scheduling

The hub's leader routes everything. A standby routes nothing: the fence, as in `sttworkers.WorkerApi`.

### 2.1 Per job type

| job | eligible nodes | order | fallback | today's code |
|---|---|---|---|---|
| Radio capture | `radio-receive` with the channel in its set | coverage mode (§2.4) | the next node in a `failover` list, on channel health | `App` jobs, antenna groups |
| STT, pre-alert classes (fast) | `stt-heavy` with the class and `fast` | idle and quick (#651 gate) | AWS or the capturing station's whisper, **by the channel's engine** and the budget: a `local` channel goes to the station's whisper before AWS; only `aws` / `both` channels go to AWS first | `sttpool`, `channels.py` |
| STT, live / re-run / low | `stt-heavy` with the class | GPU first | idle `stt-light` after a 1 s deferral (#715; never an AWS-engine channel's clip), then AWS by priority and budget, then the capturing station's whisper | `sttpool` + #715 |
| STT, context / shed | `stt-heavy`, then `stt-light` | GPU, then the idle station | marked shed, and the catch-up runs it | #715 |
| Infer (LLM JSON) | `infer` on, `llm` up, not gaming, no pre-alert lease held | priority, then age | none: rules only (any cloud LLM stays off) | `cloud/infer.py`; never holds a GPU that could take a pre-alert clip: the worker preempts generation for a pre-alert claim (#755) |
| Feed adapter | `feed-adapter`, adapter installed and configured | priority list | the next node in the list (§2.2) | station only, one station |
| ADS-B / AIS | `adsb` / `ais` | all run | the others; the hub merges by ICAO hex / MMSI | one station |

What stays as it is: the pool's classes, budgets, leases, heartbeats (6 s, lost at 12 s), `MAX_LEASES`, cooldowns,
the infer lane's preemption and the "nothing is ever dropped" fallbacks. The registry changes only **which nodes are
in the pool's lists**: today's "every active worker, every class on" becomes "the nodes whose role and options allow
this class". `stt-light` keeps every #715 gate listed in §1. A node is offered only clips up to its `max_s`.

### 2.2 Exactly-one roles: the feed-adapter lease

The county CAD feed adapter must have **at most one poller** in the cluster. It uses a shared account, it is polled
gently, and its log-ons are counted. The design reuses the leader lock's shape (`sizeup/leaderlock.py`): a generation
counter, self-fencing with a safety margin, and a grace period for the taker. The difference is that the hub is the
arbiter, so the lease lives in hub.sqlite (replicated) and not in S3.

| step | rule |
|---|---|
| grant | the first eligible node in the priority list (online, role on, adapter installed and configured) gets `{role, holder, gen, ttl_s}` in its state answer. Time is **relative** (`ttl_s`); an `expires_at` is advisory at most. The lease id is the pair **(hub leader-lock generation, lease gen)**, always compared as a pair |
| start | the adapter starts only on a grant **received by this process**. A grant is never written to disk on the node: stations restart about 13 times a day, and the 90 s TTL rides through a restart |
| renew | every state post (about 5 s) renews it. TTL **90 s**, longer than a hot/cold hub handoff (35 to 60 s). Grant and renew are idempotent per (node, gen), because the station's HubClient re-sends a POST once when a keep-alive connection drops |
| self-fence | the holder stops **polling** (only that) **75 s** (TTL − 15 s) after the send of its last answered renew, counted on **`CLOCK_BOOTTIME`**. Plain monotonic time stops during suspend and WSL host sleep, and BOOTTIME doesn't. The check runs right before **every** poll. A **wall step** means the lease is lost until the hub grants it again. A step is defined as in `docs/native/capture-split.md` §4.3 "Clocks": the offset wall − BOOTTIME changing by **more than 5 s between two samples at most 5 s apart**. The production station corrects its wall clock in small backward steps of up to about 2 s every 30 to 40 s (#788); those are not steps, or the lease would drop constantly. **Clock error:** the timing is safe for a station clock off by up to about ±25 %, because take-over is always timed on the hub's clock. The holder fences at 75 s of its own BOOTTIME; the hub re-grants after TTL + 15 = 105 s of its own clock. A fast station clock fences earlier, which is the safe way: at the measured 4.7 % (#788) the holder stops at about 71.6 real s, and the margin before the hub re-grants grows from 30 to about 33 s. A slow clock fences later: at −25 % it stops at 100 real s, still before 105. A fast clock also shortens how long the holder rides through a hub handoff with no renewals answered: 75 s against a handoff of up to 60 s leaves 15 s; at 4.7 % it is 71.6 s, about 11.6 s to spare, still over the 35 to 60 s handoff. (At +25 % the holder would fence at 60 real s: still exactly one poller, but it could stop during a long handoff) |
| take over | the hub grants the next node only after the TTL + 15 s. A new hub leader may have restored the lease table from a Litestream replica that lags. It grants no exactly-one lease until its own start + TTL + 15 s, unless the stored holder renews first |
| fail over on | the node unheard or past its lease, the role removed or the node revoked (at once), the node reporting the adapter not installed or not configured, the adapter's reports stale 3 min while the node is heard (a hung thread), the admin's **Move** |
| never fail over on | the source itself being bad (`backing_off` / `failing` from the far side: every node would see the same), or the adapter **stopped after refused log-ons** (an account problem: a second node would only add refused log-ons; a person resets it, as #301 says) |
| failback | none automatic (each move is one log-on on the shared account). Move by the admin, or see open question 2 |
| events | `source_*` events carry the lease id in their data, behind a hub cap. A station from before this sends none and counts as the legacy holder. Events from any generation are **applied**: an old holder's outbox draining after a partition is real data, deduped by the source's own ids. **Health and the acting primary come only from the current lease**, and health from an older one is ignored. A test covers this |
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
| `exclusive` | one node | none | none: the channel is dark while that node is (a cluster alarm). The pane warns when a fire, paging or alert-tier channel has only one node (one line, details in a hover) |
| `failover` | the first healthy node of a ranked list | none | moves on **channel health** (below) |
| `overlap` | every listed node | yes | any copy will do. A P25 system is one job per node (one trunk-recorder), so overlap there means two receivers of the same site |

**The hub sends the wish; the node answers with what runs.** The hub can't know whether a node has a free dongle on
the right antenna. It sends the channel set (`set_radio_role`), and the node's antenna groups answer with each
channel's running state (§1 `channels`).

**Failover on channel health, not only a node going offline.** The trigger is the channel not being `running` on the
first node for 60 s. That covers:
- a node that is offline;
- a dongle that opens but streams nothing (#710);
- a group that shed the job to a higher-priority one;
- a dongle held by another app.

A target that answers anything but `running` counts as unhealthy, and the hub tries the next node. Fail back after
10 min healthy, with #609's flap hold (2 / 10 / 30 min), so a flapping channel can't ping-pong between nodes.

**Local control stays.** A person can still pick a job outside the node's set on the node's Receivers page. That pick
is audited, shown as `local override` on Health, and counted as coverage. Auto-assign stays inside the set. Migration
puts each station's current jobs in its set, so nothing changes on day one.

**Overlap dedupe.**

Matching is done on **hub-corrected time**: each node's start time plus the per-node skew the hub measures
(`hub.skew`), tracked all the time. Station clocks under WSL2 drift and step (#720). If a node's skew is unknown or more
than 0.5 s, nothing is matched and both rows are kept.

| kind | same transmission when |
|---|---|
| P25 | same system and talkgroup, and the hub-corrected start within ±1 s. The source unit is a **tie-breaker only**, never a reason to keep two rows: a Phase 2 SUID and the MAC_PTT source address differ between decoders (#647's aliases), and a weak copy may have none. With #647 splitting on, matching is per **turn**, and one node's turn may overlap several of another's (1:N by interval overlap) |
| conventional | same channel, and the intervals overlap by ≥ 60 % of the shorter piece. Squelch tails, hang time and #645's key-up cuts differ per node, so one node's A+B may be another's A and B. MDC / FleetSync IDs decoded on both copies are the strongest key |
| paging | tone pair within ±2 s. Also when one node decoded the tones and the other got only the voice (overlap on the page window) |

- **Nothing waits.** The first copy makes the radio log row, and a page makes its incident, at once. Later copies
  attach to that row (`copies: [{node, score}]`) and never make a second row or a second incident. Late copies attach
  the same way, including calls replayed after a station restart (#753).
- **Attribution is the union of all copies.** Unit IDs, the emergency flag and the "ID not decoded" note are merged
  across copies (#690 / #694 / #698).
  - An emergency alarms on the **first copy that has the flag**, from any node.
  - A later copy that adds the flag alarms; one that repeats it doesn't (one alarm per press, as #698).
- **Best copy.** Each node scores its own copy:
  - P25: decode errors, and whether the end is cut off;
  - FM: the margin over **that node's own noise floor** (#606 / #685) and the quieting fraction during the
    transmission. Raw dB doesn't compare across antennas and gains.
- **STT input.**
  - Pre-alert classes have a 0 s hold. The first copy's node sends its input at once, as today (#701). If a copy that
    scores much better arrives later, the hub asks that node for its input (`send_clip`, which carries #671's stt-src
    blob) and runs **one** re-run.
  - That re-run is a **new transcript version** and never overwrites the first copy's raw text. It re-fires no
    pre-alert or intel push, under the same rule as #753's retext gate.
  - Other classes: **nodes push no STT input.** After the hold, the hub asks the chosen copy's node for it (an
    `stt_send` job, beside `send_clip`), so no uplink or encoder CPU is spent on copies that get dropped.
  - The cluster therefore transcribes each transmission once. `stt_runs` records `copy_node` and `copy_score`, so
    evaluation can measure whether choosing the best copy helps.
- **Count each transmission once.** Metrics and labels, such as trailing-time rates (#736) and agreement and
  auto-labels (#728), count a deduped transmission once, not once per copy.
- **HA copies:** the other copies stay on their nodes for 24 h. If the chosen node's audio never arrives, the hub asks
  another node for its copy (`send_clip`). The retention index is kept on disk, so this works even for a node that has
  restarted since.

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
| `GET /api/station/jobs` (long poll) | yes (the jobs for that node and its roles only) | | never |
| `POST /api/station/jobs/<id>` (a job's answer) | yes (once jobs are targeted: only a job handed to that node) | | never |
| `POST /api/station/stt` (each captured clip's STT input, beside its event) | | radio-receive | never |
| `/api/station/aircraft` / `vessels` | | adsb / ais | never |
| `/api/station/stt/hello`, `state`, `claim`, `jobs/<lease>/{audio, extend, complete, release}`: exactly #715's list, no others | | stt-light with whisper in pool on (off: **403**, as today, so the station backs off instead of retrying; never 404) | stt-heavy |
| `/api/station/stt/infer/*` | **never** (403, #733) | never | infer, LLM jobs on |
| `/api/station/train/*` (later) | never | never | training |

Invariants (each one gets a test):
- **Prompts go only to nodes that hold `sw1` and `infer`.** This generalises #733. A station's identity never
  receives a prompt, whatever its roles.
- **Pre-alert clips (the fast classes, police alert among them) go only to nodes that hold `stt-heavy` with `fast`
  and that class.** A station's whisper never gets one (#715).
- **A clip's audio goes only to the node that holds that clip's live lease**, fetched by lease id. Nothing lists clips.
- **A node never gets STT input from its own channels back through the pool** (`origin_station`).
- **Training data (real audio and transcripts) goes only to a node with `training`**, and only while the admin's
  setting allows it. It never goes to the repo or anywhere public (open question 5).
- **The feed's data goes only to the hub.** It goes into no node's job, clip or prompt unless intel's own setting
  allows it (default off).
- A `sw1` credential opens no `/api/station/` route outside `stt/`, and a `sz1` credential opens no `infer` or
  `train` route.
- A test lists every `/api/station/*` route and fails when one has no row in the table (fail closed, like #581's
  `need`).
- The payload's id must be the token's (`id_mismatch`, as today).
- No `HEAD` under `/api/station/`: 405 and no handler runs (a `GET` there does things: the long poll hands each job
  out once). Bodies both ways are strict JSON: no `NaN` or `Infinity` (sent as `null`, read as `null`).

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
| 1 | **Registry and capabilities.** A `nodes` table and a `NodeRegistry` over the two credential tables: one node per existing station or worker id, and an id used by both becomes one `station+worker` node. `capabilities` go in the station's state and the worker's hello, are kept in memory and shown on Health. No behaviour change. Native writes the schemas first (wire contract below) | main session (registry); native session (wire contract, schemas, conformance tests) | none | unchanged |
| 2 | **STT and infer roles.** Each worker gets `stt-heavy` with `fast` on and today's global classes copied in as its options, and `infer` from its "LLM jobs" switch (`stt_llm`). Each station gets `stt-light` with #715's switch. The pool's eligibility comes from roles and options; **the global per-class switches are still read as a cap until step 9**, so turning a class off globally still works. The route table for `stt/*` and `infer/*`, with the invariant tests. No pane UI: the STT workers pane reads and writes the roles through its existing controls | speech-to-text session | 1, #715, #733, #755 (the worker's `models`) | unchanged controls |
| 3 | **Per-node state, cursors, jobs** (A2 to A4). Today the hub keeps **one global events cursor** `(epoch, seq)`. Because of it, a second station's events with `seq` ≤ that cursor are skipped, and a new outbox without a new epoch is skipped entirely. This step makes the cursor **per node**, adds per-event-type handling of poison batches (native is fixing that part separately), and lifts `multi_station_off` | main session | 1 | the Stations pane lists per-node state |
| 4a | **Coverage reporting.** Channel sets mirror today's jobs; nodes report per-channel running state; Health and the "running on no node" alarm. No behaviour change | radio session | 3 | Settings > Radios unchanged |
| 4b | **Failover** on channel health, with the flap hold; `set_radio_role`; local overrides audited | radio session | 4a | channel sets edited on the node |
| 5a | **Dedupe, logging only.** Hub-corrected clocks (skew on Health) and the matching rules; logs "would merge" only, like #647's `log` mode, measured on real traffic from two nodes | radio session | 4a | none |
| 5b | **Merging on.** Copies, attribution merged across copies, best copy, HA copies, `send_clip`; metrics count each transmission once | radio session; speech-to-text session (re-run versions, `copy_node` / `copy_score`, metrics and labels) | 5a | none |
| 5c | **`stt_send`.** The chosen copy's STT input sent on request; nodes push no input for non-pre-alert classes | radio session with the speech-to-text session | 5b | none |
| 6 | **Feed adapter, exactly one.** The lease table, the grant in the state answer, the runner gate in `sizeup/sources/runner.py`, the lease id on `source_*` events, Move, Health | main session (hub, runner); native session (contract, the BOOTTIME fence) | 3 (per-node cursors are a hard prerequisite) | the Adapter tab unchanged |
| 7 | **Nodes pane** beside the old tabs, behind a Settings > System switch (admins only), with a Health row per node | main session | 2, 4b, 6 | both still there |
| 8 | **Switch.** The Nodes pane becomes the default. The old tabs and `/api/cad/stations` and `/api/cad/stt-workers` stay for one release as aliases | main session; reviewed by the speech-to-text session | 7 | hidden |
| 9 | **Remove** the old panes, the aliases and the global per-class pool switches | main session | 8, plus one release | gone |

Steps 2 and 3 can run in parallel after step 1. After step 3, steps 4a, 5a and 6 can run in parallel. The CLIs
(`python -m sizeup.cloud.stations` and the workers' equivalent) keep working through step 8. In step 9 a `nodes` CLI
with the same leader-lease check replaces them.

### Wire contract (native session)

| topic | rule |
|---|---|
| names | the node reports **`capabilities`**. The name `caps` is taken: it is the hub-to-station list of capability strings |
| resync | by **content hash**, never counters. On each post the node sends `capabilities_rev`, and the hub answers `capabilities_wanted` when it lacks that version. The hub sends `roles_rev`, and sends the full roles only when the node's `roles_applied` differs |
| lease | `{role, holder, gen, ttl_s}`, relative time, the lease id being the pair (leader gen, lease gen) (§2.2) |
| versioning | unknown keys are ignored in both directions. New behaviour is turned on only by a `caps` string. Native writes JSON Schemas for node, role, capability and lease first, plus conformance tests |
| node files | written tmp + rename, and tolerant of unknown keys, so an older build reads a newer file after a rollback |
| size | load stays a few numbers. `/api/station/state` is gzipped, behind a hub cap: today's status is about 11.4 KB every 5 s |
| transport | `Content-Length` bodies only. Integers that may exceed 2^53 are sent as strings |
| routes | the fail-closed route test (§3) takes its fixture from native's route inventory |

## 6. Open questions for the admin

1. If the hub is unreachable for more than 75 s, should the feed adapter **stop** (strict, no chance of two pollers;
   proposed) or keep polling?
2. Should the adapter **fail back** to priority 1 when it returns? Proposed: never on its own; the admin presses Move.
3. Adapter credentials: should each node in the list get its own SSM read key (proposed: revocable one by one) or
   share one?
4. Dedupe: is 24 h of HA copies on the nodes OK? The 2 s hold for non-pre-alert classes is set only after 5a has
   measured the time between nodes' copies arriving, using `stt_in`'s arrival delay (#701).
5. Training (later): may audio and transcripts leave the hub for a node? The speech-to-text session recommends: only to
   `training` nodes, gated by the admin, with real data kept on our own storage. Where should models be kept (S3)?
6. Should the tab be called **Nodes**, or keep the name **Stations**?

Settled in review: the GPU box may also run the station agent. That makes two credentials (`sz1` and `sw1`) under one
node id.
