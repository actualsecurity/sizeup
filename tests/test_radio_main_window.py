"""The Radio view's Main and Other talkgroups each get their own newest N (#241: on a busy evening the other
talkgroups filled the shared newest 300 within 20 minutes and Main said None though there was earlier traffic on
our channels). Main (FG1 and the FM channels, the talkgroups marked Main, the notes) is asked on its own, at most
MAIN_LIMIT; Other at most OTHER_LIMIT; the same filters on both; each found through an index, not a scan."""

import json
import re
import subprocess

import pytest

from sizeup.cad.api import CadApi
from sizeup.cad.comms import MAIN_LIMIT, MAX_ROWS, OTHER_LIMIT
from tests.test_radio_emergency import NODE, STATIC
from tests.test_radios import Clock, _event_with, cad, tx
from tests.test_stuck_emergency import COLLAPSE_HARNESS


def bulk(c, rows):
    """Many transmissions at once (record_traffic for each would link and log them one by one): (id, start,
    channel, talkgroup, rig, text, emergency)."""
    with c.store.tx() as db:
        db.executemany("INSERT INTO radio_traffic (id, start, end, channel, talkgroup, rig, text, status, emergency, "
                       "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'done', ?, ?)",
                       [(i, s, s + 2, ch, tg, rig, text, em, s) for i, s, ch, tg, rig, text, em in rows])
    c.comms.rev += 1


def busy_evening(c, t, n_other=500, n_main=10):
    """n_main transmissions on our channels (FG1 and Fire/EMS 15), then n_other on other talkgroups, all newer."""
    main = [(f"FG1-{k}" if k % 2 else f"307-{k}-call_1", t + k, "FG1" if k % 2 else "Fire/EMS 15", None if k % 2 else 307,
             "", "engine on location", 0) for k in range(n_main)]
    other = [(f"o-{k}", t + 1000 + k, ("WCPD Ch 1", "Fire/EMS 13", "LLT Supervisor 1")[k % 3], (359, 313, 1201)[k % 3],
              "", "unit 12 copy", 0) for k in range(n_other)]
    bulk(c, main + other)
    return [r[0] for r in main], [r[0] for r in other]


def get(c, q=""):
    code, body, _ = CadApi(c).get("/api/cad/comms", q)
    assert code == 200, body
    return json.loads(body)


def split(d):
    return [r["id"] for r in d["rows"] if r["main"]], [r["id"] for r in d["rows"] if not r["main"]]


