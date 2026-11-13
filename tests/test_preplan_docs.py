"""Pre-plan site documents (#459, sizeup/cad/preplan_docs.py, sizeup/docfiles.py, sizeup/docstore.py, static/preplan-docs.js):
a building's floor plans, overlaid on the map of every call there, and its other documents, opened in the app.

Physical-security data: everything here is invented. The buildings are tests/test_preplan.py's ("100 Example Ave" on an
invented street grid), the plans are rectangles drawn here, the PDFs are written here byte by byte, and nothing a
document is called, where its plan sits, or what its file was named ever reaches the server log."""
import hashlib
import http.client
import io
import json
import re
import sqlite3
import struct
import subprocess
import threading
import zlib
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

from sizeup import aws, docfiles, docstore
from sizeup.cad import preplan_docs as ppd
from sizeup.cad.api import CadApi
from sizeup.cloud.access import Identity
from tests.test_address_history import fake_geocode
from tests.test_cad import make_cad
from tests.test_preplan import LON, NODE, STATIC, at, fake_gis, lat_of
from tests.test_roles import COREY, DEE, JULIAN, VAL, call, people

HAVE_PDFIUM = docfiles.can_render()


# -- invented files ---------------------------------------------------------------------------------------------------------

def png(w=40, h=30, extra=b"", tail=b""):
    """A grey rectangle with a dark frame (a "floor plan"), as an RGB PNG; extra: chunks before IDAT; tail: after IEND."""
    rows = b""
    for y in range(h):
        line = bytes(40 if (y in (0, h - 1) or x in (0, w - 1)) else 220 for x in range(w) for _ in range(3))
        rows += b"\0" + line

    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + extra
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"") + tail)


def png_chunk(t, b):
    return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))


def jpeg_with_gps(orientation=6):
    """A photo with EXIF: GPS (an invented point), the camera, the orientation; a comment; data after the end."""
    PIL = pytest.importorskip("PIL.Image")
    from PIL import Image
    im = Image.new("RGB", (64, 48), (200, 200, 200))
    for x in range(10, 54):
        im.putpixel((x, 24), (20, 20, 20))
    exif = Image.Exif()
    exif[0x010F] = "ExampleCam"                            # Make
    exif[0x0112] = orientation
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (0.0, 30.0, 0.0), "E", (0.0, 30.0, 0.0)          # 0.5 N, 0.5 E: nowhere real
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif, comment=b"Example comment")
    assert PIL
    return buf.getvalue() + b"<html><script>alert(1)</script></html>"


def pdf(pages=1, extra=b"", page_extra=b""):
    """A small PDF written here: each page a framed rectangle (a "floor plan"); extra: more names in its catalog;
    page_extra: in each page's dictionary."""
    objs = [b"<< /Type /Catalog /Pages 2 0 R " + extra + b">>"]
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(pages)).encode()
    objs.append(b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(pages).encode() + b" >>")
    for i in range(pages):
        stream = f"0.2 g 50 50 500 {300 + 50 * i} re S 0.8 g 60 60 480 {280 + 50 * i} re f".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 {400 + 50 * i}] /Contents {4 + 2 * i} 0 R ".encode()
                    + page_extra + b">>")
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    out, offs = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


# -- fixtures ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def cad(tmp_path):
    c = make_cad(geocoder=fake_geocode)
    c.gis = fake_gis()
    c.logged = []
    c.log = c.logged.append
    c.preplan_docs.store = docstore.FsDocStore(tmp_path / "docs")
    return c


@pytest.fixture
def api(cad):
    a = CadApi(cad)
    a.ids = people(cad)
    a.iid = at(cad, 100)
    return a


def wait_for(cond, timeout=10.0):
    """cond() true within timeout (a server thread finishing what it does after its answer), polled."""
    import time as _time
    end = _time.monotonic() + timeout
    while not cond():
        if _time.monotonic() > end:
            return False
        _time.sleep(0.01)
    return True


def who(email):
    return Identity(email=email) if email else None


def upload(api, data, email=JULIAN, **meta):
    m = {"kind": "floor_plan", "title": "First floor", "floor": "1", "incident_id": api.iid, **meta}
    code, raw, _ = api.upload("/api/cad/preplan/docs", quote(json.dumps(m)), data, who(email))
    return code, json.loads(raw)


def get(api, path, query="", email=None):
    code, raw, ctype, *h = api.get(path, query, who(email))
    if hasattr(raw, "read_all"):                       # a document: streamed (sizeup/attachments.py Stream)
        raw = raw.read_all()
    return code, (json.loads(raw) if "json" in ctype else raw), (h[0] if h else {})


def box_corners(lat, lon, dlat=0.00003, dlon=0.00004):
    """TL TR BR BL of a north-up box around the point (the 7 m footprints of tests/test_preplan.py)."""
    return [[lon - dlon, lat + dlat], [lon + dlon, lat + dlat], [lon + dlon, lat - dlat], [lon - dlon, lat - dlat]]


# -- what may be uploaded ------------------------------------------------------------------------------------------------

def test_the_type_is_the_content_never_the_name():
    assert docfiles.sniff(png()) == "image/png"
    assert docfiles.sniff(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert docfiles.sniff(pdf()) == "application/pdf"
    for bad in (SVG, b"<svg/>", b"  <!DOCTYPE html><html>", b"<html><script>", b"GIF89a....", b"PK\x03\x04zip",
                b"RIFF\0\0\0\0WEBPVP8 ", b"\0\0\0\x18ftypheic", b"just text", b""):
        with pytest.raises(docfiles.Refused):
            docfiles.sniff(bad)
    with pytest.raises(docfiles.Refused, match="SVG"):
        docfiles.sniff(SVG)


def test_svg_html_and_bad_magic_bytes_are_refused_whatever_they_are_called(api):
    for data in (SVG, b"<html><body>plan</body></html>", b"GIF89a" + b"\0" * 100, b"\x89PNX....", b"%PDX-1.4"):
        code, out = upload(api, data, title="plan.png")
        assert code == 400, data[:10]
    assert api.cad.store.q("SELECT COUNT(*) AS n FROM preplan_docs")[0]["n"] == 0
    assert not list((Path(api.cad.preplan_docs.store.root)).rglob("*.*"))          # nothing stored
    code, out = upload(api, png()[:-20])                                            # a PNG cut short
    assert code == 400 and "PNG" in out["error"]
    bad_crc = bytearray(png())
    bad_crc[40] ^= 0xFF
    assert upload(api, bytes(bad_crc))[0] == 400


def test_the_size_cap(api, monkeypatch):
    monkeypatch.setattr(ppd, "MAX_BYTES", 1000)
    code, out = upload(api, png(tail=bytes(2000)))
    assert code == 400 and "MB" in out["error"]
    assert upload(api, b"")[0] == 400


def test_exif_and_gps_are_stripped_from_photos_and_the_orientation_kept(api):
    from PIL import Image
    raw = jpeg_with_gps(orientation=6)
    assert b"ExampleCam" in raw and b"Example comment" in raw
    code, out = upload(api, raw, kind="photo", title="Rear", floor="")
    assert code == 200, out
    d = out["doc"]
    stored = (Path(api.cad.preplan_docs.store.root) / "preplan-docs").rglob("original.jpg")
    data = next(stored).read_bytes()
    assert b"ExampleCam" not in data and b"Example comment" not in data and b"<script" not in data
    assert data.endswith(b"\xff\xd9")
    im = Image.open(io.BytesIO(data))
    ex = im.getexif()
    assert dict(ex) == {0x0112: 6}                                    # only the orientation
    assert not ex.get_ifd(0x8825)                                     # no GPS
    assert im.size == (64, 48) and im.getpixel((30, 24))[0] < 80      # the picture itself unchanged
    assert d["bytes"] == len(data) and d["plan"] is False
    clean, w, h = docfiles.clean_jpeg(jpeg_with_gps(orientation=1))
    assert 0x0112 not in Image.open(io.BytesIO(clean)).getexif() and (w, h) == (64, 48)


def test_png_text_and_exif_chunks_are_dropped():
    text = png_chunk(b"tEXt", b"Comment\0secret") + png_chunk(b"eXIf", b"MM\0*\0\0\0\x08\0\0") + png_chunk(b"iTXt", b"x\0\0\0\0\0y")
    clean, w, h = docfiles.clean_png(png(extra=text, tail=b"<script>alert(1)</script>"))
    assert (w, h) == (40, 30)
    assert b"tEXt" not in clean and b"eXIf" not in clean and b"secret" not in clean and b"<script" not in clean
    assert clean == png()


ACTIVE_NAMES = (b"/URI", b"/JS", b"/JavaScript", b"/Launch", b"/AA", b"/OpenAction", b"/SubmitForm", b"/A ", b"/A<")


def inflated(data: bytes) -> bytes:
    """The PDF with every stream inflated (FlateDecode) beside it: what a name could hide in."""
    out = [data]
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            out.append(zlib.decompress(m.group(1)))
        except zlib.error:
            pass
    return b"\n".join(out)


def stored_pdf(api, did):
    ref = api.cad.store.q("SELECT ref FROM preplan_docs WHERE id = ?", (did,))[0]["ref"]
    return (Path(api.cad.preplan_docs.store.root) / "preplan-docs" / ref / "original.pdf").read_bytes()


def test_attachments_xfa_and_encryption_are_refused(api):
    for name in (b"/Names << /EmbeddedFiles 9 0 R >>", b"/AcroForm << /XFA 9 0 R >>", b"/Encrypt 9 0 R"):
        code, out = upload(api, pdf(extra=name), kind="inspection", title="Annual")
        assert code == 400 and "PDF" in out["error"], name
    code, out = upload(api, pdf(), kind="inspection", title="Annual", floor="")
    assert code == 200, out


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_links_actions_and_scripts_are_stripped_from_the_stored_copy(api):
    """Vendor and county exports have links, a GoTo /OpenAction, page actions, form fields: they upload, and what is
    stored has no action at all (in its streams either)."""
    annots = (b"/Annots [<< /Type /Annot /Subtype /Link /Rect [10 10 90 30] /A << /S /URI /URI (https://example.com/) >> >> "
              b"<< /Type /Annot /Subtype /Square /Rect [100 100 200 200] /C [1 0 0] "
              b"/AA << /E << /S /Launch /F (x.exe) >> >> /A << /S /JavaScript /JS (app.alert(1)) >> >> "
              b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (Owner) /V (Example) /Rect [300 300 400 320] "
              b"/AA << /K << /S /JavaScript /JS (event.rc = true) >> >> >>] "
              b"/AA << /O << /S /JavaScript /JS (app.alert(2)) >> >> ")
    catalog = (b"/OpenAction << /S /GoTo /D [3 0 R /Fit] >> /AA << /WC << /S /JavaScript /JS (x) >> >> "
               b"/Names << /JavaScript << /Names [(a) << /S /JavaScript /JS (app.alert(3)) >>] >> >> "
               b"/AcroForm << /Fields [] /NeedAppearances true >> ")
    for extra, page_extra in ((catalog, annots), (b"/OpenAction << /S /J#61vaScript /JS (x) >>", b""),
                              (b"/OpenAction << /S /Launch /F (x.exe) >>", b""), (b"", b"/AA << /O 9 0 R >> ")):
        raw = pdf(extra=extra, page_extra=page_extra)
        code, out = upload(api, raw, kind="preplan", title="Vendor export", floor="")
        assert code == 200, (out, extra, page_extra)
        clean = inflated(stored_pdf(api, out["doc"]["id"]))
        for name in ACTIVE_NAMES:
            assert name not in clean, (name, extra, page_extra)
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(stored_pdf(api, get(api, "/api/cad/preplan/docs", f"incident={api.iid}",
                                                         VAL)[1]["docs"][0]["id"])))
    assert len(reader.pages) == 1
    code, out = upload(api, pdf(extra=catalog, page_extra=annots), title="Plan with links", floor="1")   # a plan too
    assert code == 200 and out["doc"]["plan"] is True
    page = PdfReader(io.BytesIO(stored_pdf(api, out["doc"]["id"]))).pages[0]
    kinds = [a.get_object()["/Subtype"] for a in page["/Annots"]]
    assert kinds == ["/Square", "/Widget"]                                  # the link gone, the markup and field kept


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdfium2 not installed")
def test_a_pdf_plan_is_rendered_from_the_chosen_page(api):
    code, out = upload(api, pdf(pages=3), page=2, title="Second floor", floor="2")
    assert code == 200, out
    d = out["doc"]
    assert (d["pages"], d["page"], d["plan"]) == (3, 2, True)
    assert max(d["plan_w"], d["plan_h"]) == min(docfiles.MAX_PX, 600 * 4)   # at most MAX_PX, at most 4x (288 dpi)
    assert abs(d["plan_w"] / d["plan_h"] - 600 / 450) < 0.01               # page 2: 600 x 450 pt
    plan = next((Path(api.cad.preplan_docs.store.root)).rglob("plan.png")).read_bytes()
    assert docfiles.png_size(plan) == (d["plan_w"], d["plan_h"])
    code, out = call(api, f"/api/cad/preplan/docs/{d['id']}", {"page": 3}, JULIAN)          # another page
    assert code == 200 and out["doc"]["page"] == 3 and out["changed"] == ["page"]
    assert out["doc"]["corners"] is None
    assert abs(out["doc"]["plan_w"] / out["doc"]["plan_h"] - 600 / 500) < 0.01
    assert call(api, f"/api/cad/preplan/docs/{d['id']}", {"page": 9}, JULIAN)[0] == 400
    assert upload(api, pdf(pages=1), page=4)[0] == 400


