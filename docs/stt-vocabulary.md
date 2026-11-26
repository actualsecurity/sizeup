# Speech-to-text vocabularies

Amazon Transcribe custom vocabularies and whisper prompts, per kind of channel. The engines, the budget and the fallback
are in [cloud.md](cloud.md) ("Speech-to-text per channel"). This page covers the words.

Code: `sizeup/transcribe.py` (`build_vocabulary`, `to_aws`, `respell`, `Vocabulary`), `sizeup/stt_vocab.py` (the county
and police lists, the profiles), `sizeup/stt_terms.py` (the private term lists), `sizeup/channels.py` (`vocabulary_suggestions`), `sizeup/atc.py` and
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

## Private term lists (per kind of source)

The STT plan's step 3a: lists of words to bias speech-to-text toward, one per kind of radio source. They are built
from data that never goes in this public repo, so the repo holds only the code that reads them (`sizeup/stt_terms.py`)
and made-up lists in its tests.

| List | Used on |
|---|---|
| `county_fire` | the county's Fire/EMS dispatch and paging: South 2, Fire/EMS 15 (`tg:307`), FG1, FG2, our fire talkgroups |
| `pd_dispatch` | police dispatch: MRRS 9211 and 9212, the county's police talkgroups |
| `ems` | EMS: MRRS 9570, the county's EMS talkgroups |
| `airband` | HPN Tower, Approach and Ground |
| `places` | loaded with every one of them |

Which list a source takes: `stt_terms.own_for()`, always by its system-qualified key.

1. Airband and paging channels by their audio.
2. An admin's service choice for the source (Settings > Radios), which wins over everything below.
3. Its key (`BY_KEY`: 9211, 9212, 9570).
4. Its service (fire, police, ems, air).

A transit or "other" talkgroup takes none.

### Where they live

The hub reads them from its backups bucket, where only its task role and the admin can read them:

```
s3://<backups bucket>/sizeup-hub/vocab/county_fire.txt
s3://<backups bucket>/sizeup-hub/vocab/pd_dispatch.txt
s3://<backups bucket>/sizeup-hub/vocab/ems.txt
s3://<backups bucket>/sizeup-hub/vocab/airband.txt
s3://<backups bucket>/sizeup-hub/vocab/places.txt
```

The bucket and prefix are the hub's Litestream ones (`LITESTREAM_BUCKET`, `LITESTREAM_PATH`). The task role's
`backups` policy already lets it read every key there, so nothing in IAM changes. To upload or replace them, from a
checkout of the private repo's term lists:

```sh
aws s3 cp <checkout>/vocab/terms/ s3://<backups bucket>/sizeup-hub/vocab/ --recursive \
    --exclude '*' --include '*.txt'
```

The hub looks at them at start and every 5 minutes (a conditional GET: an unchanged file costs a 304).

- A change is used once a read a minute later finds the same files, so an upload of all five is used together.
- A file deleted takes its list away (and its AWS vocabularies).
- A file that can't be read (S3 error, not UTF-8) keeps what was read before, and the hub's log says why.
- No terms are ever logged, and no AWS error text (it can quote a phrase): only counts, versions and error codes.

All-in-one (dev) reads them from a folder instead: `[stt] terms_dir`, holding `<name>.txt` or `terms/<name>.txt`.
A station never reads them. The hub sends it the top of each list (`set_stt_terms`) once its first read is over,
kept in memory only.

### Format

