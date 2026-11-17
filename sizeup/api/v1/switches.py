"""The switches of /api/v1 (#619, docs/mobile-api.md "Sharing the production hub"): the master switch ([mobile]
enabled, off by default) and one flag per feature. Both are read on every request, so a change needs no deploy of
the app. Nothing here reads a database or a key: the hub's config says whether mobile is on."""
from __future__ import annotations

from typing import Callable, Mapping

# The app's feature flags, as `meta.features` reports them. Each stays off until its pull request is proven: the
# route table names the one a route needs (routes.Route.flag).
FEATURES = ("auth", "reads", "stream", "writes", "push", "media", "responders")


class Switches:
    """enabled(): the master switch. features(): which flags are on (none today: each pull request that builds a
    feature turns its flag on here, behind the master switch). max_requests / max_streams: the caps in limits.py."""

    def __init__(self, enabled: Callable[[], bool] | None = None,
                 features: Callable[[], Mapping[str, bool]] | None = None,
                 max_requests: int = 8, max_streams: int = 16):
        self._enabled = enabled or (lambda: False)
        self._features = features or (lambda: {})
        self.max_requests, self.max_streams = max_requests, max_streams

    def enabled(self) -> bool:
        try:
            return self._enabled() is True
        except Exception:        # a broken switch is an off switch
            return False

    def features(self) -> dict[str, bool]:
        """Every flag, effective: off when the master switch is off."""
        if not self.enabled():
            return {f: False for f in FEATURES}
        try:
            on = self._features()
        except Exception:
            on = {}
        return {f: on.get(f) is True for f in FEATURES}

    def allows(self, flag: str) -> bool:
        """May a route needing `flag` be served? "" = no flag (meta, which the handler lets through with mobile off)."""
        return self.features().get(flag, False) if flag else True