def test_a_pdf_that_pdfium_cant_open_is_refused():
    if not HAVE_PDFIUM:
        pytest.skip("pypdfium2 not installed")
    with pytest.raises(docfiles.Refused):
        docfiles.render(b"%PDF-1.4\ngarbage\n%%EOF\n", 1)


# -- who --------------------------------------------------------------------------------------------------------------------

def test_viewers_look_and_never_write_and_a_deactivated_person_sees_nothing(api):
    code, out = upload(api, png(), email=VAL)
    assert code == 403
    code, out = upload(api, png())
    assert code == 200
    did = out["doc"]["id"]
    assert [d["id"] for d in get(api, "/api/cad/preplan/docs", f"incident={api.iid}", VAL)[1]["docs"]] == [did]
    assert get(api, f"/api/cad/preplan/docs/{did}/file", "", VAL)[0] == 200
    for body in ({"title": "x"}, {"corners": box_corners(lat_of(100), LON)}, {"action": "delete"}):
        assert call(api, f"/api/cad/preplan/docs/{did}", body, VAL)[0] == 403, body
    assert get(api, "/api/cad/preplan/docs", f"incident={api.iid}", DEE)[0] == 403        # deactivated
    assert get(api, f"/api/cad/preplan/docs/{did}/file", "", DEE)[0] == 403


