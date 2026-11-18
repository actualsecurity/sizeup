"""Radio intelligence: SIGINT-style pre-alerts from radio transcripts (#411). Rule packs (data/intel/*.json, Settings over
them) read police / fire chatter, a window per source merges a call-out and its read-back, the gazetteer places it, and
the policy pushes it on the INTEL kind (sizeup/notify.py). See engine.py for the pipeline and its replay interface."""


def push_sources() -> tuple[frozenset[str], int]:
    """The source keys intel could push from right now and a revision bumped when that set changes (speech-to-text's
    priority hook: engine.py, "Push sources"). (frozenset(), 0) when no engine runs here (a station: the hub's)."""
    from sizeup.intel import engine
    return engine.push_sources()
