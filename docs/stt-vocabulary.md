# Amazon Transcribe custom vocabulary (county-wide)

60 Control and the Fire/EMS channels carry every department in the county, but the vocabulary sizeup suggested had
only Port Chester's and Rye Brook's units and streets. `sizeup/stt_vocab.py` adds the rest, generated from public
files in the repo:

| part | from | e.g. |
|---|---|---|
| phrasing | statuses as units say them | "to 60 Control", "all units back in service" |
| channels | battalions 10-19, ground ops | "Fire EMS 18", "Battalion 19", "Fire 3" |
| places | `westchester_departments.csv`, `westchester_ems.csv`, `cad_lists.json` town aliases | "Town of Mamaroneck", "New Rochelle", "Port Chester EMS", "Byram" |
| chief cars | dept number + rank 1-5 (the county's numbering); home departments from `cad_lists.json` | "2301" said "twenty-three oh one" |
| apparatus | `westchester_apparatus.csv` (no RESERVED numbers, no antiques) | "Engine 34", "Ladder 50", "S.S.U. 15" |
| streets | the gazetteer's busiest, after the home towns' 60 | "Boston Post Road" |

Settings suggests the callsigns of `SUGGEST_BATTALIONS` (15, 18, 19: the Sound Shore and its neighbours); every
department's name, the channels and the phrasing are in either way. That is about 610 phrases and 11 KB, under the
caps (`transcribe.MAX_PHRASES` = 1500 and AWS's 50 KB, checked by `build_vocabulary`).

## Pushing it

Nothing is pushed automatically. **Settings > Radios > Vocabulary** shows the suggestion when the list hasn't been
edited; an edited list stays as it is (reset it to get the new suggestion). **Push** sends it as `sizeup-pcfd`.

The push uses AWS's list form: digits are spelled out the way they are said ("twenty-three-oh-one") and
`respell()` writes them back ("2301") in the text. "Fire N", "Battalion N", "to 60" and 4-digit numbers that are
also years are sent but never written back (`transcribe.NO_RESPELL`), so "working fire two story" stays as said.
Settings refuses a list over 50 KB. `stt_vocab.table()` writes the same list in AWS's table form
(Phrase, IPA, SoundsLike, DisplayAs: DisplayAs does the respelling on AWS's side, SoundsLike covers names like
Mamaroneck and Greenwich). A table vocabulary has to be a file in S3, and in the measurement it gave the same
words as the list, so it isn't pushed.

## Measurement (2026-11)

146 real transmissions from 60 Control, Fire/EMS 10-19 and FG1, scored against a hand-corrected reference. Batch
Transcribe, one concatenated file per run (1 s of silence between transmissions), split back by word timestamps.
Production streams instead, so the numbers can differ slightly from what the hub gets. Callsign and status recall
are the share of the reference's unit names and status phrases that the transcript has. "Silent" is how many of
the 26 transmissions with no speech got text anyway.

| run | WER | key-token recall | callsigns | statuses | silent |
|---|---|---|---|---|---|
| (a) no vocabulary | 35.4% | 56.6% | 43% | 59% | 5/26 |
| (b) production `sizeup-pcfd` (190 phrases) | 34.1% | 60.2% | 52% | 59% | 7/26 |
| (c) whole county (1086 phrases) | 33.3% | 60.7% | 49% | 66% | 7/26 |
| (c) suggested battalions 15/18/19 (612 phrases) | 32.7% | 62.7% | 51% | 67% | 7/26 |
| (c) whole county, table form | 33.3% | 60.7% | 49% | 66% | 7/26 |
| (d) whole county, 16 kHz (wideband) | 32.5% | 62.3% | 48% | 70% | 7/26 |

The 95% interval on WER is about ±5 points, so these runs overlap. For comparison, local Whisper large-v3 with a
domain and talker prompt got 25.5% WER, 72.5% key-token recall, 67% callsigns and 76% statuses.

- A vocabulary helps only a little. Statuses improve the most (59% to 67%) because of the added phrasing. Callsigns
  stay around 50%: Transcribe runs a callsign and the numbers after it together (a chief car followed by "to 60"
  comes out as one long number) whatever the vocabulary says.
- The smaller list does at least as well as the whole county's, so Settings suggests the smaller one.
- The table form (SoundsLike, DisplayAs) gives the same words as the list.
- 8 kHz audio is already Transcribe's telephony model. Upsampling to 16 kHz made no reliable difference, so the
  stream stays at 8 kHz.