def test_unauthenticated_requests_are_refused_by_the_server(api):
    """With Cloudflare Access on (the hub), nobody signed in gets a 401: no list, no file, no upload."""
    from sizeup.web.server import WebServer

    class Access:
        def identify(self, headers):
            e = headers.get("X-Test-Email")
            return Identity(email=e) if e else None
    did = upload(api, png())[1]["doc"]["id"]
    srv = WebServer("127.0.0.1", 0, {}, dict, lambda cid: None, lambda i: False, cad=api, access=Access(), role="hub")
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    try:
        def req(method, path, body=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", srv.httpd.server_address[1], timeout=10)
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, r.read(), r
        meta = quote(json.dumps({"kind": "photo", "title": "x", "incident_id": api.iid}))
        assert req("GET", f"/api/cad/preplan/docs?incident={api.iid}")[0] == 401
        assert req("GET", f"/api/cad/preplan/docs/{did}/file")[0] == 401
        assert req("POST", "/api/cad/preplan/docs", png(), {"X-Sizeup-Doc": meta})[0] == 401
        assert req("POST", "/api/cad/preplan/docs", png(), {"X-Sizeup-Doc": meta, "X-Test-Email": VAL})[0] == 403
        code, body, r = req("GET", f"/api/cad/preplan/docs/{did}/file", headers={"X-Test-Email": VAL})
        stored = next(Path(api.cad.preplan_docs.store.root).rglob("original.png")).read_bytes()
        assert code == 200 and body == stored and int(r.getheader("Content-Length")) == len(stored)   # streamed, whole
        assert r.getheader("Referrer-Policy") == "no-referrer" and r.getheader("Location") is None
        assert r.getheader("X-Content-Type-Options") == "nosniff"
        assert r.getheader("Content-Disposition") == f'inline; filename="site-doc-{did}.png"'
        assert "sandbox" in r.getheader("Content-Security-Policy") and "no-store" in r.getheader("Cache-Control")
        code, body, r = req("GET", f"/api/cad/preplan/docs/{did}/file?dl=1", headers={"X-Test-Email": VAL})
        assert r.getheader("Content-Disposition").startswith("attachment;")
        code, body, r = req("POST", "/api/cad/preplan/docs", png(), {"X-Sizeup-Doc": meta, "X-Test-Email": JULIAN})
        assert code == 200, body
        assert req("POST", "/api/cad/preplan/docs", png(), {"X-Test-Email": JULIAN})[0] == 400        # no details
        big = {"X-Sizeup-Doc": meta, "X-Test-Email": JULIAN, "Content-Length": str(30 * 1024 * 1024)}
        conn = http.client.HTTPConnection("127.0.0.1", srv.httpd.server_address[1], timeout=10)
        conn.putrequest("POST", "/api/cad/preplan/docs")
        for k, v in big.items():
            conn.putheader(k, v)
        conn.endheaders()                                                     # never sent: refused on its length
        assert conn.getresponse().status == 413
    finally:
        srv.httpd.shutdown()


def test_admin_only_documents_are_hidden_from_everyone_else(api):
    c = api.cad
    assert upload(api, png(), admin_only=True)[0] == 400                      # a dispatcher can't make one
    code, out = upload(api, png(), email=COREY, admin_only=True, title="Alarm panel layout", kind="preplan")
    assert code == 200 and out["doc"]["admin_only"] is True
    did = out["doc"]["id"]
    upload(api, png(), title="Lobby")
    for email in (VAL, JULIAN):
        docs = get(api, "/api/cad/preplan/docs", f"incident={api.iid}", email)[1]["docs"]
        assert [d["title"] for d in docs] == ["Lobby"], email
        assert get(api, f"/api/cad/preplan/docs/{did}", "", email)[0] == 404
        assert get(api, f"/api/cad/preplan/docs/{did}/file", "", email)[0] == 404
        assert get(api, f"/api/cad/preplan/docs/{did}/plan", "", email)[0] == 404
    for body in ({"title": "x"}, {"action": "delete"}, {"admin_only": False}):
        code, out = call(api, f"/api/cad/preplan/docs/{did}", body, JULIAN)
        assert code == 400 and out["error"] == f"no document {did}", body              # as if there were none
    assert len(get(api, "/api/cad/preplan/docs", f"incident={api.iid}", COREY)[1]["docs"]) == 2
    rows = [r for r in get(api, "/api/cad/settings/audit", f"section={ppd.SECTION}", JULIAN)[1]["audit"]]
    assert rows and not [r for r in rows if r["key"].split()[0] == str(did)]          # not there at all
    assert not any("Alarm" in (r["old"] or "") + (r["new"] or "") + r["key"] for r in rows)
    mine = [r for r in get(api, "/api/cad/settings/audit", f"section={ppd.SECTION}", COREY)[1]["audit"]
            if r["key"].split()[0] == str(did)]
    assert json.loads(mine[0]["new"]) == {"admin_only": True, "changed": sorted(ppd.AUDITED)}      # redacted once
    # a document made admin-only later: its earlier rows too
    lobby = next(d for d in get(api, "/api/cad/preplan/docs", f"incident={api.iid}", COREY)[1]["docs"] if d["title"] == "Lobby")
    assert call(api, f"/api/cad/preplan/docs/{lobby['id']}", {"admin_only": True}, JULIAN)[0] == 400
    assert call(api, f"/api/cad/preplan/docs/{lobby['id']}", {"admin_only": True}, COREY)[0] == 200
    rows = get(api, "/api/cad/settings/audit", f"section={ppd.SECTION}", JULIAN)[1]["audit"]
    assert not any("Lobby" in (r["old"] or "") + (r["new"] or "") + r["key"] for r in rows)
    assert not [r for r in rows if r["key"].split()[0] == str(lobby["id"])]
    assert not any('"changed":["admin_only","changed"]' in (r["new"] or "") for r in
                   get(api, "/api/cad/settings/audit", f"section={ppd.SECTION}", COREY)[1]["audit"])
    assert any("Lobby" in (r["new"] or "") for r in get(api, "/api/cad/settings/audit", f"section={ppd.SECTION}", COREY)[1]["audit"])
    assert c.preplan_docs.for_incident(c.store.incident(api.iid), "viewer") == []


# -- storage: streamed by the hub, never linked ----------------------------------------------------------------------------

class FakeS3:
    """The bucket: send() for PUT / GET / DELETE (sizeup/attachments.py S3), opener() for a streamed GET."""

    def __init__(self):
        self.objects, self.calls, self.opened = {}, [], []

    def __call__(self, method, url, headers, body, timeout):
        key = urlsplit(url).path.lstrip("/")
        self.calls.append((method, key, headers))
        if method == "PUT":
            self.objects[key] = (body, headers.get("Content-Type"))
            return 200, {}, b""
        if method == "GET":
            return (200, {}, self.objects[key][0]) if key in self.objects else (404, {}, b"<Code>NoSuchKey</Code>")
        if method == "DELETE":
            self.objects.pop(key, None)
            return 204, {}, b""
        return 400, {}, b""

    def opener(self, url, headers, timeout):
        u = urlsplit(url)
        key = u.path.lstrip("/")
        r = io.BytesIO(self.objects[key][0] if key in self.objects else b"<Code>NoSuchKey</Code>")
        self.opened.append({"url": url, "query": u.query, "headers": headers, "timeout": timeout, "r": r})
        return (200 if key in self.objects else 404), {"Content-Length": str(len(r.getvalue()))}, r


def s3_store():
    fake = FakeS3()
    st = docstore.S3DocStore("example-bucket", "preplan-docs", region="us-east-1", send=fake, opener=fake.opener,
                             creds=lambda: aws.Credentials("AKIDEXAMPLE", "secret", "session-token"))
    return st, fake


def test_documents_are_streamed_by_the_hub_and_never_linked(api, monkeypatch):
    monkeypatch.setattr(docstore, "CHUNK", 16)
    st, fake = s3_store()
    api.cad.preplan_docs.store = st
    code, out = upload(api, png(), email=JULIAN)
    assert code == 200
    did = out["doc"]["id"]
    (key, (body, ctype)), = [(k, v) for k, v in fake.objects.items() if "/original." in k]
    assert re.fullmatch(r"preplan-docs/[0-9a-f]{16}/original\.png", key) and ctype == "image/png"
    put = next(h for m, k, h in fake.calls if m == "PUT")
    assert put["x-amz-server-side-encryption"] == "AES256"
    code, stream, ctype, h = api.get(f"/api/cad/preplan/docs/{did}/file", "", who(VAL))
    assert code == 200 and ctype == "image/png" and len(stream) == len(body)
    assert h == {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                 "Content-Disposition": f'inline; filename="site-doc-{did}.png"',
                 "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox; "
                                            "frame-ancestors 'self'"}
    pieces = list(stream)
    assert len(pieces) > 1 and all(len(x) <= 16 for x in pieces) and b"".join(pieces) == body     # in chunks, the same bytes
    stream.close()
    o, = fake.opened
    assert o["r"].closed and o["query"] == "" and o["timeout"] == docstore.READ_S             # a signed GET, no link
    assert o["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert "Location" not in h and not any("X-Amz" in str(v) for v in h.values())
    code, raw, h = get(api, f"/api/cad/preplan/docs/{did}/plan", "", VAL)
    assert code == 200 and raw == fake.objects[key.replace("original.png", "plan.png")][0] and h["X-Content-Type-Options"] == "nosniff"
    code, raw, h = get(api, f"/api/cad/preplan/docs/{did}/file", "dl=1", VAL)
    assert h["Content-Disposition"] == f'attachment; filename="site-doc-{did}.png"'
    assert ppd.STREAMS._value == 12 and ppd.READS._value == 4            # every slot given back
    assert call(api, f"/api/cad/preplan/docs/{did}", {"action": "delete"}, JULIAN)[0] == 200
    assert fake.objects == {}
    assert get(api, f"/api/cad/preplan/docs/{did}/file", "", VAL)[0] == 404


def test_no_presigned_link_is_ever_made_for_a_document():
    root = Path(__file__).resolve().parent.parent / "sizeup"
    for f in ("docstore.py", "cad/preplan_docs.py", "cad/api.py", "web/server.py", "web/static/preplan-docs.js"):
        src = (root / f).read_text()
        assert "presign(" not in src and "X-Amz-Expires" not in src and "response-content-disposition" not in src, f
    assert not [p for p in root.rglob("*.py") if "SIZEUP_PREPLAN_DOCS_LINKS" in p.read_text()]
    assert not hasattr(docstore.S3DocStore, "link")


def test_downloads_at_once_are_bounded_and_plans_hold_a_slot_only_while_read(api, monkeypatch):
    monkeypatch.setattr(ppd, "DOWNLOAD_WAIT_S", 0.05)
    monkeypatch.setattr(ppd, "STREAMS", threading.BoundedSemaphore(2))
    monkeypatch.setattr(ppd, "READS", threading.BoundedSemaphore(1))
    did = upload(api, png())[1]["doc"]["id"]
    held = []
    for _ in range(2):
        code, stream, _, _ = api.get(f"/api/cad/preplan/docs/{did}/file", "", who(VAL))
        assert code == 200 and stream.deadline > 0
        held.append(stream)                                              # a slow client: still sending
    code, raw, ctype, h = api.get(f"/api/cad/preplan/docs/{did}/file", "", who(VAL))
    assert code == 503 and h["Retry-After"] and "Busy" in json.loads(raw)["error"]
    for _ in range(3):                                                   # the plan: read whole, its slot back at once
        code, body, ctype, _ = api.get(f"/api/cad/preplan/docs/{did}/plan", "", who(VAL))
        assert code == 200 and ppd.READS._value == 1
        assert body.read_all().startswith(b"\x89PNG") and ppd.PLAN_BUDGET.used == 0
    held.pop().close()                                                   # one done (or dropped)
    code, stream, _, _ = api.get(f"/api/cad/preplan/docs/{did}/file", "", who(VAL))
    assert code == 200
    import time as _time
    assert abs(stream.deadline - (_time.monotonic() + ppd.download_s(stream.length))) < 5
    stream.close()
    for x in held:
        x.close()
    assert ppd.STREAMS._value == 2
    assert ppd.DOWNLOAD_WRITE_S <= 60


def test_a_client_gone_before_its_headers_gives_its_slot_back(api, monkeypatch):
    """#464 re-review A: a reset while the headers are written still closes the stream (the slot, the store's read)."""
    from sizeup.web.server import WebServer
    monkeypatch.setattr(ppd, "STREAMS", threading.BoundedSemaphore(2))
    monkeypatch.setattr(ppd, "DOWNLOAD_WAIT_S", 0.05)
    did = upload(api, png())[1]["doc"]["id"]
    srv = WebServer("127.0.0.1", 0, {}, dict, lambda cid: None, lambda i: False, cad=api)
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    cls = srv.httpd.RequestHandlerClass
    real = cls.end_headers

    def reset(self):
        raise ConnectionResetError("the client reset")
    try:
        cls.end_headers = reset
        for _ in range(5):                                               # more than there are slots
            conn = http.client.HTTPConnection("127.0.0.1", srv.httpd.server_address[1], timeout=10)
            conn.request("GET", f"/api/cad/preplan/docs/{did}/file")
            with pytest.raises((http.client.HTTPException, OSError)):
                conn.getresponse().read()
            conn.close()
        cls.end_headers = real
        assert wait_for(lambda: ppd.STREAMS._value == 2)                  # every slot back (the handlers' finally)
        conn = http.client.HTTPConnection("127.0.0.1", srv.httpd.server_address[1], timeout=10)
        conn.request("GET", f"/api/cad/preplan/docs/{did}/file")
        assert conn.getresponse().status == 200
    finally:
        cls.end_headers = real
        srv.httpd.shutdown()


def test_a_failing_store_read_gives_its_slot_back(api, monkeypatch):
    import http.client as hc
    did = upload(api, png())[1]["doc"]["id"]
    st = api.cad.preplan_docs.store
    for part in ("file", "plan"):
        for err in (hc.IncompleteRead(b""), ValueError("bad"), docstore.DocStoreError("S3 GET: 500")):
            def boom(key, err=err):
                raise err
            monkeypatch.setattr(st, "open", boom)
            if isinstance(err, docstore.DocStoreError):
                assert api.get(f"/api/cad/preplan/docs/{did}/{part}", "", who(VAL))[0] == 400
            else:
                with pytest.raises(type(err)):                              # (the server answers 500, the slot is back)
                    api.get(f"/api/cad/preplan/docs/{did}/{part}", "", who(VAL))
    assert ppd.STREAMS._value == 12 and ppd.READS._value == 4
    s3, fake = s3_store()

    def bad_opener(url, headers, timeout):
        raise hc.BadStatusLine("x")
    s3.opener = bad_opener
    with pytest.raises(docstore.DocStoreError):
        s3.open("preplan-docs/0123456789abcdef/original.png")


def test_without_storage_uploads_say_so(api):
    api.cad.preplan_docs.store = None
    code, out = upload(api, png())
    assert code == 400 and "not configured" in out["error"]
    assert get(api, "/api/cad/preplan/docs", f"incident={api.iid}", VAL)[1]["configured"] is False


def test_from_env(tmp_path, monkeypatch):
    for k in ("SIZEUP_PREPLAN_DOCS_BUCKET", "SIZEUP_PREPLAN_DOCS_DIR", "SIZEUP_PREPLAN_DOCS_PREFIX"):
        monkeypatch.delenv(k, raising=False)
    assert docstore.from_env(log=lambda m: None) is None                  # a hub without the bucket
    assert isinstance(docstore.from_env(log=lambda m: None, default_dir=tmp_path), docstore.FsDocStore)
    monkeypatch.setenv("SIZEUP_PREPLAN_DOCS_BUCKET", "example-bucket")
    st = docstore.from_env(log=lambda m: None)
    assert isinstance(st, docstore.S3DocStore) and st.prefix == "preplan-docs"
    monkeypatch.setenv("SIZEUP_PREPLAN_DOCS_PREFIX", "../x")
    assert docstore.from_env(log=lambda m: None) is None
    with pytest.raises(ValueError):
        docstore.FsDocStore(tmp_path).key("../../etc", "original.png")


# -- the building, the call, the overlay -------------------------------------------------------------------------------------

def test_a_call_lists_its_buildings_documents_in_order_and_never_a_neighbours(api):
    c = api.cad
    upload(api, png(), title="Second floor", floor="2")
    upload(api, png(), title="Basement", floor="B1")
    upload(api, pdf(), kind="hazmat", title="Tier II", floor="")
    upload(api, png(), title="Ground", floor="1")
    other = at(c, 102)
    upload(api, png(), title="Next door", incident_id=other)
    docs = get(api, "/api/cad/preplan/docs", f"incident={api.iid}", VAL)[1]["docs"]
    assert [d["title"] for d in docs] == ["Basement", "Ground", "Second floor", "Tier II"]
    d = docs[0]
    assert (d["bldg_key"], d["building_ref"], d["by"], d["plan"], d["corners"]) == (
        "100|example ave|port chester", "buildings/7100", "Julian", True, None)
    assert [d["title"] for d in get(api, "/api/cad/preplan/docs", "where=100+Example+Ave", VAL)[1]["docs"]][:1] == ["Basement"]
    assert [d["title"] for d in get(api, "/api/cad/preplan/docs", f"incident={other}", VAL)[1]["docs"]] == ["Next door"]
    # a call with no address at all, its pin on the building: the footprint's documents, "nearby"
    pin = c.store.create_incident(type="STRU", lat=lat_of(100) + 0.00001, lon=LON)
    near = get(api, "/api/cad/preplan/docs", f"incident={pin}", VAL)[1]["docs"]
    assert len(near) == 4 and all(x["nearby"] for x in near)


def test_align_saves_four_corners_audited_and_validated(api):
    c = api.cad
    did = upload(api, png())[1]["doc"]["id"]
    out = get(api, f"/api/cad/preplan/docs/{did}", "", VAL)[1]
    assert out["footprint"]["type"] == "Polygon"                          # the snap's footprint
    corners = box_corners(lat_of(100), LON)
    code, out = call(api, f"/api/cad/preplan/docs/{did}", {"corners": corners}, JULIAN)
    assert code == 200 and out["changed"] == ["corners"] and out["doc"]["aligned_by"] == "Julian"
    assert out["doc"]["corners"] == [[round(x, 8) for x in p] for p in corners]
    mirrored = [corners[1], corners[0], corners[3], corners[2]]
    crossed = [corners[0], corners[2], corners[1], corners[3]]
    far = [[x + 0.02, y] for x, y in corners]
    tiny = box_corners(lat_of(100), LON, 0.000001, 0.000001)
    for bad in (mirrored, crossed, far, tiny, corners[:3], [["a", 1]] * 4, [[float("nan"), 41]] * 4):
        assert call(api, f"/api/cad/preplan/docs/{did}", {"corners": bad}, JULIAN)[0] == 400, bad
    doc = call(api, f"/api/cad/preplan/docs/{did}", {"corners": None}, JULIAN)[1]["doc"]
    assert doc["corners"] is None
    other = upload(api, pdf(), kind="contacts", title="Owner", floor="")[1]["doc"]["id"]
    assert call(api, f"/api/cad/preplan/docs/{other}", {"corners": corners}, JULIAN)[0] == 400   # not a plan
    audit = c.store.q("SELECT * FROM settings_audit WHERE section = ? ORDER BY id", (ppd.SECTION,))
    assert [a["key"] for a in audit][:3] == [f"{did} Floor plan"] * 3
    assert json.loads(audit[0]["new"])["title"] == "First floor"
    assert json.loads(audit[1]["new"])["corners"] and json.loads(audit[1]["old"]) == {"corners": None}
    acts = [r["summary"] for r in c.activity.rows(kind="action")]
    assert f"Aligned site doc {did}" in acts and any(s.startswith(f"Uploaded site doc {did} (Floor plan)") for s in acts)


def test_every_upload_edit_alignment_and_delete_is_audited(api):
    c = api.cad
    did = upload(api, png())[1]["doc"]["id"]
    call(api, f"/api/cad/preplan/docs/{did}", {"title": "Ground floor", "floor": "G"}, JULIAN)
    call(api, f"/api/cad/preplan/docs/{did}", {"corners": box_corners(lat_of(100), LON)}, JULIAN)
    call(api, f"/api/cad/preplan/docs/{did}", {"action": "delete"}, COREY)
    audit = c.store.q("SELECT * FROM settings_audit WHERE section = ? ORDER BY id", (ppd.SECTION,))
    assert [sorted(json.loads(a["new"])) for a in audit] == [sorted(ppd.AUDITED), ["floor", "title"], ["corners"], ["active"]]
    assert [a["operator"].split(" <")[0] for a in audit] == ["Julian", "Julian", "Julian", "Corey"]
    acts = [r["summary"] for r in c.activity.rows(kind="action")][:4]
    assert acts == [f"Deleted site doc {did}", f"Aligned site doc {did}", f"Edited site doc {did}: floor, title",
                    f"Uploaded site doc {did} (Floor plan)" + (f" from {c.store.incident(api.iid)['number']}"
                                                              if c.store.incident(api.iid)["number"] else "")]
    assert not list((Path(c.preplan_docs.store.root)).rglob("*.png"))    # the bytes are gone
    assert get(api, "/api/cad/preplan/docs", f"incident={api.iid}", COREY)[1]["docs"] == []
    sha = c.store.q("SELECT sha256 FROM preplan_docs WHERE id = ?", (did,))[0]["sha256"]
    assert sha == hashlib.sha256(docfiles.clean_png(png())[0]).hexdigest()   # the record keeps what was there


def test_no_titles_filenames_notes_or_coordinates_in_the_logs(api):
    c = api.cad
    log = []
    c.activity.log = log.append
    c.log = log.append
    code, out = upload(api, png(), title="Knox box behind blue door", floor="Mezzanine")
    did = out["doc"]["id"]
    corners = box_corners(0.5123456, 0.5432198)
    call(api, f"/api/cad/preplan/docs/{did}", {"title": "Alarm panel code room"}, JULIAN)
    call(api, f"/api/cad/preplan/docs/{did}", {"corners": [[x, y] for x, y in box_corners(lat_of(100), LON)]}, JULIAN)
    call(api, f"/api/cad/preplan/docs/{did}", {"corners": corners}, JULIAN)          # refused: too far
    get(api, f"/api/cad/preplan/docs/{did}/file", "", VAL)
    upload(api, SVG, title="evil-plan.svg")
    c.preplan_docs.store = s3_store()[0]
    c.preplan_docs.store.s3.send = lambda *a: (500, {}, b"<Code>InternalError</Code>")
    upload(api, png(), title="Secret riser room.png")                                   # a store failure
    get(api, f"/api/cad/preplan/docs/{did}/file", "", VAL)
    text = "\n".join(map(str, log))
    assert log and f"site doc {did}" in text
    for s in ("blue door", "Knox", "Mezzanine", "Alarm panel", "evil-plan", "Secret riser", ".svg", "0.512", "0.543",
              str(round(lat_of(100), 4)), str(LON)):
        assert s not in text, s


# -- the page -------------------------------------------------------------------------------------------------------------

def test_the_page_loads_the_script_and_hooks_it_in():
    html = (STATIC / "cad.html").read_text()
    assert html.index('src="/static/preplan.js"') < html.index('src="/static/preplan-docs.js"')
    grid = html[html.index('<div id="d-grid"'):html.index('<form id="edit-form"')]
    assert grid.index('id="d-preplan"') < grid.index('id="d-docs"') < grid.index('id="d-cams"')
    assert 'id="sv-pp-docs"' in html
    cad = (STATIC / "cad.js").read_text()
    assert 'if (window.PreplanDocs) PreplanDocs.card(inc, { readonly: BOARD || !canDo("dispatch") });' in cad
    src = (STATIC / "preplan-docs.js").read_text()
    assert "MapLayers.addGroup(" in src
    for f in ("cadmap.js", "map-style.js"):
        assert "preplan" not in (STATIC / f).read_text().lower(), f


def test_the_script_draws_the_plan_under_the_points_and_uploads_with_its_header():
    src = (STATIC / "preplan-docs.js").read_text()
    # the plan: an image source and a raster layer, under the pre-plan points (and so the routes, rigs and pins)
    assert 'type: "image"' in src and 'type: "raster"' in src
    assert 'const ABOVE = ["pp-edit-fp", "pp-icons", "pp-labels", "cm-lines", "cm-trail", "cm-routes", "firehouse"];' in src
    assert '"raster-opacity"' in src and "setCoordinates(" in src
    # a plan that did not load (503: the hub busy): tried again after 1, 3, 8 s (#464 re-review C)
    assert "const RETRY_MS = [1000, 3000, 8000];" in src and 'this.map.on("error"' in src and "retry()" in src
    # the upload: the file as the body, its details in X-Sizeup-Doc; opened through the hub's link
    assert '"X-Sizeup-Doc": encodeURIComponent(JSON.stringify(' in src
    assert "`/api/cad/preplan/docs/${d.id}/file`" in src
    assert "`/api/cad/preplan/docs/${d.id}/plan?v=${d.page || 0}-${d.plan_w}x${d.plan_h}`" in src   # a new page: a new URL
    assert 'accept = "application/pdf,image/png,image/jpeg,.pdf,.png,.jpg,.jpeg"' in src
    for line in src.splitlines():                                         # never a title or a corner in the console
        assert not re.search(r"console\.\w+\([^)]*\b(title|corners|lat|lon|name)\b", line), line
    assert "localStorage" in src and re.search(r"try \{[^}]*localStorage", src)


NODE_HARNESS = r"""
global.window = global;
require(process.argv[2]);
const D = window.PreplanDocs;
const out = {};
out.kinds = Object.keys(D.KINDS);
// a footprint 40 m east-west, 10 m north-south, rotated 30 degrees (counter-clockwise), at 0.5 N 0.5 E (nowhere real)
const lat0 = 0.5, lon0 = 0.5, kx = 111320 * Math.cos(lat0 * Math.PI / 180), ky = 110574;
const rot = (x, y, a) => [x * Math.cos(a) - y * Math.sin(a), x * Math.sin(a) + y * Math.cos(a)];
const a = 30 * Math.PI / 180;
const ring = [[-20, -5], [20, -5], [20, 5], [-20, 5], [-20, -5]].map(([x, y]) => { const [u, v] = rot(x, y, a); return [lon0 + u / kx, lat0 + v / ky]; });
out.obb = D.obb(ring);
out.snapWide = D.snap(ring, 800, 200, false);
out.snapTall = D.snap(ring, 200, 800, false);
out.snapNorth = D.snap(ring, 800, 200, true);
out.valid = D.valid(out.snapWide);
const c = out.snapWide;
out.moved = D.move(c, [lon0 + 10 / kx, lat0], [lon0, lat0]);
out.rotated = D.rotate(c, Math.PI / 2);
out.scaled = D.scale(c, 2);
out.corner = D.setCorner(c, 0, [c[0][0] - 1 / kx, c[0][1]]);
out.mirrored = D.valid([c[1], c[0], c[3], c[2]]);
out.size = D.sizeM(c);
out.floors = D.floorOrder(["2", "Roof", "1", "B1", "", "Mezz", "B2", "G"]);
out.pick = D.pickPlan([{ id: 1, kind: "floor_plan", floor: "2", corners: c }, { id: 2, kind: "floor_plan", floor: "1", corners: c },
                       { id: 3, kind: "site_plan", floor: "", corners: c }, { id: 4, kind: "floor_plan", floor: "B1", corners: null }]);
out.pickNone = D.pickPlan([{ id: 4, kind: "floor_plan", floor: "B1", corners: null }]);
out.count = D.countText(3);
out.before = D.beforeId(["bg", "gis-buildings", "pp-edit-fp", "pp-icons", "cm-lines"]);
out.beforeNoPP = D.beforeId(["bg", "cm-trail", "firehouse"]);
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_overlay_math_in_node(tmp_path):
    import math
    h = tmp_path / "h.js"
    h.write_text(NODE_HARNESS)
    out = json.loads(subprocess.run([NODE, str(h), str(STATIC / "preplan-docs.js")], capture_output=True, text=True,
                                    check=True, timeout=30).stdout)
    assert out["kinds"] == list(ppd.KINDS)                                   # one list, both ends
    o = out["obb"]
    assert abs(o["w"] - 40) < 0.5 and abs(o["h"] - 10) < 0.5               # the oriented box: 40 x 10 m
    assert abs(math.degrees(o["angle"]) % 180 - 30) < 1
    kx, ky = 111320 * math.cos(math.radians(0.5)), 110574

    def m(c):
        return [((x - 0.5) * kx, (y - 0.5) * ky) for x, y in c]
    tl, tr, br, bl = m(out["snapWide"])
    assert abs(math.hypot(tr[0] - tl[0], tr[1] - tl[1]) - 40) < 0.5        # the image's width along the long side
    assert abs(math.hypot(bl[0] - tl[0], bl[1] - tl[1]) - 10) < 0.5
    assert tl[1] > bl[1]                                                    # its top is the north side
    assert abs(math.degrees(math.atan2(tr[1] - tl[1], tr[0] - tl[0])) - 30) < 1
    tl, tr, br, bl = m(out["snapTall"])                                       # a tall image: its height along the long side
    assert abs(math.hypot(bl[0] - tl[0], bl[1] - tl[1]) - 40) < 0.5
    tl, tr, br, bl = m(out["snapNorth"])                                      # north up: the footprint's lon/lat box
    assert abs(tl[1] - tr[1]) < 0.01 and abs(tl[0] - bl[0]) < 0.01 and tr[0] > tl[0] and tl[1] > bl[1]
    assert out["valid"] is True and out["mirrored"] is False
    assert all(abs((a[0] - b[0]) * kx - 10) < 0.01 for a, b in zip(out["moved"], out["snapWide"]))
    w, h = out["size"]
    assert abs(w - 40) < 0.5 and abs(h - 10) < 0.5
    rt = m(out["rotated"])
    assert abs(math.degrees(math.atan2(rt[1][1] - rt[0][1], rt[1][0] - rt[0][0])) - 120) < 1
    sc = m(out["scaled"])
    assert abs(math.hypot(sc[1][0] - sc[0][0], sc[1][1] - sc[0][1]) - 80) < 0.5
    assert out["corner"][1:] == out["snapWide"][1:] and out["corner"][0] != out["snapWide"][0]
    assert out["floors"] == ["B2", "B1", "G", "1", "Mezz", "2", "Roof", ""]
    assert out["pick"] == 2 and out["pickNone"] is None                     # floor 1 first; an unaligned plan: none
    assert out["count"] == "Site docs (3)"
    assert out["before"] == "pp-edit-fp" and out["beforeNoPP"] == "cm-trail"
    py = sorted(["2", "Roof", "1", "B1", "", "Mezz", "B2", "G"], key=ppd.floor_key)
    assert py == out["floors"]                                              # the hub's order and the page's agree


# -- the security review of #464 (each a regression test) ----------------------------------------------------------------

def objstm_pdf(action: bytes):
    """A PDF 1.5 whose catalog and its /OpenAction live in a compressed object stream (FlateDecode), found through a
    cross-reference stream: nothing active is visible in the raw bytes."""
    inner = [(1, b"<< /Type /Catalog /Pages 2 0 R /OpenAction 4 0 R >>"), (4, action)]
    head, body = b"", b""
    for n, o in inner:
        head += f"{n} {len(body)} ".encode()
        body += o + b" "
    raw = head + body
    packed = zlib.compress(raw)
    objs = {2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
            5: b"<< /Type /ObjStm /N 2 /First " + str(len(head)).encode() + b" /Filter /FlateDecode /Length "
               + str(len(packed)).encode() + b" >>\nstream\n" + packed + b"\nendstream"}
    out, offs = b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n", {}
    for n in (2, 3, 5):
        offs[n] = len(out)
        out += f"{n} 0 obj\n".encode() + objs[n] + b"\nendobj\n"
    offs[6] = len(out)
    rows = [(0, 0, 65535), (2, 5, 0), (1, offs[2], 0), (1, offs[3], 0), (2, 5, 1), (1, offs[5], 0), (1, offs[6], 0)]
    xs = b"".join(struct.pack(">BIH", *r) for r in rows)
    out += (b"6 0 obj\n<< /Type /XRef /Size 7 /W [1 4 2] /Root 1 0 R /Length " + str(len(xs)).encode()
            + b" >>\nstream\n" + xs + b"\nendstream\nendobj\n")
    return out + f"startxref\n{offs[6]}\n%%EOF\n".encode()


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_actions_hidden_in_a_compressed_object_stream_never_reach_the_stored_copy(api):
    for action in (b"<< /S /JavaScript /JS (app.alert(1)) >>", b"<< /S /URI /URI (https://example.com/) >>",
                   b"<< /S /Launch /F (x) >>", b"<< /S /Named /N /Print >>", b"<< /D [3 0 R /Fit] /S /GoTo >>",
                   b"[3 0 R /Fit]"):
        data = objstm_pdf(action)
        docfiles.check_pdf(data)                                   # the raw bytes show nothing
        assert b"JavaScript" not in data and b"/URI" not in data
        code, out = upload(api, data, kind="inspection", title="Annual", floor="")
        assert code == 200, (out, action)
        clean = inflated(stored_pdf(api, out["doc"]["id"]))
        for name in ACTIVE_NAMES:
            assert name not in clean, (name, action)
    with pytest.raises(docfiles.Refused):                          # an attachment inside one: refused all the same
        docfiles.render(objstm_pdf(b"<< /Type /Filespec /F (x) /EF << /F 3 0 R >> >>"), 0)


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_the_stored_pdf_is_saved_again_without_its_metadata(api):
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(300, 200)
    w.add_metadata({"/Author": "Example Author", "/Title": "Example inner title"})
    b = io.BytesIO()
    w.write(b)
    raw = b.getvalue()
    assert b"Example Author" in raw
    code, out = upload(api, raw, kind="inspection", title="Annual", floor="")
    assert code == 200, out
    stored = next(Path(api.cad.preplan_docs.store.root).rglob("original.pdf")).read_bytes()
    assert stored.startswith(b"%PDF-") and b"Example Author" not in stored and b"Example inner title" not in stored
    assert api.cad.store.q("SELECT sha256 FROM preplan_docs")[0]["sha256"] == hashlib.sha256(stored).hexdigest()


def test_without_a_pdf_reader_pdfs_are_refused(api, monkeypatch):
    monkeypatch.setattr(docfiles, "can_render", lambda: False)
    code, out = upload(api, pdf(), kind="inspection", title="Annual", floor="")
    assert code == 400 and "PDF" in out["error"]


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_the_child_gets_none_of_the_hubs_environment(monkeypatch):
    monkeypatch.setenv("SIZEUP_STATION_TOKEN", "example-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example-secret")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/v2/example")
    seen = []
    real = subprocess.run

    def run(*a, **kw):
        seen.append(kw.get("env"))
        return real(*a, **kw)
    monkeypatch.setattr(docfiles.subprocess, "run", run)
    assert docfiles.render(pdf(), 1)["png"].startswith(b"\x89PNG")
    assert len(seen) == 2                                                       # the render, then the recheck
    for env in seen:
        assert env is not None and not [k for k in env if k.startswith(("SIZEUP_", "AWS_"))]
        assert set(env) <= {"PATH", "LC_ALL"}
    # and in the child itself
    out = real([docfiles.sys.executable, "-I", "-c", "import os, json; print(json.dumps(sorted(os.environ)))"],
               env=docfiles.child_env(), capture_output=True, text=True, check=True)
    assert not [k for k in json.loads(out.stdout) if k.startswith(("SIZEUP_", "AWS_"))]


def test_the_hub_process_is_made_non_dumpable():
    code = ("import sys; from sizeup.docfiles import no_dump; r = no_dump(); "
            "d = None\nif sys.platform.startswith('linux'):\n import ctypes; d = ctypes.CDLL(None).prctl(3, 0, 0, 0, 0)\n"
            "print(r, d)")
    out = subprocess.run([docfiles.sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=str(Path(__file__).resolve().parent.parent)).stdout.split()
    if docfiles.sys.platform.startswith("linux"):
        assert out == ["True", "0"]                                 # PR_GET_DUMPABLE: 0
    else:
        assert out == ["False", "None"]                             # a no-op elsewhere


def big_image_pdf(side=6000):
    """A page drawing one large grey image (6000 x 6000 px, FlateDecode: small on disk, 36 MB decoded)."""
    img = zlib.compress(bytes(side * side), 9)
    content = b"q 600 0 0 600 0 0 cm /Im1 Do Q"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 600] /Resources << /XObject << /Im1 5 0 R >> >> "
            b"/Contents 4 0 R >>",
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
            b"<< /Type /XObject /Subtype /Image /Width " + str(side).encode() + b" /Height " + str(side).encode()
            + b" /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length " + str(len(img)).encode()
            + b" >>\nstream\n" + img + b"\nendstream"]
    out, offs = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_a_large_image_pdf_renders_within_the_limits_or_is_refused(monkeypatch):
    data = big_image_pdf()
    try:
        r = docfiles.render(data, 1)
    except docfiles.Refused:
        r = None
    if r is not None:
        assert max(r["w"], r["h"]) <= docfiles.MAX_PX and r["w"] * r["h"] <= docfiles.RENDER_PIXELS
    assert docfiles.RLIMIT_AS_MB <= 800
    monkeypatch.setattr(docfiles, "RENDER_PIXELS", 1000)               # the pixels capped before rendering
    with pytest.raises(docfiles.Refused):
        docfiles.render(pdf(), 1)


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_one_render_at_a_time(monkeypatch):
    monkeypatch.setattr(docfiles, "RENDER_WAIT_S", 0.05)
    assert docfiles._RENDER.acquire(timeout=1)
    try:
        with pytest.raises(docfiles.Refused, match="Busy"):
            docfiles.render(pdf(), 1)
    finally:
        docfiles._RENDER.release()
    assert docfiles.render(pdf(), 1)


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_another_page_clears_the_alignment_and_says_so(api):
    c = api.cad
    did = upload(api, pdf(pages=2), page=1)[1]["doc"]["id"]
    assert call(api, f"/api/cad/preplan/docs/{did}", {"corners": box_corners(lat_of(100), LON)}, JULIAN)[0] == 200
    code, out = call(api, f"/api/cad/preplan/docs/{did}", {"page": 2}, JULIAN)
    assert code == 200 and out["doc"]["corners"] is None and out["changed"] == ["corners", "page"]
    last = c.store.q("SELECT * FROM settings_audit WHERE section = ? ORDER BY id DESC LIMIT 1", (ppd.SECTION,))[0]
    assert json.loads(last["new"])["corners"] is None and json.loads(last["old"])["corners"]
    assert f"Aligned site doc {did} and edited: page" in [r["summary"] for r in c.activity.rows(kind="action")][:1]
    # a kind that isn't a plan: no picture, no corners
    out = call(api, f"/api/cad/preplan/docs/{did}", {"kind": "inspection"}, JULIAN)[1]
    assert out["doc"]["plan"] is False and out["doc"]["corners"] is None
    assert not list(Path(c.preplan_docs.store.root).rglob("plan.png"))


def test_without_a_verified_person_the_reader_is_a_viewer(api):
    c = api.cad
    did = upload(api, png(), email=COREY, admin_only=True, title="Panel")[1]["doc"]["id"]
    assert get(api, "/api/cad/preplan/docs", f"incident={api.iid}", None)[1]["docs"] == []          # all-in-one: a viewer
    assert get(api, f"/api/cad/preplan/docs/{did}/file", "", None)[0] == 404
    with pytest.raises(ppd.CadError):
        c.preplan_docs.edit(did, {"title": "x"})                                                    # no _may: a viewer
    with pytest.raises(ppd.CadError):
        c.preplan_docs.upload({"kind": "photo", "title": "x", "incident_id": api.iid, "admin_only": True}, png())
    assert c.preplan_docs.edit(did, {"title": "y", "_may": "admin"})["ok"]                         # admin: explicit


def test_the_upload_slot_is_taken_before_the_body_is_read(api, monkeypatch):
    from sizeup.web.server import WebServer
    monkeypatch.setattr(ppd, "UPLOAD_WAIT_S", 0.1)
    srv = WebServer("127.0.0.1", 0, {}, dict, lambda cid: None, lambda i: False, cad=api)
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    held = [ppd.UPLOADS.acquire(timeout=1) for _ in range(2)]
    try:
        assert all(held)
        conn = http.client.HTTPConnection("127.0.0.1", srv.httpd.server_address[1], timeout=10)
        conn.putrequest("POST", "/api/cad/preplan/docs")
        meta = quote(json.dumps({"kind": "photo", "title": "x", "incident_id": api.iid}))
        for k, v in {"X-Sizeup-Doc": meta, "Content-Length": str(20 * 1024 * 1024)}.items():
            conn.putheader(k, v)
        conn.endheaders()                                                    # 20 MB announced, never sent
        r = conn.getresponse()
        assert r.status == 503 and r.getheader("Retry-After")
    finally:
        for _ in held:
            ppd.UPLOADS.release()
        srv.httpd.shutdown()
    assert upload(api, png())[0] == 200


def test_a_delete_marks_the_row_first_and_the_sweep_takes_what_was_left(api, monkeypatch):
    c = api.cad
    st = c.preplan_docs.store
    did = upload(api, png(), kind="photo", title="Rear", floor="")[1]["doc"]["id"]
    real = st.delete

    def broken(key):
        raise docstore.DocStoreError("S3 DELETE: 503")
    monkeypatch.setattr(st, "delete", broken)
    code, out = call(api, f"/api/cad/preplan/docs/{did}", {"action": "delete"}, JULIAN)
    assert code == 200 and out["doc"]["active"] is False                       # deleted for everyone at once
    assert get(api, f"/api/cad/preplan/docs/{did}/file", "", COREY)[0] == 404
    assert len(list(Path(st.root).rglob("original.*"))) == 1                   # the bytes: still there
    assert c.store.q("SELECT purged FROM preplan_docs WHERE id = ?", (did,))[0]["purged"] is None
    monkeypatch.setattr(st, "delete", real)
    assert c.preplan_docs.sweep() == 1                                          # a deleted row: its objects go
    assert not list(Path(st.root).rglob("original.*"))
    assert c.store.q("SELECT purged FROM preplan_docs WHERE id = ?", (did,))[0]["purged"] is not None
    assert c.preplan_docs.sweep() == 0                                          # once
    assert "site doc" in "\n".join(map(str, c.logged)) and "Rear" not in "\n".join(map(str, c.logged))


def test_the_sweep_never_deletes_what_no_row_names_after_a_restore(api):
    """#464 re-review B: the database restored to an earlier point (rows gone out from under their objects): nothing of
    theirs is deleted, however old. Only deleted rows' objects, and unfinished uploads' after the margin."""
    import time as _time
    c = api.cad
    st = c.preplan_docs.store
    kept = [upload(api, png(), kind="photo", title=f"Doc {i}", floor="")[1]["doc"]["id"] for i in range(3)]
    with c.store.tx() as db:                                                   # "restored": these rows never were
        db.execute("DELETE FROM preplan_docs")
    objects = sorted(Path(st.root).rglob("original.*"))
    assert len(objects) == 3 and kept
    much_later = _time.time() + 30 * 86400
    assert c.preplan_docs.sweep(now=much_later) == 0
    assert sorted(Path(st.root).rglob("original.*")) == objects                # every object still there
    stray = Path(st.root) / "preplan-docs" / "0123456789abcdef" / "original.png"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    assert c.preplan_docs.sweep(now=much_later) == 0 and stray.exists()        # no row: never ours to delete
    # an upload that never finished: its row pending, its objects the sweep's once the margin has passed
    did = upload(api, png(), kind="photo", title="Half", floor="")[1]["doc"]["id"]
    with c.store.tx() as db:
        db.execute("UPDATE preplan_docs SET active = ? WHERE id = ?", (ppd.PENDING, did))
    assert get(api, "/api/cad/preplan/docs", f"incident={api.iid}", COREY)[1]["docs"] == []      # never shown
    assert get(api, f"/api/cad/preplan/docs/{did}/file", "", COREY)[0] == 404
    assert c.preplan_docs.sweep(now=_time.time()) == 0                          # inside the margin
    c.preplan_docs.may_write = lambda: False
    assert c.preplan_docs.sweep(now=much_later) == 0                            # only the leader
    c.preplan_docs.may_write = lambda: True
    assert c.preplan_docs.sweep(now=much_later) == 1
    assert len(list(Path(st.root).rglob("original.*"))) == 4                   # the 3 restored-away + the stray: kept
    assert ppd.SWEEP_MIN_ALLOW == 0 and ppd.SWEEP_FIRST_S >= 72 * 3600          # never in a process's first 72 h
    assert not hasattr(docstore.FsDocStore, "pages") and not hasattr(docstore.S3DocStore, "pages")   # it never lists


def test_an_upload_writes_its_row_before_its_objects(api, monkeypatch):
    c = api.cad
    st = c.preplan_docs.store
    seen = []
    real = st.put

    def put(key, body, ctype):
        seen.append([r["active"] for r in c.store.q("SELECT active FROM preplan_docs")])
        return real(key, body, ctype)
    monkeypatch.setattr(st, "put", put)
    did = upload(api, png(), kind="photo", title="Rear", floor="")[1]["doc"]["id"]
    assert seen == [[ppd.PENDING]]                                              # the row first, pending
    assert c.store.q("SELECT active FROM preplan_docs WHERE id = ?", (did,))[0]["active"] == ppd.ACTIVE

    def failing(key, body, ctype):
        raise docstore.DocStoreError("S3 PUT: 503")
    monkeypatch.setattr(st, "put", failing)
    assert upload(api, png(), kind="photo", title="Lost", floor="")[0] == 400
    row = c.store.q("SELECT * FROM preplan_docs ORDER BY id DESC LIMIT 1")[0]
    assert row["active"] == ppd.PENDING and row["purged"] is not None           # nothing left behind to sweep


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_the_hub_checks_the_childs_copy_again(monkeypatch):
    """#464 re-review D: whatever the child returns is looked at again before it is stored."""
    for name in (b"/JS", b"/JavaScript", b"/OpenAction", b"/AA", b"/Launch", b"/URI", b"/EmbeddedFile", b"/ObjStm",
                 b"/J#53"):
        with pytest.raises(docfiles.Refused):
            docfiles.recheck_pdf(b"%PDF-1.4\n1 0 obj << " + name + b" 2 0 R >> endobj\n%%EOF", 1000)
    with pytest.raises(docfiles.Refused):
        docfiles.recheck_pdf(b"%PDF-1.4\n" + b"x" * (3 << 20), 1000)              # bigger than it can be
    honest = text_pdf(b"Lobby")
    docfiles.recheck_pdf(honest, len(honest))
    real = docfiles._child

    def lying_child(mode, data, page, max_px):                                  # a child that returns something active
        info, pdf, png = real(mode, data, page, max_px)
        assert b"/Type /Page\n" in pdf
        return info, pdf.replace(b"/Type /Page\n", b"/Type /Page\n/AA << >>\n", 1), png
    monkeypatch.setattr(docfiles, "_child", lying_child)
    with pytest.raises(docfiles.Refused):
        docfiles.render(pdf(), 1)


def test_the_prefix_is_never_another_tenant_of_the_bucket(monkeypatch):
    for k in ("SIZEUP_PREPLAN_DOCS_BUCKET", "SIZEUP_PREPLAN_DOCS_DIR", "SIZEUP_PREPLAN_DOCS_PREFIX", "LITESTREAM_PATH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SIZEUP_PREPLAN_DOCS_BUCKET", "example-bucket")
    monkeypatch.setenv("LITESTREAM_PATH", "example-db")
    for bad in ("sizeup-hub", "example-db", "replay-fixtures", "cache", "other", "preplan"):
        monkeypatch.setenv("SIZEUP_PREPLAN_DOCS_PREFIX", bad)
        assert docstore.from_env(log=lambda m: None) is None, bad
    for good in ("preplan-docs", "preplan-docs-test"):
        monkeypatch.setenv("SIZEUP_PREPLAN_DOCS_PREFIX", good)
        assert docstore.from_env(log=lambda m: None).prefix == good


def test_a_stalled_upload_lets_its_slot_go(api, monkeypatch):
    """#464 re-review 9: a client that announces a body and stops sending is cut off; its upload slot is back."""
    from sizeup.web.server import WebServer
    monkeypatch.setattr(ppd, "UPLOAD_READ_S", 0.3)
    srv = WebServer("127.0.0.1", 0, {}, dict, lambda cid: None, lambda i: False, cad=api)
    threading.Thread(target=srv.httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.httpd.server_address[1], timeout=10)
        conn.putrequest("POST", "/api/cad/preplan/docs")
        meta = quote(json.dumps({"kind": "photo", "title": "x", "incident_id": api.iid}))
        for k, v in {"X-Sizeup-Doc": meta, "Content-Length": str(1024 * 1024)}.items():
            conn.putheader(k, v)
        conn.endheaders()
        conn.send(b"\x89PNG" + b"\0" * 100)                                     # a little, then nothing
        r = conn.getresponse()
        assert r.status == 408
        # the handler gives the slot back in its finally, after the answer is on its way: wait for it, a while at most
        assert wait_for(lambda: ppd.UPLOADS._value == 2)
    finally:
        srv.httpd.shutdown()
    assert ppd.UPLOAD_READ_S and ppd.UPLOAD_DEADLINE_S <= 600


def test_the_supervisor_is_made_non_dumpable_too():
    src = (Path(__file__).resolve().parent.parent / "sizeup" / "handoff.py").read_text()
    main = src[src.index("def main("):]
    assert 'import__("sizeup.docfiles", fromlist=["no_dump"]).no_dump()' in main[:1500]


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_an_images_own_metadata_is_dropped_from_the_stored_pdf(api):
    img = zlib.compress(bytes(4 * 4))
    xmp = b"<x:xmpmeta>Example Author</x:xmpmeta>"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << /XObject << /Im1 5 0 R >> >> "
            b"/Contents 4 0 R >>",
            b"<< /Length 22 >>\nstream\nq 50 0 0 50 0 0 cm /Im1 Do Q\nendstream",
            b"<< /Type /XObject /Subtype /Image /Width 4 /Height 4 /ColorSpace /DeviceGray /BitsPerComponent 8 "
            b"/Filter /FlateDecode /Metadata 6 0 R /Length " + str(len(img)).encode() + b" >>\nstream\n" + img + b"\nendstream",
            b"<< /Type /Metadata /Subtype /XML /Length " + str(len(xmp)).encode() + b" >>\nstream\n" + xmp + b"\nendstream"]
    out, offs = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    code, res = upload(api, out, kind="inspection", title="Annual", floor="")
    assert code == 200, res
    assert b"Example Author" not in inflated(stored_pdf(api, res["doc"]["id"]))


def test_plan_pictures_are_upright_and_at_most_4096_a_side(api):
    from PIL import Image
    c = api.cad
    d = upload(api, png(4200, 30), title="Long", floor="1")[1]["doc"]
    assert (d["plan_w"], d["plan_h"]) == (4096, 29) or (d["plan_w"], d["plan_h"]) == (4096, 30)
    code, out = upload(api, jpeg_with_gps(orientation=6), title="Phone photo of a plan", floor="2")
    assert code == 200, out
    assert (out["doc"]["plan_w"], out["doc"]["plan_h"]) == (48, 64)          # turned upright
    ref = c.store.q("SELECT ref FROM preplan_docs WHERE id = ?", (out["doc"]["id"],))[0]["ref"]
    plan = (Path(c.preplan_docs.store.root) / "preplan-docs" / ref / "plan.png").read_bytes()
    im = Image.open(io.BytesIO(plan))
    assert im.size == (48, 64) and not im.getexif() and not im.info.get("exif")
    assert ppd.PLAN_MAX_SIDE == docfiles.MAX_PX == 4096


def test_jfxx_thumbnails_and_other_app_segments_are_dropped():
    raw = jpeg_with_gps(orientation=1)
    jfxx = b"\xff\xe0" + struct.pack(">H", 2 + 5 + 4) + b"JFXX\0" + b"\x10\x01\x01\x00"
    app2 = b"\xff\xe2" + struct.pack(">H", 2 + 8) + b"MPF\0\0\0\0\0"
    data = raw[:2] + jfxx + app2 + raw[2:]
    clean, w, h = docfiles.clean_jpeg(data)
    assert b"JFXX" not in clean and b"MPF\0" not in clean and (w, h) == (64, 48)


def test_local_files_are_never_readable_by_others(tmp_path):
    import os
    st = docstore.FsDocStore(tmp_path)
    k = st.key("0123456789abcdef", "original.png")
    st.put(k, b"x", "image/png")
    assert os.stat(tmp_path / k).st_mode & 0o777 == 0o600


# -- the final check of #464 (each a regression test) --------------------------------------------------------------------

def test_a_table_made_by_an_earlier_build_gets_its_new_columns(tmp_path):
    """The table as 05f9f30 made it (no purged): opened again, the column is added, and upload, delete and sweep work."""
    old = ppd.SCHEMA.replace("  purged REAL,                          -- a deleted or abandoned one: when its objects were "
                             "confirmed gone\n", "")
    assert "purged" not in old
    c = make_cad(tmp_path, geocoder=fake_geocode)
    with c.store.lock:
        c.store.db.execute("DROP TABLE preplan_docs")
        c.store.db.executescript(old)
    assert "purged" not in {r["name"] for r in c.store.q("PRAGMA table_info(preplan_docs)")}
    c.store.db.close()
    c = make_cad(tmp_path, geocoder=fake_geocode)                             # a new release opens it
    make_cad(tmp_path, geocoder=fake_geocode).store.db.close()                # and again: nothing added twice
    assert "purged" in {r["name"] for r in c.store.q("PRAGMA table_info(preplan_docs)")}
    c.gis = fake_gis()
    c.log = lambda m: None
    c.preplan_docs.store = docstore.FsDocStore(tmp_path / "docs")
    iid = at(c, 100)
    d = c.preplan_docs.upload({"kind": "photo", "title": "Rear", "incident_id": iid, "_may": "dispatcher"}, png())["doc"]
    out = c.preplan_docs.edit(d["id"], {"action": "delete", "_may": "dispatcher"})
    assert out["doc"]["active"] is False
    assert c.store.q("SELECT purged FROM preplan_docs WHERE id = ?", (d["id"],))[0]["purged"] is not None
    assert not list((tmp_path / "docs").rglob("original.*"))
    assert c.preplan_docs.sweep() == 0


def test_a_delete_that_cant_record_purged_still_answers(api, monkeypatch):
    c = api.cad
    did = upload(api, png(), kind="photo", title="Rear", floor="")[1]["doc"]["id"]
    real = c.store.tx

    def tx():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(c.preplan_docs, "_purged", lambda d, real=c.preplan_docs._purged: (
        monkeypatch.setattr(c.store, "tx", tx), real(d), monkeypatch.setattr(c.store, "tx", real)))
    code, out = call(api, f"/api/cad/preplan/docs/{did}", {"action": "delete"}, JULIAN)
    assert code == 200 and out["doc"]["active"] is False                        # done, whatever the purged mark did
    monkeypatch.undo()
    assert c.store.q("SELECT active, purged FROM preplan_docs WHERE id = ?", (did,))[0]["active"] == ppd.DELETED
    assert c.preplan_docs.sweep() == 1                                          # the sweep records it


def text_pdf(label: bytes) -> bytes:
    content = b"BT /F1 24 Tf 40 40 Td (" + label + b") Tj ET"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 120] /Resources << /Font << /F1 5 0 R >> >> "
            b"/Contents 4 0 R >>",
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    return _assemble(objs)


