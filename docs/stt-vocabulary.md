# Speech-to-text vocabularies

Amazon Transcribe custom vocabularies and whisper prompts, per kind of channel. The engines, the budget and the fallback
are in [cloud.md](cloud.md) ("Speech-to-text per channel"). This page covers the words.

Code: `sizeup/transcribe.py` (`build_vocabulary`, `to_aws`, `respell`, `Vocabulary`), `sizeup/stt_vocab.py` (the county
and police lists, the profiles), `sizeup/channels.py` (`vocabulary_suggestions`), `sizeup/atc.py` and
`sizeup/atc_vocab.py` (ATC), `sizeup/app.py` (the wiring).

## The three vocabularies

| AWS name | Used on | Phrases from | Size as sent |
|---|---|---|---|
| `sizeup-pcfd` (`[stt] aws_vocabulary`) | Our channels: South 2, FG1, FG2, the county P25 talkgroups | Settings > Radios > Vocabulary: the suggestion, or the list as edited | about 610 phrases, 10.5 KB (suggested) |
| `sizeup-atc` | Airband channels (HPN Tower, NY Approach, HPN Ground) | `data/atc/vocabulary.json`, plus the operators seen near HPN | 229 fixed phrases, plus up to 120 operator names |
| `sizeup-pd` | Police sources on another P25 system (`p25:mrrs:9211`) | `stt_vocab.police_phrases()` | about 950 phrases, 14 KB |

The three are kept apart. A police source never streams with `sizeup-pcfd`, and police respelling is never applied to
fire text, so a police "266" never becomes a fire callsign.

### Fire and county (`sizeup-pcfd`)

The suggestion is `channels.vocabulary_suggestions()` with `stt_vocab.county_phrases()` appended, in this order:

