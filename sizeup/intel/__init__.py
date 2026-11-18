"""Radio intelligence: SIGINT-style pre-alerts from radio transcripts (#411). Rule packs (data/intel/*.json, Settings over
them) read police / fire chatter, a window per source merges a call-out and its read-back, the gazetteer places it, and
the policy pushes it on the INTEL kind (sizeup/notify.py). See engine.py for the pipeline and its replay interface."""


def push_sources() -> tuple[frozenset[str], int]:
    """The source keys intel could push from right now and their revision, a content hash of the set (engine.push_rev:
    the same set, the same rev, after a restart too; speech-to-text's priority hook: engine.py, "Push sources").
    (frozenset(), 0) when no engine runs here (a station: the hub's)."""
    from sizeup.intel import engine
    return engine.push_sources()


def is_push_source(channel_key: str, kind: str = "") -> bool:
    """A speech-to-text channel key ("tg:307", "p25:mrrs:9211", a bare "FG1" / "HPN Tower"; kind: the channel's kind,
    "airband" for an airband label) is one intel could push from now. Normalised as rows are keyed (engine.source_key):
    call this, never `key in push_sources()[0]` with the raw key. False when no engine runs here."""
    from sizeup.intel import engine
    return engine.is_push_source(channel_key, kind)