def _assemble(objs):
    out, offs = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_names_in_page_text_or_pixels_are_not_objects(api):
    """#464 final check 2: a label "Stair A/AA" drawn on the page, or pixels that happen to spell /JS, aren't refused."""
    code, out = upload(api, text_pdf(b"Stair A/AA /URI /JS"), kind="preplan", title="Stairs", floor="")
    assert code == 200, out
    import random
    w, h = 1024, 2048                                                          # 2 MB of grey noise, uncompressed
    px = bytearray(random.Random(7).randbytes(w * h))
    px[1000:1004] = b"/JS "
    px[50000:50012] = b"/OpenAction "
    content = b"q 300 0 0 600 0 0 cm /Im1 Do Q"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 600] /Resources << /XObject << /Im1 5 0 R >> >> "
            b"/Contents 4 0 R >>",
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
            b"<< /Type /XObject /Subtype /Image /Width " + str(w).encode() + b" /Height " + str(h).encode()
            + b" /ColorSpace /DeviceGray /BitsPerComponent 8 /Length " + str(len(px)).encode() + b" >>\nstream\n"
            + bytes(px) + b"\nendstream"]
    code, out = upload(api, _assemble(objs), kind="inspection", title="Photo sheet", floor="")
    assert code == 200, out
    # a real /AA in a dictionary, before or after a stream, is still refused; a length that doesn't lead to endstream too
    clean = text_pdf(b"Lobby")
    docfiles.recheck_pdf(clean, len(clean))
    length = re.search(rb"<< /Length \d+ >>", clean).group(0)
    for bad in (clean.replace(b"/Type /Font", b"/Type /Font /AA << >>"),
                clean.replace(b"/Type /Catalog", b"/Type /Catalog /OpenAction 3 0 R"),
                clean.replace(length, b"<< /Length 5 >>"),
                clean.replace(length, b"<< /Length 9 0 R >>")):
        assert bad != clean
        with pytest.raises(docfiles.Refused):
            docfiles.recheck_pdf(bad, len(bad))


