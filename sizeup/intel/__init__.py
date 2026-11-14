"""Radio intelligence: SIGINT-style pre-alerts from radio transcripts (#411). Rule packs (data/intel/*.json, Settings over
them) read police / fire chatter, a window per source merges a call-out and its read-back, the gazetteer places it, and
the policy pushes it on the INTEL kind (sizeup/notify.py). See engine.py for the pipeline and its replay interface."""
