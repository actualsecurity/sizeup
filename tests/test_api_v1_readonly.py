"""The owner's rule (docs/mobile-api.md "Scope"): the mobile app is a situational-awareness tool and `/api/v1` must not
modify the hub's or the CAD's data. This is the permanent guard: (a) no route but a GET unless it is named below with a
reason, (b) a read of every kind, as every kind of person, leaves every table of cad.sqlite and the CAD's change counter
exactly as it was. The first run of (b) found `Reads.may()` adding an unknown email to the CAD's `operators` table (a
dispatcher!), a settings-audit row and a store.rev bump through Settings.person(). Everything here is invented."""
from __future__ import annotations

import hashlib
import json

import pytest

from sizeup.api.v1.handler import V1
from sizeup.api.v1.limits import Limits
from sizeup.api.v1.principal import Principal
from sizeup.api.v1.routes import ROUTES
from sizeup.api.v1.switches import Switches
from tests.test_api_v1_reads import link_radio, seed
from tests.test_settings import post

# The ONLY non-GET routes /api/v1 may have, by (method, path): the app's own device bookkeeping (sign-in sessions and
# push registration, in hub.sqlite; never the CAD). Empty on purpose: the pull requests that build them add their routes
# here, each with the reason, and the owner decides the boundary. A route that changes a unit, an acknowledgement, a
# note, a dispatch or an incident is never allowed here (docs/mobile-api.md "Scope").
DEVICE_BOOKKEEPING: dict[tuple[str, str], str] = {}

PEOPLE = {"Bearer t-viewer": "val@example.org", "Bearer t-dispatcher": "jordan@example.org",
          "Bearer t-admin": "casey@example.org", "Bearer t-unknown": "newcomer@example.org",
          "Bearer t-inactive": "dee@example.org"}


def resolver(headers):
    email = PEOPLE.get(headers.get("Authorization") or "")
    return Principal("dev-" + email.split("@")[0], email) if email else None


def make_v1(cad):
    return V1(switches=Switches(lambda: True, lambda: {"reads": True}), resolver=resolver, cad=cad,
              limits=Limits(clock=_ticks()))


def _ticks():
    n = [0.0]

    def clock():
        n[0] += 1.0                                    # a second a request: a test is never rate limited
        return n[0]
    return clock


