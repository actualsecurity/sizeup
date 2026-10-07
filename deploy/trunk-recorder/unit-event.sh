#!/bin/sh
# trunk-recorder unitScript (optional): tells sizeup who keyed up the moment the control channel
# grants the call, instead of when the call ends. trunk-recorder runs this for every unit event as
#   unit-event.sh <shortName> <radioID> <on|off|join|call|ackresp|data|ans_req|location> [talkgroup] [patches]
# and its stdout is trunk-recorder's, which sizeup reads (sizeup/radio/live.py). Only grants print.
# Enable: add  "unitScript": "./unit-event.sh"  to the system in config.json, then restart sizeup.
[ "$3" = "call" ] && echo "SIZEUP-UNIT $1 $2 call $4"
exit 0