| Part | From | e.g. |
|---|---|---|
| Home words | fixed in `channels.py` | "60 Control", "Fire EMS 15", "Fireground 1", "alert level two", "carbon monoxide" |
| Status words | `channels.STATUS_WORDS`, and a few unit + status pairs (`STATUS_SAID_BY`) | "en route", "Engine 61 en route" |
| Our units | `data/cad_lists.json` units, as said | "Engine 60", "Tower Ladder 2", "23 98" |
| Home streets | the gazetteer's 60 busiest in Port Chester and Rye Brook, as said | "North Main Street" |
| Phrasing | `stt_vocab.PHRASING` | "to 60 Control", "all units back in service" |
| Channels | battalions 10-19, ground ops 1-8 | "Fire EMS 18", "Battalion 19", "Fire 3" |
| Places | `westchester_departments.csv`, `westchester_ems.csv`, `cad_lists.json` town aliases | "Town of Mamaroneck", "New Rochelle", "Byram" |
| Chief cars | department number + rank 1-5 (the county's numbering); the home departments' from `cad_lists.json` | "2301", said "twenty-three oh one" |
| Apparatus | `westchester_apparatus.csv`, no RESERVED numbers, no antiques | "Engine 34", "Ladder 50", "S.S.U. 15" |
| County streets | the gazetteer's 40 busiest overall | "Boston Post Road" |

Callsigns (chief cars and apparatus) come only from `SUGGEST_BATTALIONS` (15, 18, 19: the Sound Shore and its
neighbours). Every department's name, the channels and the phrasing are in whichever battalions are chosen. The whole
county would be about 1,040 phrases and 19 KB before the home streets; the measurement below found no gain from it.

### ATC (`sizeup-atc`)

- The fixed list and whisper's base prompt are in `data/atc/vocabulary.json`.
- `atc_vocab.Refresher` adds the operators whose aircraft were near HPN in the last 48 h, by their telephony name. Flight
  numbers are not added.
- It looks every 3 h and pushes only when the list's signature changed. Only the hub's lease holder does it.
- While the new list is PENDING, new airband streams open without a vocabulary. Open streams keep the one they have.
- Switch: Settings > System > ATC talkers, "Refresh the ATC vocabulary from the traffic" (on by default).

### Police (`sizeup-pd`)

`stt_vocab.police_phrases()`, in priority order:

1. the headquarters' callsigns (`data/pd_units.json`, role `headquarters`);
2. 10-codes, written "10-4" and sent as "ten-four";
3. generic police phrasing;
4. the towns (Port Chester, Rye, Rye Brook) and nearby places;
5. every street of those towns from the gazetteer, as said;
6. public landmarks only (schools, parks, town halls and similar). No firehouse, and no place named for a person's home.

Nothing in it names a person. Which source uses it is set in `data/stt/profiles.json` (a source key, else a service,
names a profile; the profile names the vocabulary).

## How a phrase is built

`build_vocabulary(phrases)` returns `{phrases, skipped, respell}`. Each phrase goes through `to_aws()`:

| Step | Rule | e.g. |
|---|---|---|
| Say it | `spoken()`: ordinals and digits spelled the way they are said; `&` and `/` said | "Engine 60" → "Engine sixty"; "2398" → "twenty three ninety eight"; "3rd" → "third" |
| Clean it | Letters, apostrophes and periods only. Letter-by-letter acronyms keep their periods | "S.S.U. 15" → "S.S.U. fifteen" |
| Join it | Words joined with hyphens: AWS's list form has no spaces | "Boston-Post-Road", "Engine-sixty", "ten-four" |

A phrase with digits also gets a respell entry ("engine sixty" → "Engine 60"). `respell()` applies the entries to each
AWS result, longest first, matching the words with spaces or hyphens between them.

Not respelled (`NO_RESPELL`): "Fire N", "Battalion N", "to N", and 4-digit numbers that are also years. They are sent to
AWS but never written back as digits, so "working fire two story" stays as said.

`stt_vocab.table()` writes the same list in AWS's table form (Phrase, IPA, SoundsLike, DisplayAs). A table vocabulary
has to be a file in S3, and in the measurement it gave the same words as the list, so it isn't pushed.

## Limits

| Limit | Value | Where |
|---|---|---|
| Phrases per vocabulary | 1,500 | `transcribe.MAX_PHRASES`; Settings refuses a longer list |
| Vocabulary size | 50 KB (AWS) | `transcribe.MAX_BYTES`; Settings refuses a larger list |
| One phrase as sent | 256 characters | `transcribe.MAX_PHRASE` |
| One phrase in Settings | 100 characters | `settings.MAX_PHRASE_CHARS` |
| Characters | letters, apostrophes, periods; no digits | `to_aws()` |

A phrase over a limit is listed under `skipped` with the reason. Duplicates after conversion are dropped silently.

## Hyphens in transcripts

AWS writes a list-form phrase back with its hyphens ("Boston-Post-Road"); the hub writes it back as written. See
[Hyphens](#hyphens) below (issue #518, PR #519).

## Pushing to AWS

Nothing is pushed by a deploy. Settings > Radios > Vocabulary:

1. The list shown is the suggestion until someone edits it. An edited list stays as it is; Reset returns to the
   suggestion (and picks up a new suggestion after a release).
2. The page shows the phrases as AWS will get them and the ones skipped (`GET /api/cad/stt`).
3. **Push** (`POST /api/cad/settings/stt`, action `push`) sends all three vocabularies:
   - `sizeup-pcfd`: the list on the page;
   - `sizeup-atc`: the fixed list plus the operators seen, when an airband channel is configured;
   - `sizeup-pd`: `police_phrases()`, when a profile names it.
4. Each is an `UpdateVocabulary` (or `CreateVocabulary` the first time), then `GetVocabulary` every 10 s until READY or
   FAILED. The state shows on the page. The push is in the Settings audit log, with the phrase count and no words.
5. New streams use a vocabulary once it is READY. A stream that is already open keeps what it opened with until it
   ends (`[stt] aws_idle_s` after its last transmission).

A push only works where Amazon Transcribe is set up (`[stt] aws = true`: the hub, or all-in-one with credentials).

## Which channel uses which vocabulary

`AwsTranscriber.url()` picks the vocabulary when a channel's stream opens:

| Channel | Vocabulary | Respelling |
|---|---|---|
| Airband (`kind = "airband"`) | `sizeup-atc` once READY; none while it is PENDING | `sizeup-pcfd`'s map, then ATC's own written form (`sizeup/atc_normalize.py`) |
| A source whose profile names a vocabulary (`p25:mrrs:9211`) | `sizeup-pd` once READY; none while PENDING | `sizeup-pd`'s own map |
| Any other source on another P25 system (`p25:...`) | none | none |
| Everything else (South 2, FG1, FG2, `tg:<n>`) | `sizeup-pcfd` once READY | `sizeup-pcfd`'s map |

Whether a channel goes to AWS at all is its engine in Settings > Radios (`local`, `aws` or `both`; defaults from
`[stt] aws_channels`).

## Whisper prompts

whisper takes a text prompt per clip, not a vocabulary. `App._prompt()` (`sizeup/app.py`) picks it:

| Source | Prompt |
|---|---|
| Airband | The base prompt from `data/atc/vocabulary.json`, then the spoken callsigns of the aircraft likely on that frequency (arriving, departing and on the ground first), up to 760 characters |
| A source with a profile (police) | The profile's phrasing, the headquarters' callsigns, the towns, and the towns' busiest streets (`prompt_streets`, 30). Never a talker's name |
| Everything else | `[stt] prompt` (empty by default), plus the talkers on the call when `[stt] talker_prompt` is `ids` or `ids+time` |

### Hyphens

AWS writes a list-form phrase back the way it was sent, hyphens included ("Example-Post-Road"). `respell()` only
ever mapped the phrases with digits, so once streets, towns and departments were in the list their names were
stored with the hyphens. The hub now writes every phrase of the lists back as written ("Example Post Road"):

- **On arrival** (`transcribe.Display`, `AwsTranscriber._done`): each result, a late answer kept as the
  alternative included, after `respell()`. The map is built from the lists as they are (the edited list, the
  suggestions, the police sources' own), so it works on the vocabulary AWS already has: nothing is sent to AWS.
- **Only known phrases.** A whole hyphenated token that is a phrase of the lists, in any case, with a possessive or
  plural ending kept. "twenty-three", "T-bone" and a name the list itself writes with hyphens stay as they are.
- **Stored transcripts are shown the same way when read**, never rewritten: the Radio log's rows, an event's
  remarks and timeline, and the monitor's pages go through the same map on their way out. No migration, nothing
  at start. The Radio log's search treats a hyphen and a space the same, so the old rows are found either way.

Not done: pushing the table form, whose DisplayAs would make AWS write the spaces itself. It needs the vocabulary
as a file in S3 (`VocabularyFileUri`: a bucket the task may write and Transcribe may read), where the push today
sends `Phrases` inline and needs neither. The hub's own mapping is the fix either way.

## Measurement (2026-11)

The fire and county list only. 146 real transmissions from 60 Control, Fire/EMS 10-19 and FG1, scored against a
hand-corrected reference. Batch Transcribe, one concatenated file per run (1 s of silence between transmissions), split
back by word timestamps. Production streams instead, so the numbers can differ slightly from what the hub gets. Callsign
and status recall are the share of the reference's unit names and status phrases that the transcript has. "Silent" is
how many of the 26 transmissions with no speech got text anyway.

| Run | WER | Key-token recall | Callsigns | Statuses | Silent |
|---|---|---|---|---|---|
| (a) no vocabulary | 35.4% | 56.6% | 43% | 59% | 5/26 |
| (b) the earlier `sizeup-pcfd` (190 phrases) | 34.1% | 60.2% | 52% | 59% | 7/26 |
| (c) whole county (1086 phrases) | 33.3% | 60.7% | 49% | 66% | 7/26 |
| (c) suggested battalions 15/18/19 (612 phrases) | 32.7% | 62.7% | 51% | 67% | 7/26 |
| (c) whole county, table form | 33.3% | 60.7% | 49% | 66% | 7/26 |
| (d) whole county, 16 kHz (wideband) | 32.5% | 62.3% | 48% | 70% | 7/26 |

The 95% interval on WER is about ±5 points, so these runs overlap. For comparison, local Whisper large-v3 with a domain
and talker prompt got 25.5% WER, 72.5% key-token recall, 67% callsigns and 76% statuses.

- A vocabulary helps only a little. Statuses improve the most (59% to 67%) because of the added phrasing. Callsigns stay
  around 50%: Transcribe runs a callsign and the numbers after it together (a chief car followed by "to 60" comes out as
  one long number) whatever the vocabulary says.
- The smaller list does at least as well as the whole county's, so Settings suggests the smaller one.
- The table form (SoundsLike, DisplayAs) gives the same words as the list.
- 8 kHz audio is already Transcribe's telephony model. Upsampling to 16 kHz made no reliable difference, so the stream
  stays at 8 kHz.

The ATC and police vocabularies have no measurement of this kind here. A general evaluation tool (`tools/stt_eval.py`)
is in open PR #491.
