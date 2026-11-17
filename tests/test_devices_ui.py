"""Settings > Users > Devices (#558, sizeup/web/static/settings.js sDevices / sDevicesRender): the app's devices card.

The functions are cut out of settings.js and run in node on the small fake DOM of test_reception_ui.py (no browser).
The values are invented."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_reception_ui import DOM

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")
NAMES = ("sAgo", "sBtn", "sCard", "sHelp", "sCardHead", "sAsk", "sMsg", "sDevices", "sDevicesRender")

# the shared fake DOM up to its api/fetch stubs, then what this card needs of the rest of the page
HARNESS = DOM.split("const IN = JSON.parse(")[0] + r"""
const IN = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const asked = [], toasts = [];
global.serverNow = () => IN.now;
global.UI = { confirm: async () => true, toast: (t) => toasts.push(t) };
global.cad = { who: { email: IN.me, may: IN.may } };
global.S = { section: "users" };
const byId = {};
global.$ = (sel) => byId[sel];
global.api = async (method, url, body) => {
  asked.push([method, url, body || null]);
  if (method === "GET") {
    if (IN.devices === null) throw new Error("not found");
    return IN.devices;
  }
  return { ok: true };
};
eval(fs.readFileSync(process.argv[2], "utf8"));
const tick = () => new Promise((ok) => setImmediate(ok));
(async () => {
  const panel = new N("div");
  const card = el("div", "card s-card");
  card.id = "s-devices"; card.hidden = true;
  byId["#s-devices"] = card; byId["#s-msg"] = el("div"); panel.append(card);
  await sDevices(true);
  const rows = card.querySelectorAll("li.s-row");
  const out = { hidden: card.hidden, title: card.querySelector("h2") && card.querySelector("h2").textContent,
                info: card.querySelector(".card-head")?.querySelector("span.dim").textContent,
                rows: rows.map((r) => ({ text: r.textContent, cls: r.className,
                                         spans: r.children.filter((k) => k.tagName === "SPAN")
                                                          .map((k) => k.textContent),
                                         buttons: r.querySelectorAll("button").map((b) => ({ t: b.textContent,
                                                                                             cls: b.className })),
                                         chips: r.querySelectorAll(".chip").map((c) => c.textContent),
                                         nodes: r.children.map((k) => k.tagName) })) };
  const btn = card.querySelectorAll("button").find((b) => b.textContent === "Revoke");
  if (btn) { btn.click(); await tick(); await tick(); await tick(); }
  out.asked = asked; out.toasts = toasts;
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e.stack || String(e)); process.exit(1); });
"""

NOW = 1_800_000_000.0


def dev(did, name, email, active=True, platform="ios", version="1.2.0", seen=300, revoked=None):
    return {
        "id": did,
        "email": email,
        "name": name,
        "platform": platform,
        "app_version": version,
        "created": NOW - 9999,
        "last_seen": NOW - seen,
        "revoked_at": None if active else NOW - (revoked or 5),
        "revoked_why": "",
        "active": active,
    }


def run(tmp_path, devices, me="riva@example.org", may="admin"):
    if NODE is None:
        pytest.skip("node is not installed")
    src = (STATIC / "settings.js").read_text()
    parts = [re.search(r"^const S_DEV_TTL_MS = [^\n]*\n", src, re.M).group(0)]
    for n in NAMES:
        m = re.search(rf"^(?:async )?function {n}\(.*?^}}\n", src, re.M | re.S)
        assert m, n
        parts.append(m.group(0))
    (tmp_path / "s.js").write_text("\n".join(parts))
    (tmp_path / "in.json").write_text(json.dumps({"devices": devices, "me": me, "may": may, "now": NOW}))
    (tmp_path / "h.js").write_text(HARNESS)
    r = subprocess.run(
        [NODE, str(tmp_path / "h.js"), str(tmp_path / "s.js"), str(tmp_path / "in.json")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def listing(devices, all_=True):
    return {"devices": devices, "max": 3, "all": all_}


def test_the_card_stays_hidden_when_the_hub_says_404(tmp_path):
    out = run(tmp_path, None)
    assert out["hidden"] is True and out["rows"] == [] and out["asked"][0][:2] == ["GET", "/api/cad/devices"]


def test_an_admin_sees_every_persons_devices_with_their_email(tmp_path):
    out = run(
        tmp_path,
        listing(
            [
                dev("dv0000000000000001", "Casey's iPhone", "casey@example.org"),
                dev("dv0000000000000002", "Otis's iPhone", "otis@example.org", seen=7200),
            ]
        ),
    )
    assert out["hidden"] is False and out["title"] == "Devices" and out["info"] == "2 active"
    texts = [r["text"] for r in out["rows"]]
    assert "casey@example.org" in texts[0] and "otis@example.org" in texts[1]
    assert "Seen 5 min ago" in texts[0] and "Seen 2 h ago" in texts[1] and "ios 1.2.0" in texts[0]


def test_anyone_else_sees_only_their_own_under_my_devices_and_can_still_revoke(tmp_path):
    out = run(
        tmp_path,
        listing([dev("dv0000000000000002", "Otis's iPhone", "otis@example.org")], all_=False),
        me="otis@example.org",
        may="viewer",
    )
    assert out["title"] == "My devices" and out["info"] == "1 of 3"
    assert "otis@example.org" not in out["rows"][0]["text"]
    assert out["rows"][0]["buttons"] == [
        {"t": "Revoke", "cls": "ro-ok"}
    ]  # ro-ok: Settings does not lock it for a viewer


def test_revoke_posts_the_action_and_the_device_id_then_reads_again(tmp_path):
    out = run(tmp_path, listing([dev("dv0000000000000001", "Casey's iPhone", "casey@example.org")]))
    assert out["asked"][1] == ["POST", "/api/cad/devices", {"action": "revoke", "id": "dv0000000000000001"}]
    assert out["asked"][2][:2] == ["GET", "/api/cad/devices"] and out["toasts"] == ["Casey's iPhone revoked"]


def test_a_revoked_device_shows_one_chip_and_no_button(tmp_path):
    out = run(tmp_path, listing([dev("dv0000000000000003", "Old phone", "casey@example.org", active=False, revoked=5)]))
    row = out["rows"][0]
    assert row["buttons"] == [] and row["chips"] == ["revoked just now"] and "retired" in row["cls"]
    assert "Revoked" not in row["text"] and "Seen" not in row["text"]  # the one fact, once


def test_text_is_rendered_as_text_never_as_markup(tmp_path):
    evil = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    out = run(tmp_path, listing([dev("dv0000000000000004", evil, "casey@example.org")]))
    assert evil in out["rows"][0]["text"]  # shown as the characters it is
    assert set(out["rows"][0]["nodes"]) <= {"STRONG", "SPAN", "BUTTON"}  # and never became an element
    src = (STATIC / "settings.js").read_text()
    body = src[src.index("function sDevicesRender") : src.index("// Per-person activity on the hub")]
    assert "innerHTML" not in body and "insertAdjacentHTML" not in body


def test_the_settings_nits_stay_fixed():
    src = (STATIC / "settings.js").read_text()
    assert (
        "S_DEV_TTL_MS" in src
        and "15000)" not in src[src.index("async function sDevices") : src.index("function sDevicesRender")]
    )
    assert (
        "nodeType === 3"
        not in src[src.index("function sDevicesRender") : src.index("// Per-person activity on the hub")]
    )
