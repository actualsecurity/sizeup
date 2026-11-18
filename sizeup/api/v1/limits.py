"""Resource caps of /api/v1 (#619, docs/mobile-api.md section 8): all in memory, no thread of its own and no database,
so v1 load cannot starve the console on the hub's one small task (#446).

- a token bucket per principal: reads 10/s burst 30, writes 2/s burst 10 (the design); a request with no principal
  is limited per client address first, then by one shared bucket (so a spoofed address does not buy more, and one
  flooder cannot drain the shared bucket: it is only taken once its own address passes). `meta` has a shared bucket
  of its own, so a flood of bad-token requests cannot starve an app's launch. Address buckets live apart from the
  device buckets, so a flood of made-up addresses never evicts a device's;
- a cap on v1's concurrent requests, and on its open streams (none yet), apart from the console's;
- at most 2 concurrent streams per device.

The address is Cf-Connecting-Ip, which is only trustworthy when the origin is reachable only through Cloudflare (the
hub's tunnel); anywhere else a client can pick it, and only the shared buckets hold. Without Cloudflare in front, a
client that rotates the header can starve `meta` and the anonymous bucket, and one flooder with no header shares the
tunnel's single address bucket with every other such caller."""
from __future__ import annotations

import math
import threading
import time
from typing import Callable

from .switches import DEFAULT_MAX_REQUESTS, DEFAULT_MAX_STREAMS

READ_RATE, READ_BURST = 10.0, 30.0
WRITE_RATE, WRITE_BURST = 2.0, 10.0
ANON_RATE, ANON_BURST = 5.0, 15.0            # one client address with no principal
ALL_ANON_RATE, ALL_ANON_BURST = 50.0, 100.0  # every such client together
STREAMS_PER_DEVICE = 2
META_RATE, META_BURST = 50.0, 100.0           # `meta`, from every address together
SIGNIN_RATE, SIGNIN_BURST = 50.0, 100.0       # the sign-in's public routes (/auth/v1/*, /.well-known/): their own
MAX_KEYS = 4096                               # buckets kept (devices, and addresses, each): the memory bound
IDLE_FULL_S = 60                              # a bucket idle this long is full again: nothing is lost by dropping it


class Bucket:
    __slots__ = ("tokens", "at")

    def __init__(self, burst: float, now: float):
        self.tokens, self.at = burst, now


class Limits:
    def __init__(self, max_requests: int = DEFAULT_MAX_REQUESTS, max_streams: int = DEFAULT_MAX_STREAMS,
                 clock: Callable[[], float] = time.monotonic):
        self.max_requests, self.max_streams, self.clock = max_requests, max_streams, clock
        self._lock = threading.Lock()
        self._devices: dict[str, Bucket] = {}
        self._ips: dict[str, Bucket] = {}
        self._shared = {"anon": Bucket(ALL_ANON_BURST, clock()), "meta": Bucket(META_BURST, clock()),
                        "signin": Bucket(SIGNIN_BURST, clock())}
        self._busy = 0
        self._streams: dict[str, int] = {}

    def _take(self, buckets: dict, key: str, rate: float, burst: float, now: float) -> int:
        """0: taken. Else the seconds until a token is back. Caller holds the lock."""
        b = buckets.get(key)
        if b is None:
            if len(buckets) >= MAX_KEYS:
                self._evict(buckets, now)
            b = buckets[key] = Bucket(burst, now)
        b.tokens = min(burst, b.tokens + (now - b.at) * rate)
        b.at = now
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return 0
        return max(1, math.ceil((1.0 - b.tokens) / rate))

    @staticmethod
    def _evict(buckets: dict, now: float) -> None:
        """Full: drop the buckets idle long enough to be full again (they lose nothing); else the oldest quarter."""
        idle = [k for k, b in buckets.items() if now - b.at > IDLE_FULL_S]
        if not idle:
            idle = sorted(buckets, key=lambda k: buckets[k].at)[:MAX_KEYS // 4]
        for k in idle:
            del buckets[k]

    def admit(self, device: str | None, write: bool, peer: str = "", public: bool = False, shared: str = "meta") -> int:
        """0: go on. Else the Retry-After seconds (a 429). device: the device's key, None: unauthenticated.
        public: the route needs no token, so it has a shared bucket of its own: `meta` for meta (the app's launch),
        `signin` for the sign-in's public routes, so a flood on one never drains the other."""
        now = self.clock()
        with self._lock:
            if device:
                rate, burst = (WRITE_RATE, WRITE_BURST) if write else (READ_RATE, READ_BURST)
                return self._take(self._devices, f"{'w' if write else 'r'}:{device}", rate, burst, now)
            wait = self._take(self._ips, peer or "-", ANON_RATE, ANON_BURST, now)     # its own address first
            if wait:
                return wait
            if not public:
                name, rate, burst = "anon", ALL_ANON_RATE, ALL_ANON_BURST
            elif shared == "signin":
                name, rate, burst = "signin", SIGNIN_RATE, SIGNIN_BURST
            else:
                name, rate, burst = "meta", META_RATE, META_BURST
            return self._take(self._shared, name, rate, burst, now)

    def enter(self) -> bool:
        """A request starts: False when v1 already has its share of concurrent requests."""
        with self._lock:
            if self._busy >= self.max_requests:
                return False
            self._busy += 1
            return True

    def leave(self) -> None:
        with self._lock:
            self._busy = max(0, self._busy - 1)

    def open_stream(self, device: str) -> bool:
        """A stream opens for `device`: False at 2 for that device or at the global cap. close_stream when it ends."""
        with self._lock:
            if self._streams.get(device, 0) >= STREAMS_PER_DEVICE or sum(self._streams.values()) >= self.max_streams:
                return False
            self._streams[device] = self._streams.get(device, 0) + 1
            return True

    def close_stream(self, device: str) -> None:
        with self._lock:
            n = self._streams.get(device, 0) - 1
            if n > 0:
                self._streams[device] = n
            else:
                self._streams.pop(device, None)

    def streams(self) -> int:
        with self._lock:
            return sum(self._streams.values())
