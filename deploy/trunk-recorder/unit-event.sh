#!/bin/sh
# trunk-recorder unitScript: tells sizeup who keyed up the moment the control channel grants the
# call (or re-grants it to a new talker), instead of seconds later or when the call ends.
# trunk-recorder 5.2 runs this, in the background, for every unit event on the system as
#   unit-event.sh <shortName> <radioID> <on|off|join|call|ackresp|data|ans_req|location> [talkgroup] [patches]
# "call" = a voice grant, or a grant whose source differs from the call's current talker; on every
# talkgroup of the system, recorded or not. Its stdout is trunk-recorder's, which sizeup reads line by
# line (sizeup/radio/live.py): one short echo is one atomic write to that pipe, nothing is buffered.
# sizeup sets "unitScript" (absolute path) on each system in the config.runtime.json it generates;
# running trunk-recorder by hand, config.json's "./unit-event.sh" does the same. Only grants print.
[ "$3" = "call" ] && [ -n "$4" ] && echo "SIZEUP-UNIT $1 $2 call $4"
exit 0
