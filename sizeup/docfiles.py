"""What a site document upload may be (#459, sizeup/cad/preplan_docs.py): the type from its first bytes, never its name;
photos without their metadata; a PDF page rendered to a picture for the floor-plan overlay.

    sniff(data)          "image/png" | "image/jpeg" | "application/pdf", else Refused. The magic bytes decide: an SVG,
                         HTML, a GIF, WebP, HEIC, a ZIP or an Office file is refused whatever it is called.
    clean_jpeg(data)     the JPEG's segments without EXIF (GPS, the camera, the time), XMP, IPTC, comments or anything
                         after its end; the orientation alone is kept (a fresh EXIF block holding only that tag), so a
                         phone photo stands the right way up. Its colour profile (ICC) and the JFIF / Adobe markers stay.
    clean_png(data)      the PNG's image chunks only (IHDR PLTE IDAT IEND and the colour ones): no text, no eXIf, no
                         time, nothing after IEND; every chunk's CRC checked.
    check_pdf(data)      a PDF with no script or anything that runs or opens something else (JavaScript, Launch,
                         embedded files, XFA, rich media, form submission, remote go-tos), not encrypted; its names'
                         #xx escapes undone before they are compared. render() asks PDFium too (document scripts).
    render(data, page)   the page as a PNG, at most MAX_PX a side, from PDFium (pypdfium2) in a child process (-I, CPU
                         and memory limits, a timeout): a malformed PDF can crash or stall that process, not the hub.
                         None when pypdfium2 isn't installed here (the hub image has it: Dockerfile, pyproject "docs").

Pure standard library here; the child process needs pypdfium2 and numpy.
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import zlib

MIMES = {"image/png": "png", "image/jpeg": "jpg", "application/pdf": "pdf"}
MAX_SIDE = 20000                 # an image's width or height, at most
MAX_PIXELS = 100_000_000         # and its pixels (a browser decoding a bomb)
MAX_PX = 4096                    # a rendered PDF page's longer side (every iPad's WebGL takes 4096)
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
            keep = (m == 0xE0 and seg[:5] in (b"JFIF\0", b"JFXX\0")) or (m == 0xE2 and seg.startswith(b"ICC_PROFILE\0")) \
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

# names in a PDF that run something, open something else or carry another file
PDF_ACTIVE = {b"JavaScript", b"JS", b"Launch", b"EmbeddedFile", b"EmbeddedFiles", b"RichMedia", b"XFA", b"SubmitForm",
              b"ImportData", b"GoToR", b"GoToE", b"Sound", b"Movie", b"Encrypt"}
NAME = re.compile(rb"/([^\s/<>\[\]()%{}]{1,127})")
HEX = re.compile(rb"#([0-9A-Fa-f]{2})")


def check_pdf(data: bytes) -> None:
    if data[:5] != b"%PDF-":
        raise Refused("not a PDF")
    if b"%%EOF" not in data[-2048:]:
        raise Refused("not a whole PDF")
    for m in NAME.finditer(data):
        name = HEX.sub(lambda h: bytes((int(h.group(1), 16),)), m.group(1)) if b"#" in m.group(1) else m.group(1)
        if name in PDF_ACTIVE:
            raise Refused("a PDF with scripts, attachments, links out or encryption is refused: print it to a new PDF")


# The child process: stdin the PDF, argv [page (1-based, 0: check only), max px]; stdout a JSON line {pages, js, form,
# w, h} then the PNG. Limits first, so nothing it reads can take the hub's memory or CPU.
RENDER_SRC = r"""
import json, struct, sys, zlib
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (1536 << 20, 1536 << 20))
    except (ValueError, OSError):
        pass
except ImportError:
    pass
import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as raw
page, max_px = int(sys.argv[1]), int(sys.argv[2])
doc = pdfium.PdfDocument(sys.stdin.buffer.read())
info = {"pages": len(doc), "js": int(raw.FPDFDoc_GetJavaScriptActionCount(doc.raw)), "form": int(raw.FPDF_GetFormType(doc.raw))}
png = b""
if page > len(doc):
    info["nopage"] = 1
elif page >= 1:
    p = doc[page - 1]
    w, h = p.get_size()
    scale = min(max_px / max(w, h, 1.0), 4.0)
    a = p.render(scale=scale, rev_byteorder=True).to_numpy()
    if a.ndim == 2:
        a = np.stack([a] * 3, axis=-1)
    a = np.ascontiguousarray(a[:, :, :3])
    hh, ww = a.shape[:2]
    rows = np.concatenate([np.zeros((hh, 1), np.uint8), a.reshape(hh, ww * 3)], axis=1)
    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", ww, hh, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows.tobytes(), 6)) + chunk(b"IEND", b""))
    info.update(w=ww, h=hh)
sys.stdout.buffer.write(json.dumps(info).encode() + b"\n" + png)
"""


def can_render() -> bool:
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        return False
    return True


def render(data: bytes, page: int = 1, max_px: int = MAX_PX) -> dict | None:
    """{pages, w, h, png} for page (1-based; 0: only check and count the pages); None without pypdfium2. Refused for a
    PDF PDFium can't open, with document scripts or XFA, or without that page."""
    if not can_render():
        return None
    try:
        r = subprocess.run([sys.executable, "-I", "-c", RENDER_SRC, str(int(page)), str(int(max_px))], input=data,
                           capture_output=True, timeout=RENDER_S, check=False)
    except subprocess.TimeoutExpired:
        raise Refused("the PDF took too long to read") from None
    head, _, png = r.stdout.partition(b"\n")
    try:
        info = json.loads(head) if r.returncode == 0 and head else None
    except ValueError:
        info = None
    if not isinstance(info, dict):
        raise Refused("the PDF couldn't be read")
    if info.get("nopage"):
        raise Refused(f"page: 1 to {info.get('pages')}")
    if info.get("js") or info.get("form") in (2, 3):            # FORMTYPE_XFA_FULL / _FOREGROUND
        raise Refused("a PDF with scripts or XFA forms is refused: print it to a new PDF")
    if page >= 1 and not png.startswith(b"\x89PNG"):
        raise Refused("the PDF couldn't be read")
    return {"pages": int(info["pages"]), "w": info.get("w"), "h": info.get("h"), "png": png}
