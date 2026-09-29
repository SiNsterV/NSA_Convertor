#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Noteshelf (Android) .nsa -> PDF (template + annotations) [best-effort]

- .nsa is a ZIP
- Document.plist describes pages (uuid, pdfKitPageRect, associatedPDFFileName, associatedPDFKitPageIndex...)
- Templates/*.ns_pdf are normal PDFs (background)
- Annotations/<page_uuid> is SQLite with table "annotation"
  - ink strokes: annotationType=0, blob in stroke_segments_v3
  - blob: segmentCount * 28 bytes; each segment 7x float32 LE:
      x1,y1,x2,y2,?,pressure,?
  - tvary: annotationType=5, JSON in shape_data (controlPoints, strokeOpacity, properties.strokeThickness, ...)

Goal: produce a "flattened" PDF similar to Noteshelf export.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import sqlite3
import struct
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import fitz  # PyMuPDF

DEFAULT_CURVE_SAMPLES = 4
MIN_CURVE_SAMPLES = 2
MAX_CURVE_SAMPLES = 8
PRESSURE_WIDTH_QUANT = 0.2
SHAPE_DEDUP_TOL = 1.0

# -----------------------------
# Helpers
# -----------------------------

def parse_pdfkit_rect(rect_str: str) -> Optional[Tuple[float, float]]:
    """
    Noteshelf mívá: '{{0.0, 0.0}}, {1200.0, 1698.1512}}'
    Vrací (w, h) z posledních dvou čísel.
    """
    nums = re.findall(r"[-+]?\d*\.?\d+", rect_str or "")
    if len(nums) >= 4:
        return float(nums[2]), float(nums[3])
    return None


def extract_points_from_blob(blob: bytes) -> List[Tuple[float, float]]:
    """stroke_segments_v3: 7 float32 per segment (28 bytes). Vrací polyline body."""
    if not blob:
        return []
    if len(blob) % 28 != 0:
        blob = blob[: len(blob) - (len(blob) % 28)]
        if not blob:
            return []
    it = struct.iter_unpack("<7f", blob)
    pts: List[Tuple[float, float]] = []
    try:
        x1, y1, x2, y2, *_ = next(it)
    except StopIteration:
        return []
    pts.append((x1, y1))
    pts.append((x2, y2))
    for x1, y1, x2, y2, *_ in it:
        pts.append((x2, y2))
    return pts


def extract_segments_from_blob(blob: bytes) -> List[Tuple[float, float, float, float, float]]:
    """
    stroke_segments_v3: 7 float32 per segment (28 bytes).
    Returns list of (x1, y1, x2, y2, size) where size is the 5th float.
    """
    if not blob or len(blob) < 28:
        return []
    if len(blob) % 28 != 0:
        blob = blob[: len(blob) - (len(blob) % 28)]
        if not blob:
            return []
    segments: List[Tuple[float, float, float, float, float]] = []
    for x1, y1, x2, y2, size, _pressure, _flag in struct.iter_unpack("<7f", blob):
        segments.append((x1, y1, x2, y2, size))
    return segments


def bbox_key(color: int, bx: float, by: float, bw: float, bh: float, tol: float = SHAPE_DEDUP_TOL) -> Tuple[int, float, float, float, float]:
    if tol <= 0:
        return (color, bx, by, bw, bh)
    q = lambda v: round(v / tol) * tol
    return (color, q(bx), q(by), q(bw), q(bh))


def quantize_width(width: float, step: float = PRESSURE_WIDTH_QUANT) -> float:
    if step <= 0:
        return width
    return round(width / step) * step


def polyline_length(points: List[Tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(points)):
        x0, y0 = points[i - 1]
        x1, y1 = points[i]
        dx = x1 - x0
        dy = y1 - y0
        total += (dx * dx + dy * dy) ** 0.5
    return total


def adaptive_curve_samples(points: List[Tuple[float, float]], base: int = DEFAULT_CURVE_SAMPLES) -> int:
    length = polyline_length(points)
    if length < 200:
        samples = base - 2
    elif length < 800:
        samples = base - 1
    elif length < 2000:
        samples = base
    elif length < 4000:
        samples = base + 1
    else:
        samples = base + 2
    if samples < MIN_CURVE_SAMPLES:
        return MIN_CURVE_SAMPLES
    if samples > MAX_CURVE_SAMPLES:
        return MAX_CURVE_SAMPLES
    return samples


def rdp(points: List[Tuple[float, float]], epsilon: float) -> List[Tuple[float, float]]:
    """Ramer-Douglas-Peucker zjednodušení polyline pro hladší výsledek a menší PDF."""
    if len(points) < 3:
        return points

    (x1, y1) = points[0]
    (x2, y2) = points[-1]
    dx = x2 - x1
    dy = y2 - y1
    norm = dx * dx + dy * dy

    max_dist = -1.0
    index = -1

    for i, (x, y) in enumerate(points[1:-1], start=1):
        if norm == 0:
            dist = (x - x1) ** 2 + (y - y1) ** 2
        else:
            t = ((x - x1) * dx + (y - y1) * dy) / norm
            projx = x1 + t * dx
            projy = y1 + t * dy
            dist = (x - projx) ** 2 + (y - projy) ** 2
        if dist > max_dist:
            max_dist = dist
            index = i

    if max_dist <= epsilon * epsilon:
        return [points[0], points[-1]]

    left = rdp(points[: index + 1], epsilon)
    right = rdp(points[index:], epsilon)
    return left[:-1] + right


def catmull_rom_spline(points: List[Tuple[float, float]], samples_per_segment: int) -> List[Tuple[float, float]]:
    """Simple Catmull-Rom spline through points (uniform parameterization)."""
    if len(points) < 2 or samples_per_segment <= 1:
        return points
    if len(points) < 4:
        # Linear upsample for short strokes
        out: List[Tuple[float, float]] = []
        for i in range(len(points) - 1):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            for s in range(samples_per_segment):
                t = s / float(samples_per_segment)
                out.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
        out.append(points[-1])
        return out

    out: List[Tuple[float, float]] = []
    n = len(points)
    for i in range(n - 1):
        p0 = points[i - 1] if i - 1 >= 0 else points[i]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[i + 2] if i + 2 < n else points[i + 1]

        for s in range(samples_per_segment):
            t = s / float(samples_per_segment)
            t2 = t * t
            t3 = t2 * t
            x = 0.5 * (
                (2 * p1[0]) +
                (-p0[0] + p2[0]) * t +
                (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 +
                (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
            )
            y = 0.5 * (
                (2 * p1[1]) +
                (-p0[1] + p2[1]) * t +
                (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 +
                (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
            )
            out.append((x, y))
    out.append(points[-1])
    return out


def catmull_rom_spline_with_widths(
    points: List[Tuple[float, float]],
    widths: List[float],
    samples_per_segment: int,
) -> List[Tuple[float, float, float]]:
    """Catmull-Rom spline through points with interpolated widths."""
    if len(points) < 2 or samples_per_segment <= 1 or len(points) != len(widths):
        return [(x, y, w) for (x, y), w in zip(points, widths)]
    if len(points) < 4:
        out: List[Tuple[float, float, float]] = []
        for i in range(len(points) - 1):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            w1 = widths[i]
            w2 = widths[i + 1]
            for s in range(samples_per_segment):
                t = s / float(samples_per_segment)
                out.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, w1 + (w2 - w1) * t))
        out.append((points[-1][0], points[-1][1], widths[-1]))
        return out

    out: List[Tuple[float, float, float]] = []
    n = len(points)
    for i in range(n - 1):
        p0 = points[i - 1] if i - 1 >= 0 else points[i]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[i + 2] if i + 2 < n else points[i + 1]

        w0 = widths[i - 1] if i - 1 >= 0 else widths[i]
        w1 = widths[i]
        w2 = widths[i + 1]
        w3 = widths[i + 2] if i + 2 < n else widths[i + 1]

        for s in range(samples_per_segment):
            t = s / float(samples_per_segment)
            t2 = t * t
            t3 = t2 * t
            x = 0.5 * (
                (2 * p1[0]) +
                (-p0[0] + p2[0]) * t +
                (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 +
                (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
            )
            y = 0.5 * (
                (2 * p1[1]) +
                (-p0[1] + p2[1]) * t +
                (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 +
                (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
            )
            w = 0.5 * (
                (2 * w1) +
                (-w0 + w2) * t +
                (2 * w0 - 5 * w1 + 4 * w2 - w3) * t2 +
                (-w0 + 3 * w1 - 3 * w2 + w3) * t3
            )
            out.append((x, y, w))
    out.append((points[-1][0], points[-1][1], widths[-1]))
    return out


def segments_to_polylines(
    segments: List[Tuple[float, float, float, float, float]],
    *,
    jump_ratio: float = 4.0,
    min_jump: float = 10.0,
) -> List[List[Tuple[float, float]]]:
    """
    Convert segment list to polylines, splitting when the next segment's start
    is far from the previous segment's end.
    """
    if not segments:
        return []

    seg_lens: List[float] = []
    for x1, y1, x2, y2, _size in segments:
        seg_lens.append(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)

    med = median(seg_lens) if seg_lens else 0.0
    threshold = max(min_jump, med * jump_ratio) if med > 0 else min_jump

    polylines: List[List[Tuple[float, float]]] = []
    cur: List[Tuple[float, float]] = []
    prev_x2 = prev_y2 = None

    for x1, y1, x2, y2, _size in segments:
        if prev_x2 is not None:
            jump = ((x1 - prev_x2) ** 2 + (y1 - prev_y2) ** 2) ** 0.5
            if jump > threshold and len(cur) >= 2:
                polylines.append(cur)
                cur = []

        if not cur:
            cur.append((x1, y1))
        cur.append((x2, y2))
        prev_x2, prev_y2 = x2, y2

    if len(cur) >= 2:
        polylines.append(cur)

    return polylines


def segments_to_polylines_with_sizes(
    segments: List[Tuple[float, float, float, float, float]],
    *,
    jump_ratio: float = 4.0,
    min_jump: float = 10.0,
) -> List[Tuple[List[Tuple[float, float]], List[float]]]:
    """Convert segments to polylines with per-point sizes, splitting on large jumps."""
    if not segments:
        return []

    seg_lens: List[float] = []
    for x1, y1, x2, y2, _size in segments:
        seg_lens.append(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)

    med = median(seg_lens) if seg_lens else 0.0
    threshold = max(min_jump, med * jump_ratio) if med > 0 else min_jump

    polylines: List[Tuple[List[Tuple[float, float]], List[float]]] = []
    cur_pts: List[Tuple[float, float]] = []
    cur_sizes: List[float] = []
    prev_x2 = prev_y2 = None

    for x1, y1, x2, y2, size in segments:
        if prev_x2 is not None:
            jump = ((x1 - prev_x2) ** 2 + (y1 - prev_y2) ** 2) ** 0.5
            if jump > threshold and len(cur_pts) >= 2:
                polylines.append((cur_pts, cur_sizes))
                cur_pts = []
                cur_sizes = []

        if not cur_pts:
            cur_pts.append((x1, y1))
            cur_sizes.append(size)
        cur_pts.append((x2, y2))
        cur_sizes.append(size)
        prev_x2, prev_y2 = x2, y2

    if len(cur_pts) >= 2:
        polylines.append((cur_pts, cur_sizes))

    return polylines


def rgb_from_int(color_int: int) -> Tuple[float, float, float]:
    """0xRRGGBB -> (r,g,b) in 0..1"""
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return (r / 255.0, g / 255.0, b / 255.0)


def hsl_metrics_from_rgb_int(color_int: int) -> Tuple[float, float]:
    """Vrátí (saturation, lightness) z RGB (0..1)."""
    r = ((color_int >> 16) & 0xFF) / 255.0
    g = ((color_int >> 8) & 0xFF) / 255.0
    b = (color_int & 0xFF) / 255.0
    mx = max(r, g, b)
    mn = min(r, g, b)
    l = (mx + mn) / 2.0
    if mx == mn:
        s = 0.0
    else:
        d = mx - mn
        s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    return s, l


def choose_invert_y(sample_pts: List[Tuple[float, float]], page_w: float, page_h: float, sx: float, sy: float) -> bool:
    """
    Zkusí (y) a (page_h - y) a vybere variantu, která má víc bodů uvnitř stránky.
    """
    def score(invert: bool) -> float:
        inside = 0
        total = 0
        for x, y in sample_pts:
            X = x * sx
            Y = y * sy
            if invert:
                Y = page_h - Y
            total += 1
            if 0 <= X <= page_w and 0 <= Y <= page_h:
                inside += 1
        return inside / total if total else 0.0

    s0 = score(False)
    s1 = score(True)
    return s1 > s0 + 0.05


# -----------------------------
# PenType classification
# -----------------------------

@dataclass
class PenStats:
    count: int
    med_width: float
    avg_sat: float
    avg_light: float
    unique_colors: int


def collect_pen_stats(zf: zipfile.ZipFile, ann_members: List[str]) -> Dict[int, PenStats]:
    widths: Dict[int, List[float]] = defaultdict(list)
    colors: Dict[int, Counter] = defaultdict(Counter)

    for m in ann_members:
        db_bytes = zf.read(m)
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf:
            tf.write(db_bytes)
            tf.flush()
            temp_path = tf.name
        
        try:
            con = sqlite3.connect(temp_path)
            cur = con.cursor()
            # Zajímá nás jen ink
            for pt, w, c in cur.execute(
                "SELECT penType, strokeWidth, strokeColor FROM annotation WHERE annotationType=0"
            ):
                pt = int(pt) if pt is not None else -1
                if w is not None:
                    widths[pt].append(float(w))
                if c is not None:
                    colors[pt][int(c)] += 1
            con.close()
        finally:
            os.unlink(temp_path)

    stats: Dict[int, PenStats] = {}
    for pt, ws in widths.items():
        if not ws:
            continue
        medw = median(ws)
        total = 0
        sat_sum = 0.0
        light_sum = 0.0
        for col, cnt in colors[pt].most_common(25):
            s, l = hsl_metrics_from_rgb_int(col)
            sat_sum += s * cnt
            light_sum += l * cnt
            total += cnt
        avg_s = sat_sum / total if total else 0.0
        avg_l = light_sum / total if total else 0.0
        stats[pt] = PenStats(
            count=len(ws),
            med_width=medw,
            avg_sat=avg_s,
            avg_light=avg_l,
            unique_colors=len(colors[pt]),
        )
    return stats


def classify_highlighters(stats: Dict[int, PenStats]) -> List[int]:
    """
    Heuristika: zvýrazňovač = jasnější/sytější barvy + rozumný strokeWidth + dost dat.
    """
    candidates = [pt for pt, s in stats.items() if s.count >= 20]
    if not candidates:
        return []

    overall_med = median([stats[pt].med_width for pt in candidates])

    highlighters: List[int] = []
    for pt in candidates:
        s = stats[pt]
        if s.avg_sat > 0.35 and s.avg_light > 0.18 and s.med_width >= overall_med * 0.9:
            highlighters.append(pt)

    return highlighters


def compute_highlighter_width_multipliers(
    stats: Dict[int, PenStats],
    highlighter_types: List[int],
    desired_ratio_to_pen: float,
    clamp: Tuple[float, float] = (1.0, 12.0),
) -> Dict[int, float]:
    """
    Cíl: aby highlighter vypadal cca desired_ratio_to_pen krát tlustší než normální pero,
    podle mediánů strokeWidth v DB.

    mult = desired_ratio * pen_med / hl_med
    """
    hl_set = set(highlighter_types)
    pen_meds = [s.med_width for pt, s in stats.items() if pt not in hl_set and s.count >= 20]
    pen_med = median(pen_meds) if pen_meds else None

    mults: Dict[int, float] = {}
    for pt in highlighter_types:
        hl_med = stats[pt].med_width
        if pen_med and hl_med > 0:
            mult = desired_ratio_to_pen * (pen_med / hl_med)
        else:
            mult = 4.0
        mult = max(clamp[0], min(clamp[1], mult))
        mults[pt] = mult
    return mults


# -----------------------------
# Core conversion
# -----------------------------

def find_document_plist_member(zf: zipfile.ZipFile) -> str:
    for n in zf.namelist():
        if n.endswith("Document.plist"):
            return n
    raise RuntimeError("Nenalezen Document.plist uvnitř .nsa")


def build_annotation_index(zf: zipfile.ZipFile) -> Dict[str, str]:
    """
    Map uuid -> zip member path
    (soubor je typicky .../Annotations/<uuid>)
    """
    ann = {}
    for n in zf.namelist():
        if "/Annotations/" in n and not n.endswith("/"):
            ann[os.path.basename(n)] = n
    return ann


def build_template_index(zf: zipfile.ZipFile) -> Dict[str, str]:
    """
    Map basename(template) -> zip member path (Templates/.../*.ns_pdf)
    """
    tpl = {}
    for n in zf.namelist():
        if n.endswith(".ns_pdf") or n.lower().endswith(".pdf"):
            base = os.path.basename(n)
            tpl[base] = n
    return tpl


def build_resource_index(zf: zipfile.ZipFile) -> Dict[str, str]:
    """
    Map resource id (basename without extension) -> zip member path (Resources/.../image)
    """
    exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
    res = {}
    for n in zf.namelist():
        if "/Resources/" not in n or n.endswith("/"):
            continue
        _, ext = os.path.splitext(n)
        if ext.lower() not in exts:
            continue
        base = os.path.splitext(os.path.basename(n))[0]
        res.setdefault(base, n)
        res.setdefault(base.lower(), n)
    return res


def open_template_cached(cache: Dict[str, fitz.Document], zf: zipfile.ZipFile, member: str) -> fitz.Document:
    if member in cache:
        return cache[member]
    data = zf.read(member)
    if not data.startswith(b"%PDF"):
        raise RuntimeError(f"Template '{member}' nevypadá jako PDF.")
    doc = fitz.open(stream=data, filetype="pdf")
    cache[member] = doc
    return doc


def draw_page_annotations(
    zf: zipfile.ZipFile,
    out_page: fitz.Page,
    ann_member: str,
    sx: float,
    sy: float,
    resource_index: Dict[str, str],
    highlighter_types: set,
    highlighter_opacity: float,
    hl_width_mults: Dict[int, float],
    smooth: bool,
    epsilon: float,
    curve: bool,
    use_pressure: bool,
) -> None:
    ann_bytes = zf.read(ann_member)
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf:
        tf.write(ann_bytes)
        tf.flush()
        temp_path = tf.name
    
    try:
        con = sqlite3.connect(temp_path)
        cur = con.cursor()

        cols = [c[1] for c in cur.execute("PRAGMA table_info(annotation)").fetchall()]
        has_pen_type = "penType" in cols

        # rozhodni invert_y podle vzorku bodů
        sample_pts: List[Tuple[float, float]] = []
        for (blob,) in cur.execute("SELECT stroke_segments_v3 FROM annotation WHERE annotationType=0 LIMIT 20"):
            pts = extract_points_from_blob(blob)
            if pts:
                step = max(1, len(pts) // 30)
                sample_pts.extend(pts[::step])
            if len(sample_pts) > 500:
                break

        invert_y = choose_invert_y(sample_pts, out_page.rect.width, out_page.rect.height, sx, sy)

        ink_bbox_keys: set = set()
        for c, bx, by, bw, bh in cur.execute(
            "SELECT strokeColor, boundingRect_x, boundingRect_y, boundingRect_w, boundingRect_h "
            "FROM annotation WHERE annotationType=0"
        ):
            if bw is None or bh is None:
                continue
            col = int(c) if c is not None else 0
            key = bbox_key(col, float(bx or 0.0), float(by or 0.0), float(bw), float(bh))
            ink_bbox_keys.add(key)

        # -------- images (annotationType=2) --------
        for img_id, bx, by, bw, bh, _img_tx, _tx in cur.execute(
            "SELECT id, boundingRect_x, boundingRect_y, boundingRect_w, boundingRect_h, imgTxMatrix, txMatrix "
            "FROM annotation WHERE annotationType=2"
        ):
            if not img_id or bw is None or bh is None:
                continue

            img_id_str = str(img_id)
            member = resource_index.get(img_id_str)
            if not member:
                base = os.path.splitext(img_id_str)[0]
                member = resource_index.get(base) or resource_index.get(base.lower())
            if not member:
                continue

            try:
                img_bytes = zf.read(member)
            except Exception:
                continue

            x0 = float(bx or 0.0) * sx
            y0 = float(by or 0.0) * sy
            w = float(bw) * sx
            h = float(bh) * sy
            if w <= 0 or h <= 0:
                continue

            if invert_y:
                y0 = out_page.rect.height - y0 - h

            rect = fitz.Rect(x0, y0, x0 + w, y0 + h)
            try:
                out_page.insert_image(rect, stream=img_bytes)
            except Exception:
                continue

        # -------- stickers / emoji (annotationType=7) --------
        # Check if rotationAngle column exists (newer NSA format)
        has_rotation_angle = "rotationAngle" in cols
        
        if has_rotation_angle:
            sticker_query = (
                "SELECT emojiName, boundingRect_x, boundingRect_y, boundingRect_w, boundingRect_h, rotationAngle "
                "FROM annotation WHERE annotationType=7"
            )
        else:
            sticker_query = (
                "SELECT emojiName, boundingRect_x, boundingRect_y, boundingRect_w, boundingRect_h, 0 as rotationAngle "
                "FROM annotation WHERE annotationType=7"
            )
        
        for emojiName, bx, by, bw, bh, rot in cur.execute(sticker_query):
            if not emojiName or bw is None or bh is None:
                continue

            key = str(emojiName)
            member = resource_index.get(key) or resource_index.get(key.lower())
            if not member:
                # sticker resource not embedded / not found
                continue

            try:
                img_bytes = zf.read(member)
            except Exception:
                continue

            x0 = float(bx or 0.0) * sx
            y0 = float(by or 0.0) * sy
            w  = float(bw) * sx
            h  = float(bh) * sy
            if w <= 0 or h <= 0:
                continue

            if invert_y:
                y0 = out_page.rect.height - y0 - h

            rect = fitz.Rect(x0, y0, x0 + w, y0 + h)

            # NOTE: rot in your sample is always 0.
            # If you later hit non-zero rotations, we can handle it (PyMuPDF supports rotate=90/180/270).
            try:
                out_page.insert_image(rect, stream=img_bytes)
            except Exception:
                continue

        # -------- ink strokes (annotationType=0) --------
        groups = defaultdict(list)  # (rgb,width,opacity) -> list[polyline]
        seg_groups = defaultdict(list)  # (rgb,width,opacity) -> list[((x1,y1),(x2,y2))]

        if has_pen_type:
            q = "SELECT strokeWidth, strokeColor, penType, stroke_segments_v3 FROM annotation WHERE annotationType=0"
        else:
            q = "SELECT strokeWidth, strokeColor, 0 as penType, stroke_segments_v3 FROM annotation WHERE annotationType=0"

        for strokeWidth, strokeColor, penType, blob in cur.execute(q):
            segments = extract_segments_from_blob(blob)
            if len(segments) < 1:
                continue

            # hodn?? dlouh?? tahy z??e?? (kv??li velikosti PDF)
            if len(segments) > 5000:
                step = max(1, len(segments) // 1600)
                segments = segments[::step]

            seg_scaled: List[Tuple[float, float, float, float, float]] = []
            for x1, y1, x2, y2, size in segments:
                X1 = x1 * sx
                Y1 = y1 * sy
                X2 = x2 * sx
                Y2 = y2 * sy
                if invert_y:
                    Y1 = out_page.rect.height - Y1
                    Y2 = out_page.rect.height - Y2
                seg_scaled.append((X1, Y1, X2, Y2, float(size)))

            col = int(strokeColor) if strokeColor is not None else 0
            rgb = rgb_from_int(col)

            w = float(strokeWidth) if strokeWidth is not None else 1.0
            width_base = w * sx  # základní převod do PDF bodů

            pt = int(penType) if penType is not None else 0
            opacity = 1.0

            if pt in highlighter_types:
                opacity = highlighter_opacity
                width_base *= hl_width_mults.get(pt, 4.0)

            if use_pressure:
                sizes = [s for *_, s in seg_scaled if s > 0]
                med_size = median(sizes) if sizes else 0.0
                if curve:
                    polys = segments_to_polylines_with_sizes(seg_scaled)
                    for pts, szs in polys:
                        if len(pts) < 2 or len(pts) != len(szs):
                            continue
                        samples = catmull_rom_spline_with_widths(pts, szs, adaptive_curve_samples(pts))
                        for i in range(len(samples) - 1):
                            x1, y1, w1 = samples[i]
                            x2, y2, w2 = samples[i + 1]
                            size = (w1 + w2) / 2.0
                            if size < 0.0:
                                size = 0.0
                            ratio = (size / med_size) if med_size > 0 else 1.0
                            ratio = max(0.25, min(3.0, ratio))
                            width = quantize_width(width_base * ratio)
                            key = (rgb, round(width, 3), round(opacity, 3))
                            seg_groups[key].append(((x1, y1), (x2, y2)))
                else:
                    for x1, y1, x2, y2, size in seg_scaled:
                        ratio = (size / med_size) if med_size > 0 else 1.0
                        ratio = max(0.25, min(3.0, ratio))
                        width = width_base * ratio
                        key = (rgb, round(width, 3), round(opacity, 3))
                        seg_groups[key].append(((x1, y1), (x2, y2)))
            else:
                polylines = segments_to_polylines(seg_scaled)
                refined: List[List[Tuple[float, float]]] = []
                for poly in polylines:
                    if curve and len(poly) > 3:
                        poly = catmull_rom_spline(poly, adaptive_curve_samples(poly))
                    elif smooth and len(poly) > 3:
                        poly = rdp(poly, epsilon)
                    if len(poly) >= 2:
                        refined.append(poly)

                key = (rgb, round(width_base, 3), round(opacity, 3))
                for poly in refined:
                    groups[key].append(poly)

        for (rgb, width, opacity), polylines in groups.items():
            sh = out_page.new_shape()
            for poly in polylines:
                sh.draw_polyline(poly)
            sh.finish(width=float(width), color=rgb, stroke_opacity=float(opacity))
            sh.commit()

        for (rgb, width, opacity), segments in seg_groups.items():
            sh = out_page.new_shape()
            for p1, p2 in segments:
                sh.draw_line(p1, p2)
            sh.finish(width=float(width), color=rgb, stroke_opacity=float(opacity))
            sh.commit()

        # -------- shapes (annotationType=5) --------
        shape_groups = defaultdict(list)  # (rgb,width,opacity,closed) -> list[polyline]
        for strokeColor, strokeWidth, shape_data, bx, by, bw, bh in cur.execute(
            "SELECT strokeColor, strokeWidth, shape_data, boundingRect_x, boundingRect_y, boundingRect_w, boundingRect_h "
            "FROM annotation WHERE annotationType=5"
        ):
            if not shape_data:
                continue
            try:
                sd = json.loads(shape_data)
            except Exception:
                continue

            pts = sd.get("controlPoints") or []
            if len(pts) < 2:
                continue

            if bw is not None and bh is not None:
                col = int(strokeColor) if strokeColor is not None else 0
                key = bbox_key(col, float(bx or 0.0), float(by or 0.0), float(bw), float(bh))
                if key in ink_bbox_keys:
                    continue

            pts_scaled: List[Tuple[float, float]] = []
            for x, y in pts:
                X = float(x) * sx
                Y = float(y) * sy
                if invert_y:
                    Y = out_page.rect.height - Y
                pts_scaled.append((X, Y))

            col = int(strokeColor) if strokeColor is not None else 0
            rgb = rgb_from_int(col)

            opacity = float(sd.get("strokeOpacity", 1.0))
            thickness = float(sd.get("properties", {}).get("strokeThickness", strokeWidth if strokeWidth is not None else 1.0))
            width = thickness * sx

            closed = int(sd.get("numberOfSides", 0)) >= 3
            key = (rgb, round(width, 3), round(opacity, 3), closed)
            shape_groups[key].append(pts_scaled)

        for (rgb, width, opacity, closed), polys in shape_groups.items():
            sh = out_page.new_shape()
            for poly in polys:
                sh.draw_polyline(poly)
                if closed:
                    sh.draw_line(poly[-1], poly[0])
            sh.finish(width=float(width), color=rgb, stroke_opacity=float(opacity))
            sh.commit()

        con.close()
    finally:
        # Close any remaining connections and allow Windows to release file lock
        try:
            if 'con' in locals():
                con.close()
        except:
            pass
        
        # Small delay for Windows file lock release
        import time
        time.sleep(0.01)
        
        try:
            os.unlink(temp_path)
        except PermissionError:
            # Windows sometimes keeps file locked briefly - retry once
            time.sleep(0.1)
            try:
                os.unlink(temp_path)
            except:
                pass  # Give up, temp file will be cleaned eventually


def nsa_to_pdf(
    nsa_path: str,
    out_pdf: str,
    *,
    desired_highlighter_ratio: float = 5.0,
    highlighter_opacity: float = 0.38,
    smooth: bool = True,
    epsilon: float = 0.8,
    curve: bool = True,
    use_pressure: bool = True,
    verbose: bool = True,
) -> None:
    if verbose:
        print(f"[+] Reading: {nsa_path}")

    with zipfile.ZipFile(nsa_path, "r") as zf:
        doc_member = find_document_plist_member(zf)
        doc = plistlib.loads(zf.read(doc_member))
        pages = doc.get("pages", [])
        if not pages:
            raise RuntimeError("Document.plist neobsahuje žádné stránky.")

        ann_index = build_annotation_index(zf)
        tpl_index = build_template_index(zf)
        res_index = build_resource_index(zf)

        # PenType stats pro auto-detekci zvýrazňovače
        ann_members_all = list(ann_index.values())
        stats = collect_pen_stats(zf, ann_members_all)
        hl_types = classify_highlighters(stats)
        hl_mults = compute_highlighter_width_multipliers(stats, hl_types, desired_highlighter_ratio)

        if verbose:
            print(f"[i] Detected highlighter penTypes: {hl_types} (opacity={highlighter_opacity}, ratio~{desired_highlighter_ratio}x)")
            if hl_types:
                print(f"[i] Highlighter width multipliers: {hl_mults}")

        # Cache pro template PDFs
        tpl_cache: Dict[str, fitz.Document] = {}

        # Output doc (nové PDF)
        out_doc = fitz.open()

        # Pro každou stránku vytvoř stránku v out_doc a dokresli anotace
        for i, p in enumerate(pages, start=1):
            uuid = p.get("uuid")
            tpl_name = p.get("associatedPDFFileName") or next(iter(doc.get("documents", {}).keys()), None)
            pdf_idx_1based = p.get("associatedPDFKitPageIndex") or p.get("associatedPageIndex") or 1
            tpl_basename = os.path.basename(tpl_name) if tpl_name else None

            # 1) vlož template stránku (nebo vytvoř blank)
            out_page: fitz.Page
            if tpl_basename and tpl_basename in tpl_index:
                tpl_member = tpl_index[tpl_basename]
                tpl_doc = open_template_cached(tpl_cache, zf, tpl_member)
                src_idx = int(pdf_idx_1based) - 1
                if 0 <= src_idx < tpl_doc.page_count:
                    out_doc.insert_pdf(tpl_doc, from_page=src_idx, to_page=src_idx)
                    out_page = out_doc[-1]
                else:
                    # fallback blank
                    dims = parse_pdfkit_rect(p.get("pdfKitPageRect", "")) or (1200.0, 1600.0)
                    out_page = out_doc.new_page(width=dims[0] * 0.5, height=dims[1] * 0.5)
            else:
                dims = parse_pdfkit_rect(p.get("pdfKitPageRect", "")) or (1200.0, 1600.0)
                out_page = out_doc.new_page(width=dims[0] * 0.5, height=dims[1] * 0.5)

            # 2) spočti scale Noteshelf coords -> PDF coords
            dims = parse_pdfkit_rect(p.get("pdfKitPageRect", "")) or None
            if dims:
                pw, ph = dims
                sx = (out_page.rect.width / pw) if pw else 1.0
                sy = (out_page.rect.height / ph) if ph else 1.0
            else:
                sx = sy = 1.0

            # 3) anotace
            if uuid and uuid in ann_index:
                draw_page_annotations(
                    zf=zf,
                    out_page=out_page,
                    ann_member=ann_index[uuid],
                    sx=sx,
                    sy=sy,
                    resource_index=res_index,
                    highlighter_types=set(hl_types),
                    highlighter_opacity=highlighter_opacity,
                    hl_width_mults=hl_mults,
                    smooth=smooth,
                    epsilon=epsilon,
                    curve=curve,
                    use_pressure=use_pressure,
                )

            if verbose and i % 10 == 0:
                print(f"[i] Processed pages: {i}/{len(pages)}")

        # Close cached template docs
        for d in tpl_cache.values():
            d.close()

        os.makedirs(os.path.dirname(os.path.abspath(out_pdf)) or ".", exist_ok=True)
        out_doc.save(out_pdf)
        out_doc.close()

    if verbose:
        print(f"[+] Written: {out_pdf}")


# -----------------------------
# CLI
# -----------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Convert Noteshelf Android .nsa to PDF (template + annotations).")
    ap.add_argument("input", help="Cesta k .nsa souboru nebo složce s .nsa")
    ap.add_argument("-o", "--output", help="Výstupní PDF (pokud input je jeden soubor)")
    ap.add_argument("--outdir", help="Výstupní složka (pokud input je složka)")
    ap.add_argument("--quiet", action="store_true", help="Méně výpisů")

    ap.add_argument("--highlighter-opacity", type=float, default=0.38,
                    help="Opacity of the highlighter (0..1). Default 0.38")
    ap.add_argument("--highlighter-ratio", type=float, default=5.0,
                    help="How much thicker the highlighter should be compared to the pen (ratio). Default 5.0")

    ap.add_argument("--no-smooth", action="store_true",
                    help="Disable smoothing (RDP).")
    ap.add_argument("--curve", action="store_true", default=True,
                    help="Use Catmull-Rom spline upsampling instead of RDP (default on).")
    ap.add_argument("--no-curve", action="store_false", dest="curve",
                    help="Disable curve smoothing (use RDP if enabled).")
    ap.add_argument("--epsilon", type=float, default=0.8,
                    help="RDP epsilon (greater = smoother, smaller = more precise). Default 0.8")
    ap.add_argument("--pressure", action="store_true", default=True,
                    help="Use per-segment width from stroke data (variable width). Works with --curve (default on).")
    ap.add_argument("--no-pressure", action="store_false", dest="pressure",
                    help="Disable variable width (pressure).")

    args = ap.parse_args()
    verbose = not args.quiet
    smooth = not args.no_smooth

    in_path = args.input

    if os.path.isdir(in_path):
        outdir = args.outdir or os.path.join(in_path, "pdf_out")
        os.makedirs(outdir, exist_ok=True)

        nsa_files = [f for f in os.listdir(in_path) if f.lower().endswith(".nsa")]
        if not nsa_files:
            raise SystemExit("No .nsa files found in the directory  .")

        for f in sorted(nsa_files):
            src = os.path.join(in_path, f)
            base = os.path.splitext(f)[0]
            dst = os.path.join(outdir, base + ".pdf")

            nsa_to_pdf(
                src, dst,
                desired_highlighter_ratio=args.highlighter_ratio,
                highlighter_opacity=args.highlighter_opacity,
                smooth=smooth,
                epsilon=args.epsilon,
                curve=args.curve,
                use_pressure=args.pressure,
                verbose=verbose,
            )

        if verbose:
            print(f"[+] Done. Output dir: {outdir}")
        return

    if not in_path.lower().endswith(".nsa"):
        raise SystemExit("Input is not a .nsa file (or directory).")

    out_pdf = args.output
    if not out_pdf:
        base = os.path.splitext(os.path.basename(in_path))[0]
        out_pdf = os.path.join(os.path.dirname(in_path) or ".", base + ".pdf")

    nsa_to_pdf(
        in_path, out_pdf,
        desired_highlighter_ratio=args.highlighter_ratio,
        highlighter_opacity=args.highlighter_opacity,
        smooth=smooth,
        epsilon=args.epsilon,
        curve=args.curve,
        use_pressure=args.pressure,
        verbose=verbose,
    )


if __name__ == "__main__":
    main()
