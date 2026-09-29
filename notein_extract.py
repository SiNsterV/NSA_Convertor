#!/usr/bin/env python3
"""Render a Notein export bundle directly to a PDF.

This script takes either:
  - a Notein export zip, or
  - an already-unpacked Notein export directory

It will:
    1) unpack the source if needed,
    2) read the Notein SQLite database,
    3) render each page from strokes, shapes, text boxes, and images,
    4) save all pages into one PDF.

Example:
    python notein_extract.py /path/to/note_export.zip -o out.pdf

Tested against a bundle that contained files such as:
    note_database_note_<uuid>_db
    note_meta.json
    note_image_<uuid>.png
    note_image_<uuid>.svg
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import uuid
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"Pillow is required: {exc}")

try:
    import cairosvg  # type: ignore
except Exception:
    cairosvg = None

try:
    import fitz  # type: ignore
except Exception:
    fitz = None

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:
    BeautifulSoup = None


HIGHLIGHTER_ALPHA = 96
STICKY_NOTE_WIDTH = 120.0
STICKY_NOTE_HEIGHT = 44.0
UNBOUNDED_PAGE_MARGIN = 96.0
PDF_UNIT_SCALE = 72.0 / 150.0


@dataclass
class PageInfo:
    page_id: str
    index: int
    width: int
    height: int
    background_rgba: tuple[int, int, int, int]
    background_style: str
    page_row: sqlite3.Row
    origin_x: float = 0.0
    origin_y: float = 0.0


@dataclass
class NoteRenderData:
    pages: list[PageInfo]
    page_entities: dict[str, dict[str, list[sqlite3.Row]]]
    layer_index: dict[str, int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a Notein export bundle to a single PDF."
    )
    parser.add_argument("input_path", help="Path to a Notein zip bundle or extracted directory")
    parser.add_argument(
        "-o",
        "--output",
        help="Output PDF path (default: <input>.pdf)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the written PDF path and page count.",
    )
    return parser.parse_args()


def default_output_pdf_path(input_path: Path) -> Path:
    base_name = input_path.name if input_path.is_dir() else input_path.stem
    return input_path.parent / f"{base_name}.pdf"


def notein_to_pdf(
    input_path: str | Path,
    output_pdf: str | Path | None = None,
    verbose: bool = False,
) -> int:
    input_path = Path(input_path).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"Input path does not exist: {input_path}")

    if output_pdf is not None:
        output_pdf_path = Path(output_pdf).expanduser().resolve()
    else:
        output_pdf_path = default_output_pdf_path(input_path)

    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    page_count = 0
    with prepare_source_context(input_path) as source_dir:
        db_path = find_database(source_dir)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            note_data = load_note_data(conn)
            if fitz is not None:
                write_vector_pdf(output_pdf_path, note_data, source_dir)
                page_count = len(note_data.pages)
            else:
                page_images = render_note_images(note_data, source_dir)
                write_pdf(output_pdf_path, page_images)
                page_count = len(page_images)
        finally:
            conn.close()

    if verbose:
        print(f"Wrote PDF: {output_pdf_path}")
        print(f"Pages: {page_count}")
    return page_count


def main() -> int:
    args = parse_args()
    notein_to_pdf(args.input_path, args.output, verbose=args.verbose)
    return 0


@dataclass
class SourceContext:
    source_dir: Path
    cleanup_dir: Optional[Path] = None

    def __enter__(self) -> Path:
        return self.source_dir

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.cleanup_dir is not None:
            shutil.rmtree(self.cleanup_dir, ignore_errors=True)


def prepare_source_context(input_path: Path) -> SourceContext:
    if input_path.is_dir():
        return SourceContext(source_dir=input_path)

    if not zipfile.is_zipfile(input_path):
        raise SystemExit(
            "Input is neither a directory nor a valid zip file. "
            f"Got: {input_path}"
        )

    work_dir = input_path.parent / f".notein_extract_{input_path.stem}_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(input_path, "r") as zf:
            safe_extract_zip(zf, work_dir)
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    return SourceContext(source_dir=work_dir, cleanup_dir=work_dir)


def safe_extract_zip(zf: zipfile.ZipFile, target_dir: Path) -> None:
    root = target_dir.resolve()
    for info in zf.infolist():
        destination = target_dir / info.filename
        resolved = destination.resolve()
        if root != resolved and root not in resolved.parents:
            raise SystemExit(f"Refusing unsafe zip member path: {info.filename}")
        if info.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info, "r") as src, destination.open("wb") as dst:
            shutil.copyfileobj(src, dst)


def find_database(source_dir: Path) -> Path:
    candidates = sorted(
        p
        for p in source_dir.glob("note_database*_db*")
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    )
    if not candidates:
        raise SystemExit(f"Could not find Notein database in: {source_dir}")
    return candidates[0]


def extract_note(
    conn: sqlite3.Connection,
    source_dir: Path,
) -> list[Image.Image]:
    note_data = load_note_data(conn)
    return render_note_images(note_data, source_dir)


def load_note_data(conn: sqlite3.Connection) -> NoteRenderData:
    cur = conn.cursor()
    note_row = cur.execute("SELECT * FROM NoteContentEntity LIMIT 1").fetchone()
    if note_row is None:
        raise SystemExit("The Notein database does not contain NoteContentEntity rows.")

    page_order = parse_json_list(note_row["page_list"])
    layer_order = parse_json_list(note_row["page_layer_list"])
    layer_index = {layer_id: i for i, layer_id in enumerate(layer_order)}

    pages_by_id = {
        row["id"]: row
        for row in cur.execute("SELECT * FROM PageEntity").fetchall()
    }

    pages: list[PageInfo] = []
    for idx, page_id in enumerate(page_order, start=1):
        row = pages_by_id.get(page_id)
        if row is None:
            continue
        pages.append(build_page_info(row, idx))

    if not pages:
        raise SystemExit("No pages were found in the Notein page list.")

    page_entities = {
        page.page_id: gather_page_entities(cur, page.page_id) for page in pages
    }

    adjust_unbounded_pages(pages, page_entities, note_row)

    return NoteRenderData(
        pages=pages,
        page_entities=page_entities,
        layer_index=layer_index,
    )


def render_note_images(
    note_data: NoteRenderData,
    source_dir: Path,
) -> list[Image.Image]:
    page_images: list[Image.Image] = []

    for page in note_data.pages:
        entities = note_data.page_entities[page.page_id]
        image = render_page(
            page=page,
            entities=entities,
            source_dir=source_dir,
            layer_index=note_data.layer_index,
            include_images=False,
        )
        page_images.append(image)

    draw_images_with_page_spillover(page_images, note_data.pages, note_data.page_entities, source_dir)

    return page_images


def parse_json_list(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        if isinstance(value, list):
            return [str(x) for x in value]
    except Exception:
        pass
    return []


def build_page_info(row: sqlite3.Row, index: int) -> PageInfo:
    paper_spec = parse_json_object(row["paper_spec"])
    paper_theme = parse_json_object(row["paper_theme"])
    background_rgba = (255, 255, 255, 255)
    background_style = "blank"

    try:
        base_theme = paper_theme.get("baseTheme", {})
        color = base_theme.get("color")
        if color is not None:
            background_rgba = android_color_to_rgba(int(color))
    except Exception:
        pass

    try:
        paper_style = paper_theme.get("paperStyle", {})
        style_type = str(paper_style.get("type", ""))
        if style_type:
            background_style = style_type
    except Exception:
        pass

    width = int(round(float(paper_spec.get("width", 1240))))
    height = int(round(float(paper_spec.get("height", 1754))))
    return PageInfo(
        page_id=row["id"],
        index=index,
        width=width,
        height=height,
        background_rgba=background_rgba,
        background_style=background_style,
        page_row=row,
    )


def gather_page_entities(cur: sqlite3.Cursor, page_id: str) -> dict[str, list[sqlite3.Row]]:
    return {
        "strokes": cur.execute(
            "SELECT * FROM StrokeEntity WHERE page_id=? ORDER BY creation_time, id", (page_id,)
        ).fetchall(),
        "shapes": cur.execute(
            "SELECT * FROM ShapeEntity WHERE page_id=? ORDER BY creation_time, id", (page_id,)
        ).fetchall(),
        "textboxes": cur.execute(
            "SELECT * FROM TextBoxEntity WHERE page_id=? ORDER BY creation_time, id", (page_id,)
        ).fetchall(),
        "images": cur.execute(
            "SELECT * FROM ImageEntity WHERE page_id=? ORDER BY creation_time, id", (page_id,)
        ).fetchall(),
        "comments": cur.execute(
            """
            SELECT
                c.id,
                c.title,
                c.page_id,
                c.quote_id,
                COALESCE(q.layer_id, 'none') AS layer_id,
                COALESCE(q.creation_time, c.creation_time, 0) AS creation_time,
                q.label_rect,
                q.rect_list,
                q.bg_color,
                q.color
            FROM CommentEntity AS c
            LEFT JOIN QuoteEntity AS q ON q.id = c.quote_id
            WHERE c.page_id=?
            ORDER BY creation_time, c.id
            """,
            (page_id,),
        ).fetchall(),
    }


def adjust_unbounded_pages(
    pages: list[PageInfo],
    page_entities: dict[str, dict[str, list[sqlite3.Row]]],
    note_row: sqlite3.Row,
) -> None:
    note_unbounded = bool(note_row["unbounded_note"]) if "unbounded_note" in note_row.keys() else False
    for page in pages:
        page_unbounded = bool(page.page_row["unbounded"]) if "unbounded" in page.page_row.keys() else False
        if not (note_unbounded or page_unbounded):
            continue

        bounds = content_bounds_for_page(page_entities.get(page.page_id, {}))
        if bounds is None:
            continue

        left, top, right, bottom = bounds
        if right <= left or bottom <= top:
            continue

        page.origin_x = math.floor(left - UNBOUNDED_PAGE_MARGIN)
        page.origin_y = math.floor(top - UNBOUNDED_PAGE_MARGIN)
        page.width = max(1, int(math.ceil(right + UNBOUNDED_PAGE_MARGIN - page.origin_x)))
        page.height = max(1, int(math.ceil(bottom + UNBOUNDED_PAGE_MARGIN - page.origin_y)))


def content_bounds_for_page(
    entities: dict[str, list[sqlite3.Row]]
) -> Optional[tuple[float, float, float, float]]:
    rects: list[tuple[float, float, float, float]] = []
    for kind in ("strokes", "shapes", "textboxes", "images"):
        for row in entities.get(kind, []):
            rect = row_rect(row)
            if rect is not None:
                rects.append(rect)
    for row in entities.get("comments", []):
        rects.append(comment_display_rect(row))

    if not rects:
        return None

    return (
        min(rect[0] for rect in rects),
        min(rect[1] for rect in rects),
        max(rect[2] for rect in rects),
        max(rect[3] for rect in rects),
    )


def row_rect(row: sqlite3.Row) -> Optional[tuple[float, float, float, float]]:
    keys = row.keys()
    if all(key in keys for key in ("left", "top", "right", "bottom")):
        try:
            left = float(row["left"] or 0)
            top = float(row["top"] or 0)
            right = float(row["right"] or left)
            bottom = float(row["bottom"] or top)
            return normalize_rect(left, top, right, bottom)
        except Exception:
            pass
    if "bounds" in keys:
        return parse_json_rect(row["bounds"])
    return None


def parse_json_rect(raw: Optional[str]) -> Optional[tuple[float, float, float, float]]:
    value = parse_json_object(raw)
    if not value:
        return None
    try:
        return normalize_rect(
            float(value.get("left", 0) or 0),
            float(value.get("top", 0) or 0),
            float(value.get("right", 0) or 0),
            float(value.get("bottom", 0) or 0),
        )
    except Exception:
        return None


def normalize_rect(left: float, top: float, right: float, bottom: float) -> tuple[float, float, float, float]:
    return min(left, right), min(top, bottom), max(left, right), max(top, bottom)


def comment_display_rect(row: sqlite3.Row) -> tuple[float, float, float, float]:
    x, y = comment_anchor_point(row)
    return (x, y, x + STICKY_NOTE_WIDTH, y + STICKY_NOTE_HEIGHT)


def comment_anchor_point(row: sqlite3.Row) -> tuple[float, float]:
    rect = parse_json_rect(row["label_rect"] if "label_rect" in row.keys() else None)
    if rect is None:
        rects = parse_json_list_of_rects(row["rect_list"] if "rect_list" in row.keys() else None)
        rect = rects[0] if rects else None
    if rect is None:
        return 0.0, 0.0

    left, top, right, bottom = rect
    if abs(right - left) < 1e-3 and abs(bottom - top) < 1e-3:
        return left, top
    return left, top


def parse_json_list_of_rects(raw: Optional[str]) -> list[tuple[float, float, float, float]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except Exception:
        return []
    if not isinstance(value, list):
        return []

    rects: list[tuple[float, float, float, float]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            rects.append(
                normalize_rect(
                    float(item.get("left", 0) or 0),
                    float(item.get("top", 0) or 0),
                    float(item.get("right", 0) or 0),
                    float(item.get("bottom", 0) or 0),
                )
            )
        except Exception:
            continue
    return rects


def parse_json_object(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    return {}


def android_color_to_rgba(value: int, alpha_override: Optional[int] = None) -> tuple[int, int, int, int]:
    value &= 0xFFFFFFFF
    a = (value >> 24) & 0xFF
    r = (value >> 16) & 0xFF
    g = (value >> 8) & 0xFF
    b = value & 0xFF
    if alpha_override is not None:
        a = max(0, min(255, alpha_override))
    return (r, g, b, a)


def notein_long_color_to_rgba(value: int) -> tuple[int, int, int, int]:
    value &= 0xFFFFFFFFFFFFFFFF
    high = (value >> 32) & 0xFFFFFFFF
    low = value & 0xFFFFFFFF
    if high:
        return android_color_to_rgba(high)
    return android_color_to_rgba(low)


def html_to_plain_text(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = raw
    if BeautifulSoup is not None:
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup.find_all("br"):
            tag.replace_with("\n")
        for tag_name in ("style", "script"):
            for tag in soup.find_all(tag_name):
                tag.decompose()
        text = soup.get_text("\n")
    else:
        text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_page(
    page: PageInfo,
    entities: dict[str, list[sqlite3.Row]],
    source_dir: Path,
    layer_index: dict[str, int],
    include_images: bool = True,
) -> Image.Image:
    image = Image.new("RGBA", (page.width, page.height), page.background_rgba)
    draw = ImageDraw.Draw(image, "RGBA")

    draw_page_background_pattern(draw, page)

    items: list[tuple[int, int, str, sqlite3.Row]] = []
    for kind in ("shapes", "images", "textboxes", "comments", "strokes"):
        if kind == "images" and not include_images:
            continue
        for row in entities[kind]:
            row_layer = row["layer_id"] if "layer_id" in row.keys() else None
            items.append((layer_index.get(row_layer, 999), int(row["creation_time"]), kind, row))
    items.sort(key=lambda item: (item[0], item[1], item[2]))

    for _, _, kind, row in items:
        if kind == "shapes":
            draw_shape(draw, row, page.origin_x, page.origin_y)
        elif kind == "images":
            draw_image_entity(image, row, source_dir, page.origin_x, page.origin_y)
        elif kind == "textboxes":
            draw_textbox(draw, row, page.origin_x, page.origin_y)
        elif kind == "comments":
            draw_comment(draw, row, page.origin_x, page.origin_y)
        elif kind == "strokes":
            draw_stroke(draw, row, page.origin_x, page.origin_y)

    return image


def draw_page_background_pattern(draw: ImageDraw.ImageDraw, page: PageInfo) -> None:
    theme = parse_json_object(page.page_row["paper_theme"])
    paper_style = theme.get("paperStyle", {}) if isinstance(theme, dict) else {}
    style_type = str(paper_style.get("type", ""))
    spacing = float(paper_style.get("requiredItemSpace", 0) or 0)
    left_pad = float(paper_style.get("leftPadding", 0) or 0)
    top_pad = float(paper_style.get("topPadding", 0) or 0)

    visible_left = page.origin_x
    visible_right = page.origin_x + page.width
    visible_top = page.origin_y
    visible_bottom = page.origin_y + page.height

    if "Square" in style_type and spacing >= 10:
        line = (215, 215, 215, 70)
        x = first_pattern_position(left_pad, spacing, visible_left)
        while x < visible_right:
            draw.line([(x - page.origin_x, 0), (x - page.origin_x, page.height)], fill=line, width=1)
            x += spacing
        y = first_pattern_position(top_pad, spacing, visible_top)
        while y < visible_bottom:
            draw.line([(0, y - page.origin_y), (page.width, y - page.origin_y)], fill=line, width=1)
            y += spacing
    elif any(token in style_type for token in ("Line", "Ruled", "Horizontal")) and spacing >= 10:
        line = (215, 215, 215, 70)
        y = first_pattern_position(top_pad, spacing, visible_top)
        while y < visible_bottom:
            draw.line([(0, y - page.origin_y), (page.width, y - page.origin_y)], fill=line, width=1)
            y += spacing


def first_pattern_position(anchor: float, spacing: float, minimum: float) -> float:
    if spacing <= 0:
        return anchor
    steps = math.floor((minimum - anchor) / spacing)
    return anchor + steps * spacing


def draw_shape(
    draw: ImageDraw.ImageDraw,
    row: sqlite3.Row,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> None:
    points_raw = row["points"]
    try:
        pts = [(float(p["x"]), float(p["y"])) for p in json.loads(points_raw)]
    except Exception:
        return
    if not pts:
        return
    pts = translate_points(pts, origin_x, origin_y)

    color = android_color_to_rgba(int(row["color"]))
    if is_marker_shape(row):
        color = with_alpha(color, HIGHLIGHTER_ALPHA)
    width = max(1, int(round(float(row["width"] or 1))))
    shape_type = int(row["type"])

    if shape_type in {2, 8, 17} and len(pts) >= 3:
        draw.line(pts + [pts[0]], fill=color, width=width)
        return

    if shape_type == 21 and len(pts) >= 2:
        rect = row_rect(row)
        if rect is not None:
            x1, y1, x2, y2 = rect
            x1 -= origin_x
            x2 -= origin_x
            y1 -= origin_y
            y2 -= origin_y
        else:
            (x1, y1), (x2, y2) = pts[0], pts[1]
        draw.ellipse([min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)], outline=color, width=width)
        return

    if shape_type == 19 and len(pts) >= 2:
        start, end = pts[0], pts[-1]
        draw_arrow(draw, start, end, color, width)
        return

    if len(pts) == 1:
        r = max(1.0, width / 2.0)
        x, y = pts[0]
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
        return

    draw.line(pts, fill=color, width=width)


def translate_points(
    points: list[tuple[float, float]],
    origin_x: float,
    origin_y: float,
) -> list[tuple[float, float]]:
    if abs(origin_x) <= 1e-9 and abs(origin_y) <= 1e-9:
        return points
    return [(x - origin_x, y - origin_y) for x, y in points]


def with_alpha(color: tuple[int, int, int, int], alpha: int) -> tuple[int, int, int, int]:
    return (color[0], color[1], color[2], max(0, min(255, alpha)))


def is_marker_shape(row: sqlite3.Row) -> bool:
    try:
        width = float(row["width"] or 1)
        blend_mode = int(row["blend_mode"] or -1) if "blend_mode" in row.keys() else -1
        return blend_mode == 13 or width >= 10
    except Exception:
        return False


def is_marker_brush(brush: dict[str, Any], width: float) -> bool:
    family = str(brush.get("brushFamilyId", "")).lower()
    return "marker" in family or "highlighter" in family or width >= 10


def draw_arrow(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    color: tuple[int, int, int, int],
    width: int,
) -> None:
    draw.line([start, end], fill=color, width=width)
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return
    ux, uy = dx / length, dy / length
    head_len = max(8.0, width * 4.0)
    head_w = max(5.0, width * 2.0)
    left = (
        end[0] - head_len * ux + head_w * uy,
        end[1] - head_len * uy - head_w * ux,
    )
    right = (
        end[0] - head_len * ux - head_w * uy,
        end[1] - head_len * uy + head_w * ux,
    )
    draw.line([left, end, right], fill=color, width=max(1, width))


def draw_stroke(
    draw: ImageDraw.ImageDraw,
    row: sqlite3.Row,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> None:
    if row["record_json"]:
        draw_legacy_stroke(draw, row, origin_x, origin_y)
    elif row["ink_stroke_json"]:
        draw_ink_stroke(draw, row, origin_x, origin_y)


def draw_legacy_stroke(
    draw: ImageDraw.ImageDraw,
    row: sqlite3.Row,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> None:
    try:
        payload = json.loads(row["record_json"])
    except Exception:
        return

    points = []
    for p in payload.get("points", []):
        try:
            points.append((float(p["x"]), float(p["y"])))
        except Exception:
            continue
    points = dedupe_points(points)
    if not points:
        return
    points = translate_points(points, origin_x, origin_y)

    stroke_type = int(payload.get("type", 1) or 1)
    width = max(1.0, float(payload.get("width", 1.0) or 1.0))
    color = android_color_to_rgba(int(payload.get("color", -16777216)))

    if stroke_type in {2, 11} or width >= 10:
        color = (color[0], color[1], color[2], min(color[3], HIGHLIGHTER_ALPHA))

    draw_polyline(draw, points, color, width)


def draw_ink_stroke(
    draw: ImageDraw.ImageDraw,
    row: sqlite3.Row,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> None:
    try:
        payload = json.loads(row["ink_stroke_json"])
        stroke = payload["stroke"]
        inputs = stroke["inputs"]["inputs"]
        brush = stroke.get("brush", {})
        stw = parse_matrix(payload.get("strokeToWorldTransform"))
        wtv = parse_matrix(payload.get("worldToViewTransform"))
    except Exception:
        return

    points = []
    for p in inputs:
        try:
            x1, y1 = apply_matrix(stw, float(p["x"]), float(p["y"]))
            x2, y2 = apply_matrix(wtv, x1, y1)
            points.append((x2, y2))
        except Exception:
            continue
    points = dedupe_points(points)
    if not points:
        return
    points = translate_points(points, origin_x, origin_y)

    color_value = int(brush.get("color", -16777216))
    color = notein_long_color_to_rgba(color_value)
    width = float(brush.get("size", 1.0) or 1.0)
    scale = 1.0
    if stw and wtv:
        scale = abs(float(stw[0])) * abs(float(wtv[0]))
        if not math.isfinite(scale) or scale <= 0:
            scale = 1.0
    width = max(1.0, width * scale)
    if is_marker_brush(brush, width):
        color = with_alpha(color, HIGHLIGHTER_ALPHA)
    draw_polyline(draw, points, color, width)


def dedupe_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not points:
        return []
    out = [points[0]]
    for p in points[1:]:
        if abs(p[0] - out[-1][0]) > 1e-6 or abs(p[1] - out[-1][1]) > 1e-6:
            out.append(p)
    return out


def draw_polyline(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    color: tuple[int, int, int, int],
    width: float,
) -> None:
    width_px = max(1, int(round(width)))
    if len(points) == 1:
        r = max(1.0, width / 2.0)
        x, y = points[0]
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)
        return

    draw.line(points, fill=color, width=width_px, joint="curve")
    r = max(1.0, width / 2.0)
    for x, y in (points[0], points[-1]):
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color)


def parse_matrix(raw: Optional[str]) -> list[float]:
    if not raw:
        return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    raw = raw.strip().strip('"')
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 9:
        return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    return [float(p) for p in parts]


def apply_matrix(matrix: list[float], x: float, y: float) -> tuple[float, float]:
    return (
        matrix[0] * x + matrix[1] * y + matrix[2],
        matrix[3] * x + matrix[4] * y + matrix[5],
    )


def draw_textbox(
    draw: ImageDraw.ImageDraw,
    row: sqlite3.Row,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> None:
    text = html_to_plain_text(row["text"])
    if not text:
        return

    x = float(row["left"] or 0) - origin_x
    y = float(row["top"] or 0) - origin_y
    text_size = max(10, int(round(float(row["text_size"] or 20))))
    line_height = float(row["line_height"] or text_size)
    line_spacing = max(0, int(round(line_height - text_size)))

    try:
        color = android_color_to_rgba(int(row["default_text_color"]))
    except Exception:
        color = (0, 0, 0, 255)

    font = load_font(text_size)
    draw.multiline_text((x, y), text, fill=color, font=font, spacing=line_spacing)


def draw_comment(
    draw: ImageDraw.ImageDraw,
    row: sqlite3.Row,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> None:
    x, y = comment_anchor_point(row)
    x -= origin_x
    y -= origin_y
    bg = android_color_to_rgba(int(row["bg_color"] or -66396))
    outline = (174, 137, 0, 255)
    rect = [x, y, x + STICKY_NOTE_WIDTH, y + STICKY_NOTE_HEIGHT]
    draw.rounded_rectangle(rect, radius=4, fill=bg, outline=outline, width=1)
    fold = 12
    draw.polygon(
        [
            (x + STICKY_NOTE_WIDTH - fold, y),
            (x + STICKY_NOTE_WIDTH, y),
            (x + STICKY_NOTE_WIDTH, y + fold),
        ],
        fill=(255, 244, 172, bg[3]),
        outline=outline,
    )

    title = str(row["title"] or "").strip()
    if title:
        font = load_font(13)
        draw.multiline_text((x + 8, y + 8), title, fill=(32, 32, 32, 255), font=font, spacing=2)


def load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            try:
                return ImageFont.truetype(candidate, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def draw_image_entity(
    canvas: Image.Image,
    row: sqlite3.Row,
    source_dir: Path,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> None:
    prepared = prepare_image_entity_bitmap(row, source_dir)
    if prepared is None:
        return
    placed, paste_x, paste_y = prepared
    alpha_composite_region(canvas, placed, int(round(paste_x - origin_x)), int(round(paste_y - origin_y)))


def prepare_image_entity_bitmap(
    row: sqlite3.Row,
    source_dir: Path,
) -> Optional[tuple[Image.Image, int, int]]:
    asset_path = image_asset_path(row, source_dir)
    if asset_path is None:
        return None

    left = float(row["left"] or 0)
    top = float(row["top"] or 0)
    right = float(row["right"] or left)
    bottom = float(row["bottom"] or top)
    box_width = max(1, int(round(abs(right - left))))
    box_height = max(1, int(round(abs(bottom - top))))
    rotation = float(row["rotation"] or 0.0)

    # If bounds store the final post-rotation box, estimate the pre-rotation size
    # so rotating does not clip the image into partial halves.
    place_width, place_height = estimate_unrotated_size_for_bounds(
        box_width,
        box_height,
        rotation,
    )

    rendered = load_asset_image(asset_path, target_size=(place_width, place_height))
    if rendered is None:
        return None

    placed = rendered.resize((place_width, place_height), Image.LANCZOS)
    if abs(rotation) > 1e-3:
        placed = placed.rotate(-rotation, expand=True, resample=Image.BICUBIC)
        cx = (left + right) / 2.0
        cy = (top + bottom) / 2.0
        paste_x = int(round(cx - placed.width / 2.0))
        paste_y = int(round(cy - placed.height / 2.0))
    else:
        paste_x = int(round(left))
        paste_y = int(round(top))
    return placed, paste_x, paste_y


def alpha_composite_region(canvas: Image.Image, tile: Image.Image, x: int, y: int) -> None:
    if tile.width <= 0 or tile.height <= 0:
        return
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    overlay.alpha_composite(tile, (x, y))
    result = Image.alpha_composite(canvas, overlay)
    canvas.paste(result)


def draw_images_with_page_spillover(
    page_canvases: list[Image.Image],
    pages: list[PageInfo],
    page_entities: dict[str, dict[str, list[sqlite3.Row]]],
    source_dir: Path,
) -> None:
    if not page_canvases or not pages:
        return

    page_offsets: list[int] = []
    running_y = 0
    for page in pages:
        page_offsets.append(running_y)
        running_y += page.height

    for src_idx, src_page in enumerate(pages):
        src_offset_y = page_offsets[src_idx]
        for row in page_entities[src_page.page_id]["images"]:
            prepared = prepare_image_entity_bitmap(row, source_dir)
            if prepared is None:
                continue
            placed, paste_x, paste_y = prepared

            gx1 = paste_x
            gy1 = src_offset_y + paste_y
            gx2 = gx1 + placed.width
            gy2 = gy1 + placed.height

            for dst_idx, dst_page in enumerate(pages):
                page_left = dst_page.origin_x
                page_top = page_offsets[dst_idx] + dst_page.origin_y
                page_right = page_left + dst_page.width
                page_bottom = page_top + dst_page.height

                ix1 = max(gx1, page_left)
                iy1 = max(gy1, page_top)
                ix2 = min(gx2, page_right)
                iy2 = min(gy2, page_bottom)
                if ix2 <= ix1 or iy2 <= iy1:
                    continue

                crop_box = (
                    int(ix1 - gx1),
                    int(iy1 - gy1),
                    int(ix2 - gx1),
                    int(iy2 - gy1),
                )
                tile = placed.crop(crop_box)
                dst_x = int(ix1 - dst_page.origin_x)
                dst_y = int(iy1 - page_top)
                alpha_composite_region(page_canvases[dst_idx], tile, dst_x, dst_y)


def estimate_unrotated_size_for_bounds(
    bounds_width: int,
    bounds_height: int,
    rotation_degrees: float,
) -> tuple[int, int]:
    if abs(rotation_degrees) <= 1e-3:
        return bounds_width, bounds_height

    theta = math.radians(abs(rotation_degrees))
    c = abs(math.cos(theta))
    s = abs(math.sin(theta))

    det = c * c - s * s
    if abs(det) < 1e-6:
        # Near 45/135 degrees: inversion is unstable; keep conservative fallback.
        return bounds_width, bounds_height

    w = (bounds_width * c - bounds_height * s) / det
    h = (bounds_height * c - bounds_width * s) / det

    if not math.isfinite(w) or not math.isfinite(h) or w <= 1 or h <= 1:
        return bounds_width, bounds_height

    return max(1, int(round(w))), max(1, int(round(h)))


def resolve_asset_path(source_dir: Path, basename: str) -> Optional[Path]:
    if not basename:
        return None
    direct = source_dir / basename
    if direct.exists():
        return direct

    prefixed = source_dir / f"note_image_{basename}"
    if prefixed.exists():
        return prefixed

    stem = Path(basename).stem
    matches = sorted(source_dir.glob(f"note_image_{stem}.*"))
    if matches:
        return matches[0]
    return None


def load_asset_image(path: Path, target_size: Optional[tuple[int, int]] = None) -> Optional[Image.Image]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".svg":
            if cairosvg is not None:
                kwargs: dict[str, Any] = {}
                if target_size is not None:
                    width, height = target_size
                    kwargs["output_width"] = max(1, int(width))
                    kwargs["output_height"] = max(1, int(height))
                png_bytes = cairosvg.svg2png(url=str(path), **kwargs)
                return Image.open(BytesIO(png_bytes)).convert("RGBA")
            return render_svg_with_fitz(path, target_size)
        return Image.open(path).convert("RGBA")
    except Exception:
        return None


def render_svg_with_fitz(path: Path, target_size: Optional[tuple[int, int]] = None) -> Optional[Image.Image]:
    if fitz is None:
        return None
    try:
        svg_doc = fitz.open(stream=svg_bytes_for_fitz(path), filetype="svg")
        svg_page = svg_doc[0]
        if target_size is None:
            width = max(1, int(math.ceil(svg_page.rect.width)))
            height = max(1, int(math.ceil(svg_page.rect.height)))
        else:
            width, height = target_size
            width = max(1, int(width))
            height = max(1, int(height))
        scale_x = width / max(1.0, float(svg_page.rect.width))
        scale_y = height / max(1.0, float(svg_page.rect.height))
        pix = svg_page.get_pixmap(matrix=fitz.Matrix(scale_x, scale_y), alpha=True)
        return Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
    except Exception:
        return None


def svg_bytes_for_fitz(path: Path) -> bytes:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except Exception:
        return data

    text = inline_svg_styles(text)
    return text.encode("utf-8")


def inline_svg_styles(text: str) -> str:
    gradients = svg_gradient_colors(text)
    styles: dict[str, str] = {}
    for name, body in re.findall(r"\.([A-Za-z0-9_-]+)\s*\{([^{}]+)\}", text, flags=re.S):
        declarations: list[str] = []
        for part in body.split(";"):
            part = part.strip()
            if not part or ":" not in part:
                continue
            key, value = [piece.strip() for piece in part.split(":", 1)]
            if key not in {
                "fill",
                "stroke",
                "stroke-width",
                "opacity",
                "fill-opacity",
                "stroke-opacity",
                "clip-path",
            }:
                continue
            gradient = re.fullmatch(r"url\(#([^)]+)\)", value)
            if gradient is not None and gradient.group(1) in gradients:
                value = gradients[gradient.group(1)]
            declarations.append(f"{key}:{value}")
        if declarations:
            styles[name] = ";".join(declarations)

    if not styles:
        return text

    def replace_class(match: re.Match[str]) -> str:
        declarations = [
            styles[class_name]
            for class_name in match.group(1).split()
            if class_name in styles
        ]
        if not declarations:
            return match.group(0)
        return f'style="{";".join(declarations)}"'

    return re.sub(r'class="([^"]+)"', replace_class, text)


def svg_gradient_colors(text: str) -> dict[str, str]:
    colors: dict[str, str] = {}
    for match in re.finditer(
        r'<linearGradient[^>]*id="([^"]+)"[^>]*>(.*?)</linearGradient>',
        text,
        flags=re.S,
    ):
        stops = re.findall(r'stop-color="([^"]+)"', match.group(2))
        if stops:
            colors[match.group(1)] = stops[len(stops) // 2]
    return colors


def write_vector_pdf(output_pdf: Path, note_data: NoteRenderData, source_dir: Path) -> None:
    if fitz is None:
        page_images = render_note_images(note_data, source_dir)
        write_pdf(output_pdf, page_images)
        return

    doc = fitz.open()
    for page_info in note_data.pages:
        pdf_page = doc.new_page(
            width=pdf_units(page_info.width),
            height=pdf_units(page_info.height),
        )
        draw_pdf_page_background(pdf_page, page_info)
        draw_pdf_non_image_items(
            pdf_page=pdf_page,
            page_info=page_info,
            entities=note_data.page_entities[page_info.page_id],
            layer_index=note_data.layer_index,
        )

    draw_pdf_images_with_page_spillover(doc, note_data.pages, note_data.page_entities, source_dir)

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_pdf)
    doc.close()


def draw_pdf_page_background(pdf_page: Any, page: PageInfo) -> None:
    bg_rgb, bg_alpha = pdf_color_and_opacity(page.background_rgba)
    pdf_page.draw_rect(
        pdf_page.rect,
        color=None,
        fill=bg_rgb,
        fill_opacity=bg_alpha,
        overlay=False,
    )

    theme = parse_json_object(page.page_row["paper_theme"])
    paper_style = theme.get("paperStyle", {}) if isinstance(theme, dict) else {}
    style_type = str(paper_style.get("type", ""))
    spacing = float(paper_style.get("requiredItemSpace", 0) or 0)
    left_pad = float(paper_style.get("leftPadding", 0) or 0)
    top_pad = float(paper_style.get("topPadding", 0) or 0)
    line_rgb = (215 / 255, 215 / 255, 215 / 255)
    line_alpha = 70 / 255

    visible_left = page.origin_x
    visible_right = page.origin_x + page.width
    visible_top = page.origin_y
    visible_bottom = page.origin_y + page.height

    if "Square" in style_type and spacing >= 10:
        x = first_pattern_position(left_pad, spacing, visible_left)
        while x < visible_right:
            px = pdf_units(x - page.origin_x)
            pdf_page.draw_line(
                (px, 0),
                (px, pdf_units(page.height)),
                color=line_rgb,
                width=pdf_line_width(1),
                stroke_opacity=line_alpha,
            )
            x += spacing

    if ("Square" in style_type or any(token in style_type for token in ("Line", "Ruled", "Horizontal"))) and spacing >= 10:
        y = first_pattern_position(top_pad, spacing, visible_top)
        while y < visible_bottom:
            py = pdf_units(y - page.origin_y)
            pdf_page.draw_line(
                (0, py),
                (pdf_units(page.width), py),
                color=line_rgb,
                width=pdf_line_width(1),
                stroke_opacity=line_alpha,
            )
            y += spacing


def draw_pdf_non_image_items(
    pdf_page: Any,
    page_info: PageInfo,
    entities: dict[str, list[sqlite3.Row]],
    layer_index: dict[str, int],
) -> None:
    items: list[tuple[int, int, str, sqlite3.Row]] = []
    for kind in ("shapes", "textboxes", "comments", "strokes"):
        for row in entities.get(kind, []):
            row_layer = row["layer_id"] if "layer_id" in row.keys() else None
            items.append((layer_index.get(row_layer, 999), safe_int(row["creation_time"]), kind, row))
    items.sort(key=lambda item: (item[0], item[1], item[2]))

    for _, _, kind, row in items:
        if kind == "shapes":
            draw_pdf_shape(pdf_page, page_info, row)
        elif kind == "textboxes":
            draw_pdf_textbox(pdf_page, page_info, row)
        elif kind == "comments":
            draw_pdf_comment(pdf_page, page_info, row)
        elif kind == "strokes":
            draw_pdf_stroke(pdf_page, page_info, row)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def pdf_color_and_opacity(color: tuple[int, int, int, int]) -> tuple[tuple[float, float, float], float]:
    return (color[0] / 255, color[1] / 255, color[2] / 255), color[3] / 255


def pdf_units(value: float) -> float:
    return value * PDF_UNIT_SCALE


def pdf_line_width(width: float) -> float:
    return max(0.25, pdf_units(width))


def pdf_points(
    points: list[tuple[float, float]],
    page_info: PageInfo,
) -> list[tuple[float, float]]:
    return [
        (pdf_units(x - page_info.origin_x), pdf_units(y - page_info.origin_y))
        for x, y in points
    ]


def pdf_rect_from_logical(
    page_info: PageInfo,
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> Any:
    return fitz.Rect(
        pdf_units(left - page_info.origin_x),
        pdf_units(top - page_info.origin_y),
        pdf_units(right - page_info.origin_x),
        pdf_units(bottom - page_info.origin_y),
    )


def draw_pdf_shape(pdf_page: Any, page_info: PageInfo, row: sqlite3.Row) -> None:
    try:
        pts = [(float(p["x"]), float(p["y"])) for p in json.loads(row["points"])]
    except Exception:
        return
    if not pts:
        return

    pts = pdf_points(pts, page_info)
    color = android_color_to_rgba(int(row["color"]))
    if is_marker_shape(row):
        color = with_alpha(color, HIGHLIGHTER_ALPHA)
    rgb, alpha = pdf_color_and_opacity(color)
    width = pdf_line_width(float(row["width"] or 1))
    shape_type = int(row["type"])

    if shape_type in {2, 8, 17} and len(pts) >= 3:
        pdf_page.draw_polyline(
            pts + [pts[0]],
            color=rgb,
            width=width,
            lineCap=1,
            lineJoin=1,
            closePath=False,
            stroke_opacity=alpha,
        )
        return

    if shape_type == 21 and len(pts) >= 2:
        rect = row_rect(row)
        if rect is None:
            x1, y1 = pts[0]
            x2, y2 = pts[1]
        else:
            left, top, right, bottom = rect
            x1 = pdf_units(left - page_info.origin_x)
            y1 = pdf_units(top - page_info.origin_y)
            x2 = pdf_units(right - page_info.origin_x)
            y2 = pdf_units(bottom - page_info.origin_y)
        pdf_page.draw_oval(
            fitz.Rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
            color=rgb,
            width=width,
            stroke_opacity=alpha,
        )
        return

    if shape_type == 19 and len(pts) >= 2:
        draw_pdf_arrow(pdf_page, pts[0], pts[-1], rgb, alpha, width)
        return

    draw_pdf_polyline(pdf_page, pts, color, width)


def draw_pdf_arrow(
    pdf_page: Any,
    start: tuple[float, float],
    end: tuple[float, float],
    rgb: tuple[float, float, float],
    alpha: float,
    width: float,
) -> None:
    pdf_page.draw_line(start, end, color=rgb, width=width, lineCap=1, stroke_opacity=alpha)
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return
    ux, uy = dx / length, dy / length
    head_len = max(pdf_units(10.0), width * 5.0)
    head_w = max(pdf_units(6.0), width * 2.5)
    left = (end[0] - head_len * ux + head_w * uy, end[1] - head_len * uy - head_w * ux)
    right = (end[0] - head_len * ux - head_w * uy, end[1] - head_len * uy + head_w * ux)
    pdf_page.draw_polyline([left, end, right], color=rgb, width=width, lineCap=1, lineJoin=1, stroke_opacity=alpha)


def draw_pdf_stroke(pdf_page: Any, page_info: PageInfo, row: sqlite3.Row) -> None:
    if row["record_json"]:
        draw_pdf_legacy_stroke(pdf_page, page_info, row)
    elif row["ink_stroke_json"]:
        draw_pdf_ink_stroke(pdf_page, page_info, row)


def draw_pdf_legacy_stroke(pdf_page: Any, page_info: PageInfo, row: sqlite3.Row) -> None:
    try:
        payload = json.loads(row["record_json"])
    except Exception:
        return

    points = []
    for p in payload.get("points", []):
        try:
            points.append((float(p["x"]), float(p["y"])))
        except Exception:
            continue
    points = dedupe_points(points)
    if not points:
        return

    points = pdf_points(points, page_info)
    stroke_type = int(payload.get("type", 1) or 1)
    logical_width = max(0.5, float(payload.get("width", 1.0) or 1.0))
    width = pdf_line_width(logical_width)
    color = android_color_to_rgba(int(payload.get("color", -16777216)))
    if stroke_type in {2, 11} or logical_width >= 10:
        color = with_alpha(color, min(color[3], HIGHLIGHTER_ALPHA))
    draw_pdf_polyline(pdf_page, points, color, width)


def draw_pdf_ink_stroke(pdf_page: Any, page_info: PageInfo, row: sqlite3.Row) -> None:
    try:
        payload = json.loads(row["ink_stroke_json"])
        stroke = payload["stroke"]
        inputs = stroke["inputs"]["inputs"]
        brush = stroke.get("brush", {})
        stw = parse_matrix(payload.get("strokeToWorldTransform"))
        wtv = parse_matrix(payload.get("worldToViewTransform"))
    except Exception:
        return

    points = []
    for p in inputs:
        try:
            x1, y1 = apply_matrix(stw, float(p["x"]), float(p["y"]))
            x2, y2 = apply_matrix(wtv, x1, y1)
            points.append((x2, y2))
        except Exception:
            continue
    points = dedupe_points(points)
    if not points:
        return

    color = notein_long_color_to_rgba(int(brush.get("color", -16777216)))
    width = float(brush.get("size", 1.0) or 1.0)
    scale = 1.0
    if stw and wtv:
        scale = abs(float(stw[0])) * abs(float(wtv[0]))
        if not math.isfinite(scale) or scale <= 0:
            scale = 1.0
    logical_width = max(0.5, width * scale)
    if is_marker_brush(brush, logical_width):
        color = with_alpha(color, HIGHLIGHTER_ALPHA)
    draw_pdf_polyline(pdf_page, pdf_points(points, page_info), color, pdf_line_width(logical_width))


def draw_pdf_polyline(
    pdf_page: Any,
    points: list[tuple[float, float]],
    color: tuple[int, int, int, int],
    width: float,
) -> None:
    rgb, alpha = pdf_color_and_opacity(color)
    if len(points) == 1:
        r = max(pdf_units(1.0), width / 2.0)
        x, y = points[0]
        pdf_page.draw_oval(
            fitz.Rect(x - r, y - r, x + r, y + r),
            color=None,
            fill=rgb,
            fill_opacity=alpha,
        )
        return

    pdf_page.draw_polyline(
        points,
        color=rgb,
        width=width,
        lineCap=1,
        lineJoin=1,
        closePath=False,
        stroke_opacity=alpha,
    )


def draw_pdf_textbox(pdf_page: Any, page_info: PageInfo, row: sqlite3.Row) -> None:
    text = html_to_plain_text(row["text"])
    if not text:
        return

    rect = row_rect(row)
    if rect is None:
        return
    left, top, right, bottom = rect
    text_size = max(10, float(row["text_size"] or 20))
    line_height_raw = float(row["line_height"] or 0)
    line_height = pdf_units(line_height_raw) if line_height_raw >= text_size else None
    try:
        color = android_color_to_rgba(int(row["default_text_color"]))
    except Exception:
        color = (0, 0, 0, 255)
    rgb, alpha = pdf_color_and_opacity(color)
    pdf_page.insert_textbox(
        pdf_rect_from_logical(page_info, left, top, right, bottom),
        text,
        fontsize=pdf_units(text_size),
        fontname="helv",
        color=rgb,
        fill_opacity=alpha,
        lineheight=line_height,
    )


def draw_pdf_comment(pdf_page: Any, page_info: PageInfo, row: sqlite3.Row) -> None:
    x, y = comment_anchor_point(row)
    x = pdf_units(x - page_info.origin_x)
    y = pdf_units(y - page_info.origin_y)
    bg = android_color_to_rgba(int(row["bg_color"] or -66396))
    bg_rgb, bg_alpha = pdf_color_and_opacity(bg)
    outline = (174 / 255, 137 / 255, 0)
    width = pdf_units(STICKY_NOTE_WIDTH)
    height = pdf_units(STICKY_NOTE_HEIGHT)
    rect = fitz.Rect(x, y, x + width, y + height)
    pdf_page.draw_rect(rect, color=outline, fill=bg_rgb, width=pdf_line_width(1), fill_opacity=bg_alpha)
    fold = pdf_units(12)
    pdf_page.draw_polyline(
        [
            (x + width - fold, y),
            (x + width - fold, y + fold),
            (x + width, y + fold),
        ],
        color=outline,
        width=pdf_line_width(1),
    )
    title = str(row["title"] or "").strip()
    if title:
        pdf_page.insert_textbox(
            fitz.Rect(x + pdf_units(8), y + pdf_units(7), x + width - pdf_units(8), y + height - pdf_units(5)),
            title,
            fontsize=pdf_units(12),
            fontname="helv",
            color=(32 / 255, 32 / 255, 32 / 255),
        )


def draw_pdf_images_with_page_spillover(
    doc: Any,
    pages: list[PageInfo],
    page_entities: dict[str, dict[str, list[sqlite3.Row]]],
    source_dir: Path,
) -> None:
    page_offsets: list[float] = []
    running_y = 0.0
    for page in pages:
        page_offsets.append(running_y)
        running_y += page.height

    for src_idx, src_page in enumerate(pages):
        src_offset_y = page_offsets[src_idx]
        for row in page_entities[src_page.page_id].get("images", []):
            asset_path = image_asset_path(row, source_dir)
            if asset_path is None:
                continue

            rotation = float(row["rotation"] or 0.0)
            if asset_path.suffix.lower() == ".svg" and abs(rotation) <= 1e-3:
                left, top, right, bottom = image_bounds(row)
                draw_pdf_image_rect_on_pages(
                    doc,
                    pages,
                    page_offsets,
                    src_offset_y,
                    (left, top, right, bottom),
                    lambda pdf_page, rect, path=asset_path: draw_pdf_svg(pdf_page, rect, path),
                )
                continue

            if abs(rotation) <= 1e-3 and asset_path.suffix.lower() != ".svg":
                left, top, right, bottom = image_bounds(row)
                data = asset_path.read_bytes()
                draw_pdf_image_rect_on_pages(
                    doc,
                    pages,
                    page_offsets,
                    src_offset_y,
                    (left, top, right, bottom),
                    lambda pdf_page, rect, stream=data: pdf_page.insert_image(rect, stream=stream, keep_proportion=False),
                )
                continue

            prepared = prepare_image_entity_bitmap(row, source_dir)
            if prepared is None:
                continue
            placed, paste_x, paste_y = prepared
            stream = pil_image_to_png_bytes(placed)
            draw_pdf_image_rect_on_pages(
                doc,
                pages,
                page_offsets,
                src_offset_y,
                (paste_x, paste_y, paste_x + placed.width, paste_y + placed.height),
                lambda pdf_page, rect, stream=stream: pdf_page.insert_image(rect, stream=stream, keep_proportion=False),
            )


def draw_pdf_image_rect_on_pages(
    doc: Any,
    pages: list[PageInfo],
    page_offsets: list[float],
    src_offset_y: float,
    rect: tuple[float, float, float, float],
    draw_func: Any,
) -> None:
    left, top, right, bottom = normalize_rect(*rect)
    gx1 = left
    gy1 = src_offset_y + top
    gx2 = right
    gy2 = src_offset_y + bottom

    for dst_idx, dst_page in enumerate(pages):
        page_left = dst_page.origin_x
        page_top = page_offsets[dst_idx] + dst_page.origin_y
        page_right = page_left + dst_page.width
        page_bottom = page_top + dst_page.height
        if gx2 <= page_left or gx1 >= page_right or gy2 <= page_top or gy1 >= page_bottom:
            continue

        target_rect = fitz.Rect(
            pdf_units(gx1 - dst_page.origin_x),
            pdf_units(gy1 - page_top),
            pdf_units(gx2 - dst_page.origin_x),
            pdf_units(gy2 - page_top),
        )
        draw_func(doc[dst_idx], target_rect)


def draw_pdf_svg(pdf_page: Any, rect: Any, path: Path) -> None:
    if fitz is None:
        return
    svg_doc = fitz.open(stream=svg_bytes_for_fitz(path), filetype="svg")
    pdf_doc = None
    try:
        pdf_doc = fitz.open("pdf", svg_doc.convert_to_pdf())
        pdf_page.show_pdf_page(rect, pdf_doc, 0, keep_proportion=False)
    finally:
        if pdf_doc is not None:
            pdf_doc.close()
        svg_doc.close()


def image_asset_path(row: sqlite3.Row, source_dir: Path) -> Optional[Path]:
    uri = row["uri"] or ""
    basename = os.path.basename(uri)
    return resolve_asset_path(source_dir, basename)


def image_bounds(row: sqlite3.Row) -> tuple[float, float, float, float]:
    left = float(row["left"] or 0)
    top = float(row["top"] or 0)
    right = float(row["right"] or left)
    bottom = float(row["bottom"] or top)
    return normalize_rect(left, top, right, bottom)


def pil_image_to_png_bytes(image: Image.Image) -> bytes:
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def write_pdf(output_pdf: Path, pages: list[Image.Image]) -> None:
    if not pages:
        raise SystemExit("No pages were rendered; cannot write PDF.")

    converted = [page.convert("RGB") for page in pages]
    converted[0].save(
        output_pdf,
        format="PDF",
        save_all=True,
        append_images=converted[1:],
        resolution=300.0,
    )


if __name__ == "__main__":
    raise SystemExit(main())
