"""What a site document upload may be (#459, sizeup/cad/preplan_docs.py): the type from its first bytes, never its name;
photos without their metadata; a PDF page rendered to a picture for the floor-plan overlay.

    sniff(data)          "image/png" | "image/jpeg" | "application/pdf", else Refused. The magic bytes decide: an SVG,
                         HTML, a GIF, WebP, HEIC, a ZIP or an Office file is refused whatever it is called.
    clean_jpeg(data)     the JPEG's segments without EXIF (GPS, the camera, the time), XMP, IPTC, comments or anything
                         after its end; the orientation alone is kept (a fresh EXIF block holding only that tag), so a
                         phone photo stands the right way up. Its colour profile (ICC) and the JFIF / Adobe markers stay.
    clean_png(data)      the PNG's image chunks only (IHDR PLTE IDAT IEND and the colour ones): no text, no eXIf, no
                         time, nothing after IEND; every chunk's CRC checked.
    check_pdf(data)      a first look at the raw bytes (names that run or open something, #xx escapes undone); it can't see
                         inside compressed object streams: render() is the check that counts.
    render(data, page)   in a child process: pypdf walks every object (object streams inflated) and refuses any action
                         that runs, opens or submits something, /AA, /JS, an action as /OpenAction, embedded files, XFA,
                         encryption; then saves the pages again without metadata (the copy that is stored); PDFium draws
                         the page as a PNG, at most MAX_PX a side. None when pypdf / pypdfium2 aren't installed (the hub
                         image has them: Dockerfile, pyproject "docs"): a PDF is then refused.
    plan_image(data)     in the same child: a plan picture the right way up (EXIF orientation), at most MAX_PX a side.
    The child: -I, an environment of its own (child_env(): no secrets), niced, RLIMIT_CPU and RLIMIT_AS, one at a time,
    a timeout; a malformed file can crash or stall it, not the hub. no_dump(): the hub's memory and environ not readable
    by it (the app and the hot/cold supervisor, sizeup/handoff.py, both call it). Accepted residual risk: the
    container's init process (initProcessEnabled) and Litestream run as the same user and stay dumpable, so a compromised
    child could read their /proc/<pid>/environ: every SSM secret the task gets, and the container-credentials URI (the
    task role's credentials). The child itself starts with none of them; a renderer under its own user or in its own
    task is a follow-up (#505).
    recheck_pdf(): the hub looks at the child's copy again before it is stored.
    Not removed: EXIF inside a PDF's own JPEG (DCTDecode) images; an image's or form's /Metadata (XMP) is.

Pure standard library here; the child process needs pypdf, pypdfium2 and Pillow.
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import threading
import zlib

MIMES = {"image/png": "png", "image/jpeg": "jpg", "application/pdf": "pdf"}
MAX_SIDE = 20000                 # an image's width or height, at most
MAX_PIXELS = 100_000_000         # and its pixels (a browser decoding a bomb)
MAX_PX = 4096                    # a plan's longer side, rendered or scaled (every iPad's WebGL takes 4096)
RENDER_S = 45.0                  # the child process, at most


class Refused(ValueError):
    """Why an upload is refused (said to the person: never its name or title)."""


def sniff(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    head = data[:512].lstrip().lower()
    if head.startswith((b"<?xml", b"<svg", b"<!doctype", b"<html", b"<")) or b"<svg" in head or b"<script" in head:
        raise Refused("SVG, HTML and XML are refused: PDF, PNG or JPEG only")
    raise Refused("PDF, PNG or JPEG only (by the file's content, not its name)")


def _check_size(w: int, h: int) -> None:
    if not (0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE) or w * h > MAX_PIXELS:
        raise Refused(f"image: at most {MAX_SIDE} px a side and {MAX_PIXELS // 1_000_000} MP")


# -- JPEG -----------------------------------------------------------------------------------------------------------------

SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _orientation(app1: bytes) -> int:
    """EXIF orientation (1-8) from an APP1 payload, 1 when absent or unreadable."""
    if not app1.startswith(b"Exif\0\0") or len(app1) < 14:
        return 1
    t = app1[6:]
    end = {b"II": "<", b"MM": ">"}.get(t[:2])
    if end is None:
        return 1
    try:
        (ifd,) = struct.unpack(end + "I", t[4:8])
        (n,) = struct.unpack(end + "H", t[ifd:ifd + 2])
        for i in range(min(n, 200)):
            e = t[ifd + 2 + 12 * i: ifd + 14 + 12 * i]
            tag, typ, _cnt = struct.unpack(end + "HHI", e[:8])
            if tag == 0x0112 and typ == 3:
                v = struct.unpack(end + "H", e[8:10])[0]
                return v if 1 <= v <= 8 else 1
    except struct.error:
        return 1
    return 1


def _orientation_app1(v: int) -> bytes:
    """A fresh APP1 holding only the orientation tag."""
    tiff = b"MM\0*" + struct.pack(">I", 8) + struct.pack(">H", 1) + struct.pack(">HHIHH", 0x0112, 3, 1, v, 0) + b"\0\0\0\0"
    payload = b"Exif\0\0" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def clean_jpeg(data: bytes) -> tuple[bytes, int, int]:
    """(the JPEG without its metadata, width, height); Refused when it isn't a whole JPEG."""
    if data[:2] != b"\xff\xd8":
        raise Refused("not a JPEG")
    out, i, n = [b"\xff\xd8"], 2, len(data)
    size, orient, scanned, ended = None, 1, False, False
    after_app0 = 1                                      # where the orientation block goes: after JFIF, else after SOI
    while i < n:
        if data[i] != 0xFF:
            raise Refused("not a whole JPEG")
        while i < n and data[i] == 0xFF:                # fill bytes
            i += 1
        if i >= n:
            break
        m = data[i]
        i += 1
        if m == 0xD9:                                   # EOI: the end; anything after it is dropped
            out.append(b"\xff\xd9")
            ended = True
            break
        if 0xD0 <= m <= 0xD7 or m == 0x01:              # standalone markers
            out.append(bytes((0xFF, m)))
            continue
        if i + 2 > n:
            raise Refused("not a whole JPEG")
        (ln,) = struct.unpack(">H", data[i:i + 2])
        if ln < 2 or i + ln > n:
            raise Refused("not a whole JPEG")
        seg = data[i + 2:i + ln]
        whole = bytes((0xFF, m)) + data[i:i + ln]
        i += ln
        if m in SOF and len(seg) >= 5:
            h, w = struct.unpack(">HH", seg[1:5])
            size = (w, h)
        if m == 0xE1:
            if seg.startswith(b"Exif\0\0"):
                orient = _orientation(seg)
            continue                                    # EXIF and XMP: gone
        if 0xE0 <= m <= 0xEF:
            keep = (m == 0xE0 and seg[:5] == b"JFIF\0") or (m == 0xE2 and seg.startswith(b"ICC_PROFILE\0")) \
                or (m == 0xEE and seg.startswith(b"Adobe"))
            if not keep:
                continue                                # IPTC, maker notes, MPF, FlashPix ...
            out.append(whole)
            if m == 0xE0:
                after_app0 = len(out)
            continue
        if m == 0xFE:                                   # a comment
            continue
        out.append(whole)
        if m == 0xDA:                                   # a scan: its entropy-coded data, to the next real marker
            scanned = True
            j = i
            while True:
                j = data.find(b"\xff", j)
                if j < 0 or j + 1 >= n:
                    raise Refused("not a whole JPEG")
                nb = data[j + 1]
                if nb == 0x00 or 0xD0 <= nb <= 0xD7 or nb == 0xFF:
                    j += 1 if nb == 0xFF else 2
                    continue
                break
            out.append(data[i:j])
            i = j
    if not (ended and scanned and size):
        raise Refused("not a whole JPEG")
    _check_size(*size)
    if orient != 1:
        out.insert(after_app0, _orientation_app1(orient))
    w, h = size
    return b"".join(out), (h if orient >= 5 else w), (w if orient >= 5 else h)


