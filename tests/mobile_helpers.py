"""For the mobile pull requests' tests: a Principal with a role, so a test that injects a resolver says who it is
(sizeup/api/v1/principal.py: a Principal with no role is a viewer, and a route's `role` is enforced)."""

from __future__ import annotations

from sizeup.api.v1.principal import Principal


def principal(
    role: str = "admin",
    email: str = "member@example.org",
    device: str = "dv0123456789abcdef",
    name: str = "Test Member",
) -> Principal:
    return Principal(device, email, role, name)
