"""Minimal stdlib reader for ESRI shapefiles (.shp polylines/points + .dbf attributes).

Only what the gazetteer builder needs for the Census TIGER/Line files.
"""

from __future__ import annotations

import struct
from typing import Iterator


def read_dbf(data: bytes, encoding: str = "latin-1") -> list[dict[str, str]]:
    n_records, header_len, record_len = struct.unpack("<IHH", data[4:12])
    fields: list[tuple[str, int]] = []
    pos = 32
    while data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\0")[0].decode("ascii")
        fields.append((name, data[pos + 16]))
        pos += 32
    out = []
    for r in range(n_records):
        off = header_len + r * record_len
        if data[off:off + 1] == b"*":          # deleted record
            out.append({})
            continue
        off += 1
        rec = {}
        for name, length in fields:
            rec[name] = data[off:off + length].decode(encoding, "replace").strip()
            off += length
        out.append(rec)
    return out


def iter_shapes(data: bytes) -> Iterator[tuple[tuple[float, float, float, float] | None,
                                               list[list[tuple[float, float]]]]]:
    """Yield (bbox, parts) per record; parts are lists of (x, y) = (lon, lat).

    Handles null, point and polyline/polygon records (type 0, 1, 3, 5 and their
    Z/M variants, whose extra measures are ignored).
    """
    pos = 100
    size = len(data)
    while pos + 8 <= size:
        _num, content_words = struct.unpack(">II", data[pos:pos + 8])
        content = data[pos + 8:pos + 8 + content_words * 2]
        pos += 8 + content_words * 2
        shape_type = struct.unpack("<i", content[:4])[0]
        if shape_type == 0:
            yield None, []
        elif shape_type in (1, 11, 21):
            x, y = struct.unpack("<2d", content[4:20])
            yield (x, y, x, y), [[(x, y)]]
        elif shape_type in (3, 5, 13, 15, 23, 25):
            bbox = struct.unpack("<4d", content[4:36])
            n_parts, n_points = struct.unpack("<ii", content[36:44])
            starts = list(struct.unpack(f"<{n_parts}i", content[44:44 + 4 * n_parts]))
            p0 = 44 + 4 * n_parts
            flat = struct.unpack(f"<{2 * n_points}d", content[p0:p0 + 16 * n_points])
            pts = list(zip(flat[0::2], flat[1::2]))
            starts.append(n_points)
            yield bbox, [pts[starts[k]:starts[k + 1]] for k in range(n_parts)]
        else:
            raise ValueError(f"unsupported shape type {shape_type}")
