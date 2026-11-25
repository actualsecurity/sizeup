# Moved: deploy/stt-worker

The GPU STT worker lives in [`deploy/stt-worker`](../../deploy/stt-worker/README.md) now. Install, configure and
change it there.

`worker.py` here is a **compatibility copy**, byte for byte the same as `deploy/stt-worker/worker.py`
(`tests/test_stt_worker_move.py` fails when they differ). A worker installed before the move knows only this path
for its self-update: the hub names a commit and the SHA-256 of `deploy/stt-worker/worker.py`, the worker downloads
`tools/stt_worker/worker.py` at that commit, finds the same hash and switches. The version it switches to downloads
from `deploy/stt-worker/worker.py` (and from here only for a commit from before the move).

Never edit this copy by hand: change `deploy/stt-worker/worker.py`, then

```sh
cp deploy/stt-worker/worker.py tools/stt_worker/worker.py
```

It goes once Settings > Station > Health shows every GPU worker on a version from the move on (the follow-up issue
linked from the pull request that moved it). After that, a worker installed from an installer pinned before the move
can't update itself: install it again from the README's Quick install.
