# GPU worker fetch latency (#678): WIP notes, to be removed before merge

Measurements (2026-11-25, read-only; counts and timings only):
- stt-src arrival after its event (metrics stt_in, since #701 deploy, ~43k samples):
  urgent n=2479 p50 2.00 s p90 2.73 p95 3.71; <=1 s 2.8%, <=2 s 50.6%
  high   n=6387 p50 2.02 s p90 2.98 p95 3.93; <=1 s 4.1%, <=2 s 43.1%
  page   n=561  p50 2.07 s p90 2.58 p95 3.20; <=1 s 37.1%, <=2 s 45.3%
  last 24 h same shape (urgent p50 2.02, high 2.02, page 2.40).
- So with the hub's flat 2.0 s cap: ~50-57% of pre-alert GPU jobs waited the full 2 s and still got listening-8k;
  the rest got stt-src at ~2 s. Fetch ~2.0 s = the wait, not transfer.
- Worker log (Logs Insights sample): a fetch with no wait = 62 ms; others 1827-2125 ms. Worker HTTP client already
  keeps one HTTPS connection alive (deploy/stt-worker/worker.py Hub): no worker.py change needed.
- stt_runs: 223 gpu-worker runs (urgent 8, high 14, low 199, context 2); run_s avg urgent 2.32 s, high 2.57 s.
- ROOT CAUSE: station.py _stt_forward waits on _stt_wake (2.0 s timeout) while an event is in the outbox, and nothing
  woke it when the events were acked -> every STT input went up at the lane's next 2 s look.

Changes:
- station.py: _forward_once sets _stt_wake after the ack (test: STT post follows the event <0.5 s; fails before: 1.14 s).
- sttpool.SRC_WAIT_S per class from queued_at (pre-alert 0.75 s, LIVE 1.5, LOW 3.0, RERUN/CATCHUP 0) + src_wait_s().
- sttworkers._audio uses it (AUDIO_WAIT_MAX_S 3.0 outer cap).
- hub._pool_audio_of records audio.input; _stt_ran stamps stt_runs.input (aws: listening-8k, station-whisper: stt-src).
- sttruns: input column (schema + ALTER for older files).

Left: run tests/test_stt_pool.py, test_stt_arrival.py, test_stt_input.py + full suite (pytest-slot -n 6); README
Logs Insights note for /sizeup/stt-worker (no doc suggests tail today); PR; #678 comment (AWS path items 1-3 remain).