UTF-8, one term per line, priority order (a smaller budget takes the top). Lines starting with `#` are comments;
blank lines are skipped. Terms are compared ignoring case, hyphens and punctuation, and repeats are dropped. Numbers
are spelled as said (`transcribe.spoken()`); both engines get them back in digits (`stt_terms.written()`: "Engine one
forty two" is "Engine 142", "ten thirty two" is "10-32").

### Versions

A list's set is its terms with `places` woven in: about a third of every stretch of the top is places, so a small
budget takes both. Its version is the sha256 of its terms as parsed, 12 hex digits. A comment or blank line changes
nothing; any term does.

Every transcript records what biased it, in the radio log's `vocab_v` column:

| `vocab_v` | Means |
|---|---|
| `county_fire@0123456789ab` | whisper's prompt had that list at that version |
| `county_fire+pcfd@0123456789ab` | AWS: the term vocabulary of that list on `sizeup-pcfd`, at that version |
| `sizeup-pcfd@0123456789ab` | AWS: a vocabulary without a term list, at its phrases' signature |
| empty | no list (switched off, none for the source, no room in the prompt) or not known |

`stt_runs` (#651) keeps it too, for every engine (the GPU workers, AWS, the station's whisper), next to `prompt_v`,
the hash of the whole prompt (terms included). The GPU workers' text goes through the same no-speech rules as the
station's whisper (`stt.judge(raw, seconds, low_energy, prompt, vocab_v)`), the read-back guard included when a list
went in the prompt.

### Switches

`[stt]` in the config. Off unless set; the hub's (`deploy/hub/config.hub.toml`) sets both, and with no lists uploaded
nothing changes. To turn one off, set it to `false` there (a deploy). `terms_aws` off deletes the term vocabularies
made before, once the task leads.

| Key | Does |
|---|---|
| `terms_whisper` | whisper's prompt gets the top of the source's list (the hub sends it to the station) |
| `terms_aws` | an Amazon Transcribe vocabulary per list and own vocabulary, used on those channels |
| `terms_dir` | all-in-one: the folder to read them from (the hub uses its bucket) |

### whisper

`App._prompt()` puts the list's top terms in front of the prompt the source already had (its configured text and
talkers, the police profile, the ATC prompt), within whisper's prompt budget. whisper keeps at most 224 tokens of a
prompt, its end. `stt_terms.est_tokens()` overcounts: measured against whisper's own tokenizer on prompts filled from
every list, the real count was at most 0.97 of the estimate. The budget is 210 by that estimate. The terms go most
important last (nearest the audio), and the source's own prompt after them.

- **No term with a number** (`MAX_NUMERIC_PROMPT_TERMS`). Spelled out, whisper wrote every number as words, and the
  dispatch parser found no unit or house number. Even in digits, a prompt of callsigns pulled heard numbers toward
  them. The talkers on the call still go in.
- A police profile with a list keeps its towns' 12 busiest streets (`PROFILE_STREETS`): the addresses that matter.
  Its 30 alone filled most of the budget.
- The ATC prompt is unchanged. With aircraft near HPN it fills the budget, so the airband list mostly goes to AWS.

whisper can read its prompt back on dead air. `stt.prompt_noise()` checks for it, only when a list went in the prompt
(the item's `vocab_v`). The text is no speech when it is:

- mostly a run of three or more of the prompt's items in the prompt's order, on a clip under 1.5 s or faster than
  4.5 words a second; or six or more of them, at any pace;
- nothing but three or more of the prompt's items on a clip under 1.5 s (two can be a real hail: a unit and a town);
- only a number of the prompt or a piece of it (the talker's ID) on a clip with no voice energy;
- one of `PROMPT_HALLUCINATIONS` ("we'll be right back") on a short or quiet clip.

Voice energy alone never drops anything: P25 has none live, and a re-run's is unreliable. The older rules
(`NOISE_LABEL`, `HALLUCINATIONS`, `SHORT_S`) still apply.

#### Measured (2026-11)

The county_fire list, 146 real county clips (60 Control, Fire/EMS 10-19, FG1; 112 with speech, 34 without), each with
its talker in the prompt as the station has it. Run on local whisper-servers: large-v3-turbo (the worker pool's) and
medium.en (nearest the station's small.en). "Digits" counts the transcripts with a digit, of the 81 whose reference
has one. "From prompt" counts numbers in the text that the prompt had and the reference doesn't. "No-speech text" is
how many of the 34 got text after every rule. "Speech dropped" is clips with speech that the new guard emptied.

| Prompt | Model | WER | Digits | From prompt | No-speech text | Speech dropped |
|---|---|---|---|---|---|---|
| talker only (before) | turbo | 30.6% | 71 | 2 | 17 | - |
| list spelled out (first version) | turbo | 29.7% | 56 | 3 | 20 | 0 |
| list in digits, every numeric term | turbo | 30.4% | 73 | 8 | 15 | 0 |
| list in digits, 8 numeric terms | turbo | 31.0% | 72 | 5 | 17 | 0 |
| **list in digits, no numeric term** | turbo | 29.4% | 74 | 3 | 17 (24 without the guard) | 0 |
| talker only (before) | medium.en | 33.2% | 67 | 2 | 11 | - |
| list spelled out (first version) | medium.en | 30.3% | 47 | 3 | 13 | 0 |
| list in digits, every numeric term | medium.en | 30.1% | 77 | 8 | 11 | 0 |
| list in digits, 8 numeric terms | medium.en | 32.9% | 73 | 3 | 11 | 0 |
| **list in digits, no numeric term** | medium.en | 35.3% | 68 | 2 | 12 | 0 |

The interval on WER is about ±5 points, so the list's effect on WER is within the noise either way. What is clear:

- A spelled-out list costs digits (71 to 56 on turbo, 67 to 47 on medium.en). Digits fix it.
- Numeric terms make whisper use numbers that weren't said.
- The guard brings turbo's no-speech text back to where it was with no list, and drops no speech.

### Amazon Transcribe

A stream takes one vocabulary, so a term vocabulary keeps the channel's own phrases. With `terms_aws`, the hub's
leader makes one per list and own vocabulary, the first time a channel of that pair streams:

| Channels | Own phrases first | Then |
|---|---|---|
| South 2, FG1, FG2, `tg:307`, our talkgroups | `sizeup-pcfd` as Settings > Radios > Vocabulary has it (edits included) | `county_fire` |
| HPN Tower / Approach / Ground | `sizeup-atc` as AWS last got it, operators seen near HPN included (#257) | `airband` |
| `p25:mrrs:9211` | `sizeup-pd` (`police_phrases()`) | `pd_dispatch` |
| another system's source with no vocabulary (`p25:mrrs:9570`) | none | its list |

- The name says the list, the own vocabulary and the version: `sizeup-terms-county_fire-pcfd-0123456789ab`.
- It holds phrases up to AWS's limits (1,500 phrases, 50 KB, 256 characters a phrase, letters only:
  `build_vocabulary()`). The list's terms are written in digits, so `build_vocabulary` spells them for AWS and keeps
  the respelling back to digits, as for the own phrases.
- It is made again when anything in it changes: the list, an admin's edit, a new push of `sizeup-atc`.
- An admin's edit to Settings > Radios > Vocabulary reaches the term vocabularies at the next read of the lists
  (within about 5 minutes), plus the time AWS takes to build them. It doesn't wait for Push: Push only sends
  `sizeup-pcfd` itself, which the channels use until their term vocabulary is READY.
- The airband one follows `sizeup-atc` as AWS last got it, so it changes only when that vocabulary does, not with
  every operator seen or gone.
- Until it is READY, the channel keeps its own vocabulary. While the next version is PENDING, the one before stays in
  use. If AWS refuses it, the channel keeps its own; a FAILED one is tried again after 1 h, 6 h, then every 24 h.
- A READY one is checked once an hour and made again if it was deleted.
- The last three per pair are kept; older ones are deleted.
- Respelling: the channel's own as before, then the term vocabulary's (its list's numbers back to digits). AWS's
  hyphens come off the list's phrases like any other's ([Hyphens](#hyphens)).

The task role's `transcribe` policy (sizeup-aws step 12) covers `vocabulary/sizeup-*`: Create, Update, Get and
Delete. Settings > Radios > Vocabulary's state (`GET /api/cad/stt`) has each pair's under `terms`.

## How a phrase is built

`build_vocabulary(phrases)` returns `{phrases, skipped, respell}`. Each phrase goes through `to_aws()`:

| Step | Rule | e.g. |
|---|---|---|
| Say it | `spoken()`: ordinals and digits spelled the way they are said; `&` and `/` said | "Engine 60" → "Engine sixty"; "2398" → "twenty three ninety eight"; "3rd" → "third" |
| Clean it | Letters, apostrophes and periods only. Letter-by-letter acronyms keep their periods | "S.S.U. 15" → "S.S.U. fifteen" |
| Join it | Words joined with hyphens: AWS's list form has no spaces | "Example-Post-Road", "Engine-sixty", "ten-four" |

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

AWS writes a list-form phrase back with its hyphens ("Example-Post-Road"); the hub writes it back as written. See
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

A push only works where Amazon Transcribe is set up (`[stt] aws = true`: the hub, or all-in-one with credentials and
`SIZEUP_ALLOW_OUTBOUND=1`, `sizeup/outbound.py`).

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
is on main.

## Post-ASR correction (#879)

Deterministic, for every engine (the GPU workers, AWS, the station's whisper), on the hub as each transcript lands:
`sizeup/stt_correct.py`, its sound keys from `sizeup/dmetaphone.py` (Double Metaphone, standard library only).

| Rule | Does | e.g. |
|---|---|---|
| `unit` | spoken numbers in a unit ID or call sign of the list, folded to digits; the whole number run only | "Engine ninety seven" → "Engine 97", "ninety Control" → "90 Control" |
| `ten` | a ten-code of the list said in words, at a clause end | "ten four." → "10-4." |
| `spell`, `sound` | 1-4 words matched to a name of the places list (a street, town, landmark), by spelling and Double Metaphone | "Samplewerth Street" → "Sampleworth Street" |

Safety:

- Only the channel's own list (`stt_terms.own_for_channel`), never another channel's. Names come from the shared places
  list only; the channel's own list adds unit IDs, call signs and ten-codes. "Fire N" and "Battalion N" are never folded.
- A street listed under `# streets: <town>` is a target only for the towns in `[stt] correct_towns` (default Port
  Chester, Rye Brook, Rye). Every other term of the list is still a rival.
- A correction needs a score of 0.86 (0.9 for one word), no rival within 0.08, the suffix heard for a street, each
  word alike when the word counts match, the same first letter unless spelled nearly the same, and no word added.
- A name needs a word the engine capitalised that isn't a sentence's first ("lyrro park is closed." is left alone).
  One heard word is never a unit's prefix, a common word ("a Marine veteran") or one with a number after it
  ("Marine 1"). A heard word ending in an "s" the name's word hasn't is never that name ("Brenlows Park" is not
  "Brenlow Park"). A possessive is kept: on the last word it is put back after the name; anywhere else, no correction.
- A unit's prefix (a call sign's word after the number) must be capitalised by the engine, unless another unit ID or
  call sign of the list is in the same text; never with a measure after the number ("a ladder thirty one feet up",
  "car accident").
- Never the trailing time (`stt_time.readings`: that is #727's) and never a digit. Already a term: left alone.

Kept, never overwritten:

- The radio log row (`radio_traffic`): `corr_text`, `corr_rules` and `corr_v`, written with the text of record before
  the row commits (`App._transcribed_one`, `App._retext`). The raw `text` stays the engine's. Everything that reads the
  row reads `COALESCE(NULLIF(corr_text, ''), text)`: the status parser, the Radio rows (with the status words put back
  after it, `statushints.decorate`; the words as transcribed and each `“heard” → to` on hover), the search index, the
  Radio filter, the context bundle and the #491 export's `fixed_text` (its `asr` stays the engines' own). The intel
  engine reads the row by its own query (sizeup/intel): it takes the same expression there.
- A row keeps the correction it was stored with: a new term list never rewrites it (so the search index never goes
  stale with a list change), and a re-run's new text gets its own.
- `stt_runs` (#651), each engine's run: `corr_text` (only when it changes the text), `corr_n` (0: it ran and found none;
  NULL: not on, or not built yet), `corr_rules` (`[[heard, to, rule, score], ...]`, cut by whole corrections) and
  `corr_v`. Not on a standby.
- `corr_v`: `vcorr/1@<list>@<version>@<sections and towns>` (a header changed alone is a new version); `pending`: on
  there, but the list's matcher wasn't built yet, so the raw text was kept; empty: off there.

The matchers are built only on the term lists' thread after a read (`Corrector.warm`), one at a time behind a lock,
never on a transcript's thread; the one before is used until the new one is ready.

Switch: `[stt] correct_channels` (default the county Fire/EMS dispatch talkgroups, `stt_time.DEFAULT_CHANNELS`; `[]`
off). It needs the term lists (`terms_whisper` or `terms_aws` on). Other channels stay off until measured.

Measure it offline with `tools/stt_correct_eval.py`: identifier recall (the list's names, unit IDs, call signs and
ten-codes as written) and WER, before and after, against the admins' corrections (the #491 export or a restored
cad.sqlite), plus a check that every trailing-time result is unchanged. Each clip's list is picked by the hub's own rule
(`--config` the hub's config; else the row's group and service). `--unlabelled N` needs no references: how often it
corrects, by rule, and the trailing-time check. It prints aggregates only.

Measured 2026-11-25 on a restored copy of the hub's radio log (every county Fire/EMS dispatch transcript kept, 117,049
clips; AWS and whisper), the first version and then with the review's rules:

| | clips changed | corrections | `unit` | `sound` | `spell` |
|---|---|---|---|---|---|
| first version | 263 (0.22%) | 273 | 75 | 153 | 45 |
| with the review's rules | 172 (0.15%) | 178 | 41 | 97 | 40 |

- Of the 95 corrections no longer made: 59 names with no word the engine capitalised, 2 a trailing "s", 23 unit IDs
  with a lower-case prefix and no other unit in the text, 11 other unit folds. No new correction was made.
- The trailing-time check was the same before and after on every clip.
- A hand review of the first version's name corrections found about 96% right. The rest were a real place missing from
  the list, corrected to the nearest one listed.
- Identifier recall against references can't be measured yet: there is only one admin correction so far. Rerun the
  tool once the #491 corrections build up.
