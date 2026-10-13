# sizeup roadmap

Where this is going, in what order, and what has to be decided first. Updated when the direction
changes, not every PR. Day-to-day work lives in
[GitHub issues](https://github.com/actualsecurity/sizeup/issues).

## What sizeup is for

A self-dispatch CAD that Port Chester FD *wants* to use. It's clear, fast and adjustable to how the
department works, and it hears the radio so the screen keeps up with the call by itself. It supplements
county dispatch (60 Control); it doesn't replace it.

## How work is tracked

- **Issues** are the backlog, one per piece of work or open question. PRs close them (`Fixes #n`).
  Anything raised in conversation gets an issue, so nothing lives only in someone's head or a chat.
- **Labels:** `now` (this week), `next` (ready to pick up), `later` (wanted, not soon), `waiting`
  (blocked on the department, a vendor, hardware or a decision).
- **No process beyond that** (no sprints, estimates or boards) until more people are working on it.
- **This file** holds the phases and the decisions. The detail lives in the issues and in
  [cad-plan.md](cad-plan.md) and [cloud.md](cloud.md).

## Where it stands (October 2026)

- **Hub** on AWS (ECS Fargate, one small task) behind a Cloudflare Tunnel and Access, at
  sizeup.actualsecurity.org. Sign-in is by email one-time PIN. Roles are admin, dispatcher and viewer,
  with per-user activity and request logs. Litestream backs it up to S3. A merge to main
  deploys it.
- **Station:** the PC with the RTL-SDRs. It calls home over outbound HTTPS only and buffers while the
  hub or the internet is down. It runs on the Mac today, and an installer for Windows (WSL2) and
  Linux / Raspberry Pi is merged (#28).
- **Radio:**
  - Channels: South 2 paging (tones and pages), Fire/EMS 15 (P25, talker at keypress) and FG1
    (MDC-1200 IDs).
  - Speech-to-text per channel: Amazon Transcribe on the main channels, whisper elsewhere.
  - Pre-alert: the phone pre-alert from Tapout's ntfy feed (read only).
- **CAD:**
  - Events: from 60 Control pages (ours by tones or town) or entered by hand.
  - Run cards, including full department and "any apparatus".
  - Dispatch wording, said by Amazon Polly.
  - Unit boards, add a unit as responding, one incident commander.
  - Radio traffic linked to events.
  - A read-only wall board at /monitor.
- **Transmit is off.** The Dispatch button logs and plays locally.

## Phases

### 1. Trial (now)

A few people use it alongside the real system and say what's wrong.

- Station moved to the HQ PC and left running unattended through restarts and a power cut (#32)
- Settings caught up after the October deploys (#33)
- Feedback from Julian and the other early users, filed as issues

**Done when:** the station has run unattended for a few weeks, early users rely on it to follow
calls, and the department has seen it.

### 2. Dependable

Things that have to be true before anyone relies on it during a call.

- Someone is alerted when the station goes quiet, and when it recovers (#35)
- The backup has been restored once on purpose, and the steps are written down (#36)
- Dispatch logic lives only on the server (#34), so UI changes stay cheap
- A retention policy for audio, transcripts and activity (#46)
- Unit status suggestions from the radio: suggest, a person confirms (#37)
- Optional: a second station for redundancy (#42)

### 3. A department tool

Needs decisions that aren't technical (#52).

- Run cards, subtypes and unit facts signed off as PCFD policy (#50)
- Radio facts confirmed: Rye Brook tones (#47), radio IDs per rig (#49), PTT-ID at keypress (#48)
- Ownership of the accounts and the cost once past the ~$0 test, and who administers it
- **Transmit** over a real PCFD radio (#41), with FCC coverage, department approval and the hardware.
  A person presses every transmission, with a hard key-time limit.
- Station alerting (#45)

### 4. Connected

- Responder status and location from IamResponding or Red Alert, or our own iPad app (#43)
- Pre-plans and hydrants on the event map (#44)
- More 60 Control call detail from the two notification websites, only with permission or a proper
  feed (#51)
- Reports and exports the department needs (NFIRS, call history)

## Decisions still open

These shape phases 3 and 4. Tracked in #52.

1. **What sizeup is to the department:** a trial tool, a supplement to county dispatch, or PCFD's
   self-dispatch of record. This sets how much policy, sign-off and liability work follows.
2. **Who owns it and pays:** the AWS, Cloudflare and GitHub accounts, and the cost after the test.
3. **Who uses it:** who administers, who dispatches, who only views, and how new people get access.
4. **How feedback comes in:** chat today. Later, perhaps a feedback button that files an issue.

## Rules that don't change

- Nothing fake on screen. Unknown is shown as unknown (e.g. "FG1 · ID on release"), never guessed.
- No autonomous transmit. A signed-in person presses every transmission, after a preview.
- The backend doesn't depend on the UI: business logic sits on the server behind the API.
- The station only calls out; nothing reaches into the firehouse network.
- Never publish to Tapout's `pcfd-phone-prealert` topic (reading is fine).
- Secrets stay in SSM, the Keychain or root-only files, never in the repo or logs.
- Internal department documents (the Mutual Aid Plan, the ECC sheet's test caller data) are never
  published.