# -- PNG ------------------------------------------------------------------------------------------------------------------

PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"sBIT", b"pHYs", b"bKGD"}


def clean_png(data: bytes) -> tuple[bytes, int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise Refused("not a PNG")
    out, i, n, size, first = [data[:8]], 8, len(data), None, True
    while True:
        if i + 12 > n:
            raise Refused("not a whole PNG")
        (ln,) = struct.unpack(">I", data[i:i + 4])
        typ = data[i + 4:i + 8]
        if ln > n or i + 12 + ln > n:
            raise Refused("not a whole PNG")
        body = data[i + 8:i + 8 + ln]
        (crc,) = struct.unpack(">I", data[i + 8 + ln:i + 12 + ln])
        if zlib.crc32(typ + body) != crc:
            raise Refused("not a whole PNG (a damaged chunk)")
        if first and (typ != b"IHDR" or ln != 13):
            raise Refused("not a PNG")
        if first:
            size = struct.unpack(">II", body[:8])
            first = False
        if typ in PNG_KEEP:
            out.append(data[i:i + 12 + ln])
        i += 12 + ln
        if typ == b"IEND":
            break                                       # anything after IEND: dropped
    _check_size(*size)
    return b"".join(out), size[0], size[1]


def clean_image(data: bytes, mime: str) -> tuple[bytes, int, int]:
    return clean_png(data) if mime == "image/png" else clean_jpeg(data)


def png_size(data: bytes) -> tuple[int, int]:
    return struct.unpack(">II", data[16:24])


# -- PDF ------------------------------------------------------------------------------------------------------------------

# names in a PDF that refuse it outright (an attachment, an XFA form, encryption): a quick first look at the raw bytes
# (names inside compressed object streams aren't visible here: the child process's walk of the parsed objects is the
# check that counts). Links, actions, scripts and form fields aren't refused: the stored copy is saved without them.
PDF_ACTIVE = {b"EmbeddedFile", b"EmbeddedFiles", b"XFA", b"Encrypt"}
NAME = re.compile(rb"/([^\s/<>\[\]()%{}]{1,127})")
HEX = re.compile(rb"#([0-9A-Fa-f]{2})")
REFUSED_PDF = "a PDF with attachments, an XFA form or encryption is refused: print it to a new PDF"


def check_pdf(data: bytes) -> None:
    if data[:5] != b"%PDF-":
        raise Refused("not a PDF")
    if b"%%EOF" not in data[-2048:]:
        raise Refused("not a whole PDF")
    for m in NAME.finditer(data):
        name = HEX.sub(lambda h: bytes((int(h.group(1), 16),)), m.group(1)) if b"#" in m.group(1) else m.group(1)
        if name in PDF_ACTIVE:
            raise Refused(REFUSED_PDF)


# The child process (-I, an environment of its own: child_env()). argv [mode: pdf | image, page (1-based; 0: check only),
# max px]; stdin the file; stdout a JSON line {pages, w, h, pdf: its length, png: its length, refused?, nopage?,
# rlimit?: why a limit couldn't be set} then the sanitised PDF and the PNG.
#   pdf     pypdf parses it (object streams inflated) and every object reachable from the trailer is looked at:
#           encryption, an XFA form, an embedded file (/EmbeddedFiles, /EF, an /EmbeddedFile stream): refused. Then it
#           is saved again with its pages only, so no catalog /OpenAction, /AA, /Names (JavaScript, attachments),
#           /AcroForm, outline, /Info or XMP; on each page /AA, /Metadata and /PieceInfo go; links, media, screen and
#           attachment annotations go; every other annotation keeps its appearance without /A, /AA or its field
#           (/Parent: form fields aren't interactive any more). That copy is walked again: anything left that is an
#           action (/A, /AA, /OpenAction, /JS, /Next, an action's /S): refused (never happens with a whole PDF). That
#           copy is what is stored; PDFium renders the page from it (its own script count checked too).
#   image   Pillow turns a plan picture the right way up (its EXIF orientation), scales it to max px a side, as a PNG.
# Limits first: niced, RLIMIT_CPU, RLIMIT_AS (RLIMIT_AS_MB), the pixels to render capped before rendering.
RLIMIT_AS_MB = 700
RENDER_PIXELS = MAX_PX * MAX_PX
RENDER_SRC = r"""
import io, json, os, struct, sys, zlib
info = {}
try:
    os.nice(19)
except OSError as e:
    info["nice"] = type(e).__name__
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
    as_bytes = int(sys.argv[4]) << 20
    resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
except (ImportError, ValueError, OSError) as e:
    info["rlimit"] = type(e).__name__
mode, page, max_px = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
data = sys.stdin.buffer.read()

def out(pdf=b"", png=b""):
    info.update(pdf=len(pdf), png=len(png))
    sys.stdout.buffer.write(json.dumps(info).encode() + b"\n" + pdf + png)
    sys.exit(0)

def refuse(why):
    info["refused"] = why
    out()

def png_of(raw, w, h, n, stride):
    ctype = {1: 0, 3: 2, 4: 6}[n]
    rows = b"".join(b"\0" + raw[y * stride:y * stride + w * n] for y in range(h))
    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))

if mode == "image":
    import warnings
    from PIL import Image, ImageOps
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    Image.MAX_IMAGE_PIXELS = 100_000_000
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    im = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
    im.thumbnail((max_px, max_px), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "PNG", compress_level=6)
    info.update(w=im.width, h=im.height)
    out(png=b.getvalue())

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject
ACTION_S = {"/JavaScript", "/Launch", "/URI", "/SubmitForm", "/ImportData", "/GoToR", "/GoToE", "/Rendition", "/Sound",
            "/Movie", "/Named", "/ResetForm", "/Hide", "/SetOCGState", "/Trans", "/GoTo3DView", "/RichMediaExecute",
            "/GoTo", "/Thread"}
ACTION_KEYS = {"/A", "/AA", "/OpenAction", "/JS", "/Next"}
DROP_ANNOTS = {"/Link", "/Screen", "/Movie", "/Sound", "/RichMedia", "/FileAttachment", "/3D"}

def walk(root, look):
    stack, seen, n = [root], set(), 0
    while stack:
        o = stack.pop()
        if isinstance(o, IndirectObject):
            k = (o.idnum, o.generation)
            if k in seen:
                continue
            seen.add(k)
            o = o.get_object()
        n += 1
        if n > 200000:
            refuse("too many objects")
        if isinstance(o, DictionaryObject):
            look(o)
            stack.extend(o.values())
        elif isinstance(o, ArrayObject):
            stack.extend(o)

def attached(o):
    keys = set(o.keys())
    if keys & {"/EmbeddedFiles", "/EF", "/XFA", "/RichMediaContent"} or o.get("/Type") == "/EmbeddedFile":
        refuse("attachment")
    if o.get("/Subtype") in ("/Image", "/Form"):          # an image's or a form's own XMP: gone with the rest
        for key in ("/Metadata", "/PieceInfo"):
            if key in o:
                del o[key]

def active(o):
    if set(o.keys()) & ACTION_KEYS or o.get("/S") in ACTION_S and ("/Type" not in o or o.get("/Type") == "/Action"):
        refuse("an action left")

try:
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        refuse("encrypted")
    walk(reader.trailer, attached)
    writer = PdfWriter()
    for p in reader.pages:
        for key in ("/Metadata", "/PieceInfo", "/AA"):
            if key in p:
                del p[key]
        if "/Annots" in p:
            kept = ArrayObject()
            for a in p["/Annots"].get_object() or []:
                ad = a.get_object()
                if not isinstance(ad, DictionaryObject) or ad.get("/Subtype") in DROP_ANNOTS:
                    continue
                for key in ("/A", "/AA", "/Parent", "/Popup"):
                    if key in ad:
                        del ad[key]
                kept.append(a)
            if kept:
                p[NameObject("/Annots")] = kept
            else:
                del p["/Annots"]
        writer.add_page(p)
    writer.metadata = None
    b = io.BytesIO()
    writer.write(b)
    clean = b.getvalue()
    walk(PdfReader(io.BytesIO(clean), strict=False).trailer, active)     # what is stored: no action at all
except SystemExit:
    raise
except Exception as e:
    info["error"] = type(e).__name__
    out()

import pypdfium2 as pdfium
import pypdfium2.raw as raw
doc = pdfium.PdfDocument(clean)
info.update(pages=len(doc), js=int(raw.FPDFDoc_GetJavaScriptActionCount(doc.raw)), form=int(raw.FPDF_GetFormType(doc.raw)))
if info["js"] or info["form"] in (2, 3):
    refuse("script")
png = b""
if page > len(doc):
    info["nopage"] = 1
elif page >= 1:
    p = doc[page - 1]
    w, h = p.get_size()
    scale = min(max_px / max(w, h, 1.0), 4.0)
    if w * scale * h * scale > int(sys.argv[5]) * 1.01:
        refuse("too large")
    bm = p.render(scale=scale, rev_byteorder=True)
    png = png_of(bytes(bm.buffer), bm.width, bm.height, bm.n_channels, bm.stride)
    info.update(w=bm.width, h=bm.height)
out(clean, png)
"""

# One render at a time: the hub task has 2 GB and half a vCPU, and its dispatch comes first.
_RENDER = threading.BoundedSemaphore(1)
RENDER_WAIT_S = 20.0


def child_env() -> dict[str, str]:
    """The child's whole environment: none of the hub's (its SSM secrets, AWS credentials, tokens)."""
    return {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}


def no_dump() -> bool:
    """The hub process made non-dumpable (Linux: prctl PR_SET_DUMPABLE 0): a child running as the same user can't read
    its /proc/<pid>/environ or memory, or ptrace it. True when set; elsewhere a no-op (False)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 0, 0, 0, 0) == 0          # PR_SET_DUMPABLE
    except (OSError, AttributeError):
        return False


def can_render() -> bool:
    """PDFs can be checked and drawn here (pypdf and pypdfium2: the hub image's "docs" extra)."""
    try:
        import pypdf  # noqa: F401
        import pypdfium2  # noqa: F401
    except ImportError:
        return False
    return True


def can_plan_images() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _child(mode: str, data: bytes, page: int, max_px: int) -> tuple[dict, bytes, bytes]:
    if not _RENDER.acquire(timeout=RENDER_WAIT_S):
        raise Refused("Busy reading another file: try again in a moment")
    try:
        r = subprocess.run([sys.executable, "-I", "-c", RENDER_SRC, mode, str(int(page)), str(int(max_px)),
                            str(RLIMIT_AS_MB), str(RENDER_PIXELS)], input=data, capture_output=True, timeout=RENDER_S,
                           check=False, env=child_env())
    except subprocess.TimeoutExpired:
        raise Refused("the file took too long to read") from None
    finally:
        _RENDER.release()
    head, _, rest = r.stdout.partition(b"\n")
    try:
        info = json.loads(head) if r.returncode == 0 and head else None
    except ValueError:
        info = None
    if not isinstance(info, dict) or info.get("error"):
        raise Refused("the file couldn't be read")
    if info.get("refused") == "an action left":
        raise Refused("the PDF couldn't be cleaned: print it to a new PDF")
    if info.get("refused"):
        raise Refused(REFUSED_PDF if mode == "pdf" else "the picture couldn't be read")
    n = int(info.get("pdf") or 0)
    return info, rest[:n], rest[n:n + int(info.get("png") or 0)]


def render(data: bytes, page: int = 1, max_px: int = MAX_PX) -> dict | None:
    """{pages, w, h, pdf: the sanitised copy to store, png: the page (page >= 1), limits: why a limit wasn't set ("")}
    for page (1-based; 0: only check, clean and count); None without pypdf and pypdfium2. Refused for a PDF that can't
    be read, has anything active (see RENDER_SRC), or hasn't that page."""
    if not can_render():
        return None
    info, pdf, png = _child("pdf", data, page, max_px)
    if info.get("nopage"):
        raise Refused(f"page: 1 to {info.get('pages')}")
    if not pdf.startswith(b"%PDF-") or (page >= 1 and not png.startswith(b"\x89PNG")):
        raise Refused("the PDF couldn't be read")
    recheck_pdf(pdf, len(data))
    return {"pages": int(info["pages"]), "w": info.get("w"), "h": info.get("h"), "pdf": pdf, "png": png,
            "limits": info.get("rlimit") or ""}


CLEAN_MAX_BYTES = 64 * 1024 * 1024
RECHECK_S = 20.0                  # sizeup/pdfcheck.py, a whole check, at most (wall clock): longer is a refusal
RECHECK_AS_MB = 512


def recheck_pdf(pdf: bytes, original: int) -> None:
    """The sanitised copy, looked at again before it is stored: no bigger than 64 MB or than twice the original and a
    megabyte, then parsed whole and strictly by sizeup/pdfcheck.py (exactly the shape pypdf writes; checks on tokens:
    no active name, the trailer's keys and /Size, the xref's offsets, every reference to an object in use) in a small
    process of its own: -I, run by its path (nothing of sizeup imported), the minimal environment, niced, RLIMIT_CPU and
    RLIMIT_AS, under the one render slot, at most RECHECK_S. No input can hold the hub's CPU or its GIL; a timeout,
    a crash or anything but "ok" is a refusal."""
    bad = Refused("the PDF couldn't be cleaned: print it to a new PDF")
    if len(pdf) > min(CLEAN_MAX_BYTES, 2 * original + (1 << 20)):
        raise bad
    from sizeup import pdfcheck
    if not _RENDER.acquire(timeout=RENDER_WAIT_S):
        raise Refused("Busy reading another file: try again in a moment")
    try:
        r = subprocess.run([sys.executable, "-I", pdfcheck.__file__, str(RECHECK_AS_MB)], input=pdf, capture_output=True,
                           timeout=RECHECK_S, check=False, env=child_env())
    except subprocess.TimeoutExpired:
        raise bad from None
    finally:
        _RENDER.release()
    if r.returncode != 0 or r.stdout != b"ok":
        raise bad


def plan_image(data: bytes, max_px: int = MAX_PX) -> dict | None:
    """A plan picture (PNG / JPEG) the right way up and at most max_px a side, as a PNG: {w, h, png}; None without
    Pillow."""
    if not can_plan_images():
        return None
    info, _, png = _child("image", data, 0, max_px)
    if not png.startswith(b"\x89PNG"):
        raise Refused("the picture couldn't be read")
    return {"w": info["w"], "h": info["h"], "png": png, "limits": info.get("rlimit") or ""}