def test_plans_in_flight_are_bounded_in_bytes_and_deadlines_scale_with_size(api, monkeypatch):
    monkeypatch.setattr(ppd, "DOWNLOAD_WAIT_S", 0.05)
    did = upload(api, png())[1]["doc"]["id"]
    code, first, _, _ = api.get(f"/api/cad/preplan/docs/{did}/plan", "", who(VAL))
    n = first.length
    first.close()
    monkeypatch.setattr(ppd, "PLAN_BUDGET", ppd.ByteBudget(int(n * 1.5)))     # room for one plan's bytes
    code, held, _, _ = api.get(f"/api/cad/preplan/docs/{did}/plan", "", who(VAL))
    assert code == 200 and ppd.PLAN_BUDGET.used == n                           # held until it is sent
    code, raw, _, h = api.get(f"/api/cad/preplan/docs/{did}/plan", "", who(VAL))
    assert code == 503 and h["Retry-After"]
    assert held.read_all().startswith(b"\x89PNG") and ppd.PLAN_BUDGET.used == 0     # sent: its bytes given back
    assert api.get(f"/api/cad/preplan/docs/{did}/plan", "", who(VAL))[1].read_all().startswith(b"\x89PNG")
    assert ppd.READS._value == 4
    # no fixed short cut-off: idle 30 s (server.py), and the whole at least 10 min or the size at 100 KB/s
    assert ppd.download_s(1000) == 600 and ppd.download_s(200_000_000) == 2000
    assert ppd.DOWNLOAD_WRITE_S == 30