def test_the_issue_main_keeps_its_ten_under_500_newer_other_rows(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    main_ids, other_ids = busy_evening(c, clock.t)
    d = get(c)
    main, other = split(d)
    assert main == main_ids[::-1]                                    # all ten, newest first, though 500 came after
    assert other == other_ids[::-1][:OTHER_LIMIT]                    # Other: its own newest 300
    assert d["counts"] == {"main": 10, "other": OTHER_LIMIT} and d["limits"] == {"main": MAIN_LIMIT, "other": OTHER_LIMIT}
    assert [r["start"] for r in d["rows"]] == sorted((r["start"] for r in d["rows"]), reverse=True)   # rows: both, newest first
    # the board's "Main" (type=radio, main rows only) sees them too
    assert [r["id"] for r in get(c, "type=radio")["rows"] if r["main"]] == main_ids[::-1]


def test_main_follows_the_main_switch_and_never_takes_the_airband(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock, airband=("HPN Tower",))
    main_ids, other_ids = busy_evening(c, clock.t, n_other=400)
    bulk(c, [("atc-1", clock.t + 5000, "HPN Tower", None, "", "cleared to land", 0)])
    main, other = split(get(c))
    assert "atc-1" in other and "atc-1" not in main                  # FM, but the airband (#233): never Main
    assert set(main) == set(main_ids)
    c.main_tg = lambda tg: tg in (307, 359)                          # WCPD Ch 1 marked Main in Settings > Radios
    wcpd = {f"o-{k}" for k in range(0, 400, 3)}
    main, other = split(get(c))
    assert set(main) == set(main_ids) | wcpd and set(other) == set(other_ids) - wcpd | {"atc-1"}


def test_the_filters_apply_to_both_sections(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E63"], clock)
    t = clock.t + 10
    busy_evening(c, t, n_other=450)                                  # the noise: on neither side of any filter below
    bulk(c, [("FG1-hit", t + 3000, "FG1", None, "E63", "Engine 63 ladder pipe", 0),
             ("o-hit", t + 3001, "WCPD Ch 1", 359, "E63", "Engine 63 ladder pipe", 0),
             ("FG1-em", t + 3002, "FG1", None, "E14", "mayday", 1),
             ("o-em", t + 3003, "DOT Dispatch 1", 1325, "E14", "mayday", 1)])
    for tid in ("FG1-hit", "o-hit"):
        c.link_traffic(a["id"], {"traffic_id": tid, "operator": "Lt. Ruiz"})
    c.note(a["id"], {"text": "E63 stated ladder pipe in service", "units": ["E63"], "operator": "Corey"})
    note = lambda d: [r["id"] for r in d["rows"] if r["kind"] == "note"]

    def both(q, main, other, notes=0):
        d = get(c, q)
        m, o = split(d)
        assert [i for i in m if not i.startswith("note-")] == main and o == other, q
        assert len(note(d)) == notes, q
        assert d["counts"] == {"main": len(m), "other": len(o)}, q

    both("unit=E63", ["FG1-hit"], ["o-hit"], notes=1)
    both("q=ladder+pipe", ["FG1-hit"], ["o-hit"], notes=1)
    both(f"event={a['number']}", ["FG1-hit"], ["o-hit"], notes=1)
    both("emergency=1", ["FG1-em"], ["o-em"])
    both("pcfd=1", ["FG1-hit"], ["o-hit"], notes=1)                  # E14: Rye Brook's
    both(f"since={t + 3000.5}&until={t + 3002.5}", ["FG1-em"], ["o-hit"])
    both("channel=WCPD+Ch+1&q=ladder", [], ["o-hit"])
    both("channel=FG1&q=ladder", ["FG1-hit"], [])
    both("type=notes", [], [], notes=1)
    both("type=radio&unit=E63", ["FG1-hit"], ["o-hit"])
    both("q=nothing+like+this", [], [])


def test_each_section_is_capped_on_its_own(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    main_ids, other_ids = busy_evening(c, clock.t, n_other=600, n_main=250)
    d = get(c)
    main, other = split(d)
    assert main == main_ids[::-1][:MAIN_LIMIT] and other == other_ids[::-1][:OTHER_LIMIT]
    assert d["counts"] == {"main": MAIN_LIMIT, "other": OTHER_LIMIT}
    main, other = split(get(c, "main_limit=5&limit=7"))
    assert main == main_ids[::-1][:5] and other == other_ids[::-1][:7]
    d = get(c, "main_limit=9999&limit=9999")                         # never more than MAX_ROWS a section
    assert d["limits"] == {"main": MAX_ROWS, "other": MAX_ROWS} and d["counts"] == {"main": 250, "other": MAX_ROWS}
    assert CadApi(c).get("/api/cad/comms", "main_limit=lots")[0] == 400
    # notes are Main's: they count against Main's limit, not Other's
    a = _event_with(c, ["E63"], clock)
    for k in range(3):
        clock.t += 100_000
        c.note(a["id"], {"text": f"E63 note {k}", "units": ["E63"], "operator": "Corey"})
    d = get(c, "main_limit=4")
    main, other = split(d)
    assert len(main) == 4 and sum(i.startswith("note-") for i in main) == 3 and len(other) == OTHER_LIMIT


def test_a_live_update_goes_to_its_own_section_and_both_stay_capped(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    main_ids, other_ids = busy_evening(c, clock.t, n_other=OTHER_LIMIT + 20)
    d = get(c)
    clock.t += 5000
    c.record_traffic(tx("FG1-new", clock.t, rig="E63", text="Engine 63 on location"))          # Main
    c.record_traffic(tx("359-new-call_1", clock.t + 1, "WCPD Ch 1", "", "", 359, text="unit 12"))   # Other
    d2 = get(c, f"rev={d['rev']}")
    assert not d2.get("unchanged") and d2["rev"] != d["rev"]
    main, other = split(d2)
    assert main[0] == "FG1-new" and main[1:] == main_ids[::-1]
    assert other[0] == "359-new-call_1" and len(other) == OTHER_LIMIT and other[1:] == other_ids[::-1][:OTHER_LIMIT - 1]
    assert get(c, f"rev={d2['rev']}")["unchanged"] is True               # nothing new: the cheap answer, as before (#137)


def test_the_queries_use_the_talkgroup_and_time_index(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    busy_evening(c, clock.t)
    idx = {r["name"]: r["sql"] for r in c.store.q("SELECT name, sql FROM sqlite_master WHERE type = 'index' "
                                                     "AND tbl_name = 'radio_traffic'")}
    assert "(talkgroup, start)" in idx["radio_traffic_tg_start"] and "(channel, start)" in idx["radio_traffic_channel_start"]
    plans = []
    q = c.store.q

    def spy(sql, args=()):
        if sql.startswith("SELECT t.*"):
            plans.append(" ".join(r["detail"] for r in c.store.db.execute("EXPLAIN QUERY PLAN " + sql, args)))
        return q(sql, args)
    c.store.q = spy
    get(c)
    assert plans and all("USING INDEX" in p and "TEMP B-TREE" not in p for p in plans), plans
    assert sum("radio_traffic_tg_start" in p for p in plans) == 2      # the FM channels, then Fire/EMS 15
    assert "radio_traffic_start" in plans[-1]                          # Other: newest first along the time index
    c.store.q = q
    assert c.comms.talkgroups() == [307, 313, 359, 1201]


def test_an_older_database_gets_the_indexes_on_open(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    for name in ("radio_traffic_tg_start", "radio_traffic_channel_start"):
        c.store.db.execute(f"DROP INDEX {name}")
    c.store.db.close()
    for _ in range(2):                                                 # and opening again changes nothing
        c = cad(tmp_path, clock)
        names = {r["name"] for r in c.store.q("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"radio_traffic_tg_start", "radio_traffic_channel_start"} <= names
        c.store.db.close()


# -- the page: the header counts each section against its own limit ----------------------------------------------


def run_view(tmp_path, d):
    head = COLLAPSE_HARNESS[:COLLAPSE_HARNESS.index("let rows = [")]
    tail = r"""
const d = JSON.parse(process.argv[4]);
global.api = async () => d;
eval(fs.readFileSync(src, "utf8"));
const tick = () => new Promise((r) => setTimeout(r, 0));
(async () => {
  RadioView.open("");
  await tick(); await tick();
  RadioView.render();
  const out = { count: $("#rv-count").textContent, main: $("#rv-main").children.length,
                other: $("#rv-other-sum").textContent };
  RadioView.close();
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""
    h = tmp_path / "view.js"
    h.write_text(head + tail)
    p = subprocess.run([NODE, str(h), str(STATIC / "radio-view.js"), "-", json.dumps(d)], capture_output=True, text=True,
                       timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def _rows(n_main, n_other, notes=0):
    main = [{"id": f"m{k}", "kind": "radio", "main": True, "talkgroup": None, "channel": "FG1", "start": 2000 - k,
             "text": "engine on location", "status": "done"} for k in range(n_main)]
    main += [{"id": f"note-{k}", "kind": "note", "main": True, "talkgroup": None, "channel": "Notes", "start": 1500 - k,
              "text": "E63 stated", "status": "done", "events": []} for k in range(notes)]
    other = [{"id": f"o{k}", "kind": "radio", "main": False, "talkgroup": 359 + k % 9, "channel": f"TG {359 + k % 9}",
              "start": 9000 - k, "text": "unit 12", "status": "done"} for k in range(n_other)]
    return sorted(main + other, key=lambda r: -r["start"])


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_header_shows_both_counts(tmp_path):
    lim = {"main": MAIN_LIMIT, "other": OTHER_LIMIT}
    out = run_view(tmp_path, {"rows": _rows(42, OTHER_LIMIT, notes=2), "rev": "1", "limits": lim,
                              "counts": {"main": 44, "other": OTHER_LIMIT}})
    assert out["count"] == f"Main 42 TX, 2 notes · Other {OTHER_LIMIT} TX (newest {OTHER_LIMIT})"
    assert out["main"] == 44 and out["other"] == f"{OTHER_LIMIT} TX, 9 TGs"
    out = run_view(tmp_path, {"rows": _rows(MAIN_LIMIT, 12), "rev": "1", "limits": lim})
    assert out["count"] == f"Main {MAIN_LIMIT} TX (newest {MAIN_LIMIT}) · Other 12 TX"
    # more than a section's limit (a stray push): each still at most its own
    out = run_view(tmp_path, {"rows": _rows(5, 40), "rev": "1", "limits": {"main": 3, "other": 30}})
    assert out["count"] == "Main 3 TX (newest 3) · Other 30 TX (newest 30)" and out["main"] == 3
    # an older hub (one shared newest 300, no limits): as before
    out = run_view(tmp_path, {"rows": _rows(0, OTHER_LIMIT), "rev": "1"})
    assert out["count"] == f"{OTHER_LIMIT} TX (newest 300)" and out["main"] == 0


def test_the_view_no_longer_shares_one_window_in_the_source():
    rv = (STATIC / "radio-view.js").read_text()
    assert "rows.length >= 300" in rv and "if (!lim)" in rv              # only for an older hub
    assert re.search(r"st\.limits = d\.limits", rv)
    html = (STATIC / "cad.html").read_text()
    assert "the newest 200" in html and "their own newest 300" in html
