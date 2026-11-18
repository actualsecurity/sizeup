# ADR NNNN: <the decision, in one line>

Status: proposed | accepted | superseded by NNNN | rejected · Date: YYYY-MM-DD

## Context

What is slow, heavy or limiting today, with the numbers and where they came from (benchmark script, production
metric, log aggregate). Measured and estimated numbers are marked as such.

## Decision

What moves to native code, in which language, behind which interface; what stays in Python.

## Alternatives

What else would get most of the gain (usually: fix the Python), and why it is or isn't enough.

## Consequences

The gain expected (a number to check after the switch), the cost (build, deploy, people who can change it), the
risks (behaviour parity, two implementations during the shadow period) and how each is contained.

## Switch plan

Shadow run, what is compared, the flag, what to watch, the rollback. Nothing switches without the admin's go.