def hidden_objects_pdf() -> bytes:
    """#464 final check (the hole): object 9's stream bytes hold a whole catalog with an /OpenAction and the JavaScript
    action it opens; the cross-reference table points into them and the trailer's /Root is that catalog."""
    inner = (b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R /OpenAction 6 0 R >>\nendobj\n"
             b"6 0 obj\n<< /S /JavaScript /JS (app.alert(1)) >>\nendobj\n")
    out, offs = b"%PDF-1.4\n", {}
    for n, o in ((2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
                 (3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>")):
        offs[n] = len(out)
        out += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    offs[9] = len(out)
    head = b"9 0 obj\n<< /Length " + str(len(inner)).encode() + b" >>\nstream\n"
    out += head
    offs[1] = len(out)
    offs[6] = len(out) + inner.index(b"6 0 obj")
    out += inner + b"\nendstream\nendobj\n"
    xref = len(out)
    rows = [b"0000000000 65535 f \n"] + [(f"{offs[n]:010d} 00000 n \n" if n in offs else "0000000000 65535 f \n").encode()
                                         for n in range(1, 10)]
    out += b"xref\n0 10\n" + b"".join(rows)
    return out + f"trailer\n<< /Size 10 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def test_objects_hidden_in_a_streams_bytes_are_refused():
    data = hidden_objects_pdf()
    from pypdf import PdfReader
    root = PdfReader(io.BytesIO(data)).trailer["/Root"]
    assert "/OpenAction" in root                                               # a reader does open it with its action
    with pytest.raises(docfiles.Refused):
        docfiles.recheck_pdf(data, len(data))
    honest = text_pdf(b"Stair A/AA")
    docfiles.recheck_pdf(honest, len(honest))
    # an incremental update (a second xref and trailer), an xref stream, an xref offset into a stream: refused
    more = honest + b"xref\n0 1\n0000000000 65535 f \ntrailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n9\n%%EOF\n"
    stream_at = honest.index(b"stream\n") + len(b"stream\n")                  # inside the page's content stream
    for bad in (more, honest.replace(b"/Type /Font", b"/Type /XRef"),
                re.sub(rb"startxref\n\d+", f"startxref\n{stream_at}".encode(), honest)):
        with pytest.raises(docfiles.Refused):
            docfiles.recheck_pdf(bad, len(bad))


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_what_pypdf_writes_passes_the_recheck(api):
    for raw in (pdf(pages=3), text_pdf(b"Stair A/AA"), objstm_pdf(b"[3 0 R /Fit]")):
        r = docfiles.render(raw, 1)                                             # the recheck runs inside
        assert r["pdf"].startswith(b"%PDF-")
        docfiles.recheck_pdf(r["pdf"], len(raw))


def prev_hidden_pdf() -> bytes:
    """#464 (the hole after the xref check): the trailer's /Prev points into object 9's stream bytes, where an older
    xref section and trailer name a hidden catalog (1) with an /OpenAction and the JavaScript action (6) it opens. The
    main table leaves 1 and 6 out."""
    out, offs = b"%PDF-1.4\n", {}
    for n, o in ((2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
                 (3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>")):
        offs[n] = len(out)
        out += f"{n} 0 obj\n".encode() + o + b"\nendobj\n"
    offs[9] = len(out)
    one = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R /OpenAction 6 0 R >>\nendobj\n"
    six = b"6 0 obj\n<< /S /JavaScript /JS (app.alert(1)) >>\nendobj\n"
    # the stream's bytes start right after "stream\n": work out where they will land, then write the inner section
    head_len = len(b"9 0 obj\n<< /Length 0000 >>\nstream\n")
    data0 = offs[9] + head_len
    inner_x = data0 + len(one) + len(six)
    inner = (one + six + b"xref\n1 1\n" + f"{data0:010d} 00000 n \n".encode() + b"6 1\n"
             + f"{data0 + len(one):010d} 00000 n \n".encode() + b"trailer\n<< /Size 10 /Root 1 0 R >>\n")
    out += b"9 0 obj\n<< /Length " + f"{len(inner):04d}".encode() + b" >>\nstream\n" + inner + b"\nendstream\nendobj\n"
    assert out.index(b"xref", offs[9]) == inner_x
    xref = len(out)
    rows = [b"0000000000 65535 f \n"] + [(f"{offs[n]:010d} 00000 n \n" if n in offs else "0000000000 65535 f \n").encode()
                                         for n in range(1, 10)]
    out += b"xref\n0 10\n" + b"".join(rows)
    return out + f"trailer\n<< /Size 10 /Root 1 0 R /Prev {inner_x} >>\nstartxref\n{xref}\n%%EOF\n".encode()


def test_the_whole_class_of_hidden_sections_and_references_is_refused():
    data = prev_hidden_pdf()
    from pypdf import PdfReader
    assert "/OpenAction" in PdfReader(io.BytesIO(data)).trailer["/Root"]          # a reader uses the hidden catalog
    with pytest.raises(docfiles.Refused):
        docfiles.recheck_pdf(data, len(data))
    honest = text_pdf(b"Stair A/AA")
    docfiles.recheck_pdf(honest, len(honest))
    tr = re.search(rb"trailer\n<< (.*?) >>", honest)
    size = int(re.search(rb"/Size (\d+)", honest).group(1))
    for extra in (b" /Prev 9", b" /XRefStm 9", b" /Encrypt 2 0 R", b" /Example 1"):     # the trailer allow-list
        bad = honest.replace(tr.group(0), tr.group(0)[:-3] + extra + b" >>")
        with pytest.raises(docfiles.Refused):
            docfiles.recheck_pdf(bad, len(bad))
    for bad in (honest.replace(tr.group(0), tr.group(0)[:-3] + b" /Info 99 0 R >>"),            # dangling
                honest.replace(b"/Parent 2 0 R", b"/Parent 2 0 R /Example 7 0 R"),             # dangling, in a page
                honest.replace(b"/Parent 2 0 R", b"/Parent 2 3 R"),                             # the wrong generation
                honest.replace(f"/Size {size}".encode(), f"/Size {size + 1}".encode()),         # the wrong /Size
                honest.replace(b"xref\n0 ", b"xref\n1 ")):                                    # not from object 0
        assert bad != honest
        with pytest.raises(docfiles.Refused):
            docfiles.recheck_pdf(bad, len(bad))


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_honest_pypdf_output_still_passes_the_whole_recheck(api):
    import random
    w, h = 512, 1024
    px = bytearray(random.Random(11).randbytes(w * h))
    px[100:110] = b"9 0 R /JS "
    content = b"q 200 0 0 400 0 0 cm /Im1 Do Q"
    scan = _assemble([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                      b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 400] /Resources << /XObject << /Im1 5 0 R >> >> "
                      b"/Contents 4 0 R >>",
                      b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
                      b"<< /Type /XObject /Subtype /Image /Width " + str(w).encode() + b" /Height " + str(h).encode()
                      + b" /ColorSpace /DeviceGray /BitsPerComponent 8 /Length " + str(len(px)).encode()
                      + b" >>\nstream\n" + bytes(px) + b"\nendstream"])
    for raw in (pdf(pages=3), text_pdf(b"Stair A/AA"), scan):
        r = docfiles.render(raw, 1)                                             # the recheck runs inside
        docfiles.recheck_pdf(r["pdf"], len(raw))
    code, out = upload(api, scan, kind="inspection", title="Scan", floor="")
    assert code == 200, out


# -- recheck_pdf: a strict parser, not patterns -------------------------------------------------------------------------

def test_a_word_inside_a_string_is_a_string():
    """#464: "stream" inside a string once made the old pattern skip over a direct /OpenAction and a /Names JavaScript
    tree; tokenised, the string is a string and the names are names."""
    bypass = _assemble([
        b"<< /Type /Catalog /Pages 2 0 R /Note (look: stream\n) /Length 60 "
        b"/OpenAction << /S /JavaScript /JS (app.alert(1)) >> /Names << /JavaScript << /Names [(a) 3 0 R] >> >> >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /S /JavaScript /JS (app.alert(2)) >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"])
    with pytest.raises(docfiles.Refused):
        docfiles.recheck_pdf(bypass, len(bypass))
    early_end = _assemble([
        b"<< /Type /Catalog /Pages 2 0 R /Note (endstream endobj 9 0 obj << /OpenAction 1 0 R >> endobj) >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Annots [4 0 R] >>",
        b"<< /Type /Annot /Subtype /Square /Rect [0 0 10 10] /Contents (trailer 1 0 R stream \\) (nested) /AA) >>"])
    docfiles.recheck_pdf(early_end, len(early_end))                             # names inside strings: just text
    as_key = early_end.replace(b"/Subtype /Square", b"/Subtype /Square /AA << >>")
    with pytest.raises(docfiles.Refused):                                       # the same name as a real key: refused
        docfiles.recheck_pdf(as_key, len(as_key))
    escaped = early_end.replace(b"/Subtype /Square", b"/Subtype /Square /#41A << >>")
    with pytest.raises(docfiles.Refused):                                       # #xx decoded: /AA
        docfiles.recheck_pdf(escaped, len(escaped))


def test_the_parser_refuses_anything_off_the_narrow_shape():
    honest = text_pdf(b"Lobby")
    docfiles.recheck_pdf(honest, len(honest))
    length = re.search(rb"<< /Length (\d+) >>", honest)
    for bad in (honest.replace(b"%PDF-1.4", b"%PDX-1.4"),                                   # the header
                honest + b"junk",                                                              # bytes after %%EOF
                honest.replace(b"/Type /Font", b"/Type /Font {"),                             # a brace
                honest.replace(b"/Type /Font", b"/Type /Font unknownword"),                   # a word that isn't one
                honest.replace(length.group(0), b"<< /Length 2 0 R >>"),                       # an indirect /Length
                honest.replace(length.group(0), f"<< /Length {int(length.group(1)) + 1} >>".encode()),   # off by one
                honest.replace(b"endobj\n2 0 obj", b"endobj\n1 0 obj", 1),                   # an object twice
                honest.replace(b"/Type /Font", b"/Type /Font " + b"[" * 100 + b"]" * 100),   # nested too deep
                honest.replace(b"/Type /Font", b"/Type /Font /Type /Font")):                  # a key twice
        assert bad != honest
        with pytest.raises(docfiles.Refused):
            docfiles.recheck_pdf(bad, len(bad))
    for poc in (hidden_objects_pdf(), prev_hidden_pdf()):                                     # the earlier ones too
        with pytest.raises(docfiles.Refused):
            docfiles.recheck_pdf(poc, len(poc))


@pytest.mark.skipif(not HAVE_PDFIUM, reason="pypdf / pypdfium2 not installed")
def test_honest_pypdf_output_of_every_shape_passes(api):
    annotated = _assemble([
        b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Annots [4 0 R] >>",
        b"<< /Type /Annot /Subtype /Text /Rect [10 10 30 30] /Contents (trailer 1 0 R stream endstream %%EOF) >>"])
    for raw in (pdf(pages=3), pdf(pages=200), text_pdf(b"Stair A/AA"), annotated):
        r = docfiles.render(raw, 1)                                             # the recheck runs inside
        docfiles.recheck_pdf(r["pdf"], len(raw))
    code, out = upload(api, annotated, kind="preplan", title="Notes", floor="")
    assert code == 200, out
    stored = stored_pdf(api, out["doc"]["id"])
    assert b"trailer 1 0 R stream" in stored                                    # the note kept, as text


# -- the parser review of #464 ------------------------------------------------------------------------------------------

def test_no_input_is_quadratic_for_the_parser():
    """A long run of digits and a letter once took the number pattern hours (it held the GIL)."""
    import time as _time
    from sizeup import pdfcheck
    for word in (b"1" * 1_000_000 + b"x", b"1" * 1_000_000, b"." + b"1" * 1_000_000 + b"x"):
        data = b"%PDF-1.4\n1 0 obj\n" + word + b"\nendobj\n"
        t0 = _time.perf_counter()
        assert pdfcheck.check(data).startswith("bad")
        assert _time.perf_counter() - t0 < 0.5
    t0 = _time.perf_counter()
    assert pdfcheck.NUMBER.fullmatch(b"1" * 1_000_000 + b"x") is None           # the pattern itself: linear
    assert _time.perf_counter() - t0 < 0.5
    t0 = _time.perf_counter()
    big = b"%PDF-1.4\n1 0 obj\n" + b"1" * 1_000_000 + b"x\nendobj\n"
    with pytest.raises(docfiles.Refused):
        docfiles.recheck_pdf(big, len(big))
    assert _time.perf_counter() - t0 < 5


def test_only_what_pypdf_writes_comments_and_odd_spacing_included():
    honest = text_pdf(b"Lobby")
    docfiles.recheck_pdf(honest, len(honest))
    x = re.search(rb"startxref\n(\d+)\n", honest)
    for bad in (honest.replace(b"1 0 obj\n", b"1 %x\n0 obj\n", 1),                      # a comment in a head
                honest.replace(b"/Type /Font", b"/Type /Font %x\n"),                     # a comment in a value
                honest.replace(b"1 0 obj\n", b"1  0 obj\n", 1),                         # two spaces
                honest.replace(b"1 0 obj\n", b"01 0 obj\n", 1),                         # a leading zero
                honest.replace(b"\nendobj\n2 0 obj", b" endobj\n2 0 obj", 1),          # endobj not on its line
                honest.replace(b"\nendobj\n2 0 obj", b"\nendobj\n\n2 0 obj", 1),     # a gap between objects
                honest.replace(x.group(0), b"startxref\n+" + x.group(1) + b"\n"),       # startxref with a sign
                honest.replace(x.group(0), b"startxref\n0" + x.group(1) + b"\n"),       # with a leading zero
                honest.replace(b"%%EOF\n", b"%%EOF\n%%EOF\n")):                         # something after %%EOF
        assert bad != honest
        with pytest.raises(docfiles.Refused):
            docfiles.recheck_pdf(bad, len(bad))


def test_a_slow_check_is_a_refusal_and_the_hub_stays_free(monkeypatch):
    import time as _time
    monkeypatch.setattr(docfiles, "RECHECK_S", 0.3)
    slow = b"%PDF-1.4\n1 0 obj\n[" + b"1 " * 3_000_000 + b"]\nendobj\n"           # millions of tokens: seconds
    out = {}

    def run():
        t0 = _time.perf_counter()
        try:
            docfiles.recheck_pdf(slow, len(slow))
            out["r"] = "passed"
        except docfiles.Refused:
            out["r"] = "refused"
        out["s"] = _time.perf_counter() - t0
    th = threading.Thread(target=run)
    th.start()
    n, t0 = 0, _time.perf_counter()
    while _time.perf_counter() - t0 < 0.2:                                     # the hub's own threads keep running
        n += 1
    th.join(10)
    assert out == {"r": "refused", "s": out["s"]} and out["s"] < 3
    assert n > 10_000
    assert docfiles._RENDER._value == 1                                         # the render slot back