def fingerprint(cad) -> dict:
    """Every table of cad.sqlite (found here, so a table added later is covered), each as a hash of all its rows, plus
    the CAD's change counter."""
    out = {"store.rev": cad.store.rev}
    names = [r[0] for r in cad.store.db.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    for name in names:
        if name.startswith("sqlite_"):
            continue
        try:
            rows = cad.store.db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
        except Exception:  # noqa: BLE001 - a table with no rowid (WITHOUT ROWID): its natural order
            rows = cad.store.db.execute(f'SELECT * FROM "{name}"').fetchall()
        out[name] = hashlib.sha256(json.dumps([list(map(repr, r)) for r in rows]).encode()).hexdigest()
    return out


def test_every_route_is_a_get_unless_it_is_named_device_bookkeeping_with_a_reason():
    for r in ROUTES:
        if r.method != "GET":
            assert (r.method, r.path) in DEVICE_BOOKKEEPING, f"{r.method} {r.path}: a write, not allowed (see Scope)"
    for key, why in DEVICE_BOOKKEEPING.items():
        assert key in {(r.method, r.path) for r in ROUTES} and len(why) > 20, key        # no stale name, a real reason
        assert key[1].startswith(("/api/v1/devices", "/api/v1/me")), f"{key}: only the app's own bookkeeping"


def test_the_writes_flag_does_not_exist():
    from sizeup.api.v1.switches import FEATURES
    assert "writes" not in FEATURES
    on = Switches(lambda: True, lambda: {"reads": True, "writes": True})
    assert "writes" not in on.features() and on.allows("writes") is False        # an unknown flag is off


@pytest.fixture
def cad(tmp_path):
    c, ids = seed(tmp_path, calls=4)
    c.ids = ids
    link_radio(c, ids[0], "FG1-ours", "", "Engine 60 on scene")
    c.premise.create({"incident_id": ids[0], "text": "Knox box at the side door", "category": "access",
                      "operator": "Casey"})
    post(c, "premise_visibility", action="set", key="medical", value="admin")
    return c


def all_gets(cad):
    """Every GET route of the table, as a (path, query) to call."""
    paths = {"/api/v1/snapshot": "", "/api/v1/incidents": "limit=2", "/api/v1/units": "limit=5", "/api/v1/alarms": ""}
    out = []
    for r in ROUTES:
        if r.method != "GET" or r.path == "/api/v1/meta":
            continue
        if "{id}" in r.path:
            out += [(r.path.replace("{id}", str(i)), "") for i in cad.ids + [999999]]       # and one that is not there
        else:
            assert r.path in paths, f"{r.path}: a new read route: add it to this guard"
            out.append((r.path, paths[r.path]))
    return out


@pytest.mark.parametrize("token", sorted(PEOPLE))
def test_no_read_changes_a_byte_of_the_cad_for_any_kind_of_person(cad, token):
    people_before = cad.store.one("SELECT COUNT(*) AS n FROM operators")["n"]
    before = fingerprint(cad)
    v1 = make_v1(cad)
    for path, query in all_gets(cad):
        status = v1.respond("GET", path, query, {"Authorization": token})[0]
        assert status in (200, 404), (path, status)
        if "999999" not in path:
            assert status == 200, path
    assert fingerprint(cad) == before
    assert cad.store.one("SELECT COUNT(*) AS n FROM operators")["n"] == people_before     # nobody was added by a read


def test_an_unknown_email_is_a_viewer_and_is_never_added(cad):
    before = fingerprint(cad)
    st = cad.settings.standing_readonly("Newcomer@Example.org")
    assert (st["role"], st["may"], st["active"], st["email"]) == ("viewer", "viewer", True, "newcomer@example.org")
    assert fingerprint(cad) == before
    assert cad.store.one("SELECT COUNT(*) AS n FROM operators WHERE email = 'newcomer@example.org'")["n"] == 0
    assert cad.store.one("SELECT COUNT(*) AS n FROM settings_audit WHERE key = 'newcomer@example.org'")["n"] == 0


def test_standing_readonly_is_standing_for_everyone_who_is_on_the_list(cad):
    for email in ("val@example.org", "jordan@example.org", "casey@example.org", "dee@example.org"):
        a, b = cad.settings.standing_readonly(email), cad.settings.standing(email)
        assert {k: a[k] for k in ("email", "role", "active", "may", "bootstrap")} == \
            {k: b[k] for k in ("email", "role", "active", "may", "bootstrap")}, email
    assert cad.settings.standing_readonly("dee@example.org")["may"] == "viewer"          # inactive: a viewer
    with pytest.raises(Exception, match="not an email"):
        cad.settings.standing_readonly("not an email")


def test_a_bad_cursor_a_missing_cursor_and_a_304_write_nothing_either(cad):
    v1 = make_v1(cad)
    h = {"Authorization": "Bearer t-unknown"}
    before = fingerprint(cad)
    assert v1.respond("GET", "/api/v1/incidents", "after=garbage", h)[0] == 400
    assert v1.respond("GET", "/api/v1/incidents", "after=", h)[0] == 200
    assert v1.respond("GET", "/api/v1/units", "after=" + "x" * 300, h)[0] == 400
    for path in ("/api/v1/snapshot", "/api/v1/incidents", "/api/v1/units", "/api/v1/alarms",
                 f"/api/v1/incidents/{cad.ids[0]}"):
        etag = v1.respond("GET", path, "", h)[3]["ETag"]
        assert v1.respond("GET", path, "", {**h, "If-None-Match": etag})[0] == 304
    assert fingerprint(cad) == before


def test_with_no_admin_yet_a_read_still_writes_nothing(tmp_path):
    """Before there is an admin everyone signed in may do what an admin does (the console's bootstrap); a read of an
    unknown email still adds no row."""
    from tests.test_cad import make_cad
    c = make_cad(tmp_path)
    c.log = lambda m: None
    before = fingerprint(c)
    st = c.settings.standing_readonly("first@example.org")
    assert st["bootstrap"] is True and st["may"] == "admin"
    assert fingerprint(c) == before
