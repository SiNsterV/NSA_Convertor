"""Synthetic notebooks only. No real notes, names or credentials are used."""
from io import BytesIO
import json
from pathlib import Path
import plistlib
import sqlite3
import struct
import tempfile
import zipfile

from PIL import Image
import pymupdf


def png_bytes():
    stream = BytesIO()
    Image.new("RGB", (32, 32), (38, 112, 170)).save(stream, format="PNG")
    return stream.getvalue()


def make_nsa(path: Path, pages=1, rich=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"pages": [{"uuid": f"page-{i}", "pdfKitPageRect": "{{0, 0}, {400, 600}}"} for i in range(pages)]}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        if rich:
            with pymupdf.open() as background:
                page = background.new_page(width=200, height=300)
                page.draw_rect(page.rect, color=None, fill=(0.97, 0.96, 0.90))
                page.insert_text((15, 25), "Synthetic Noteshelf", fontsize=12)
                archive.writestr("Notebook/Templates/paper.ns_pdf", background.tobytes())
            for i in range(pages):
                meta["pages"][i]["associatedPDFFileName"] = "paper.ns_pdf"
                meta["pages"][i]["associatedPDFKitPageIndex"] = 1
            with tempfile.TemporaryDirectory() as temp:
                database = Path(temp) / "annotations"
                connection = sqlite3.connect(database)
                connection.execute("""CREATE TABLE annotation (
                    id TEXT, annotationType INTEGER, penType INTEGER, strokeWidth REAL,
                    strokeColor INTEGER, strokeOpacity REAL, stroke_segments_v3 BLOB,
                    shape_data TEXT, boundingRect_x REAL, boundingRect_y REAL,
                    boundingRect_w REAL, boundingRect_h REAL, imgTxMatrix TEXT,
                    txMatrix TEXT, emojiName TEXT)""")
                for pen, width, color, y in [(1, 3, 0x202020, 130), (2, 20, 0xFFDF00, 200)]:
                    blob = b"".join(struct.pack("<7f", 40 + k*40, y + (k % 2)*20,
                                               80 + k*40, y + ((k+1) % 2)*20, 0, width, 0) for k in range(5))
                    connection.execute("INSERT INTO annotation(id,annotationType,penType,strokeWidth,strokeColor,stroke_segments_v3) VALUES(?,0,?,?,?,?)",
                                       (f"pen{pen}", pen, width, color, blob))
                connection.execute("INSERT INTO annotation(id,annotationType,boundingRect_x,boundingRect_y,boundingRect_w,boundingRect_h) VALUES('picture',2,40,320,100,100)")
                connection.execute("INSERT INTO annotation(annotationType,strokeColor,strokeWidth,shape_data) VALUES(5,?,3,?)",
                                   (0x16654F, json.dumps({"controlPoints": [[200, 340], [300, 340], [300, 420]], "numberOfSides": 3})))
                connection.commit()
                connection.close()
                for i in range(pages):
                    archive.writestr(f"Notebook/Annotations/page-{i}", database.read_bytes())
                archive.writestr("Notebook/Resources/picture.png", png_bytes())
        archive.writestr("Notebook/Document.plist", plistlib.dumps(meta))
    return path


def make_notein(path: Path, rich=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        database = Path(temp) / "note_database_synthetic_db"
        db = sqlite3.connect(database)
        db.executescript("""
            CREATE TABLE NoteContentEntity(page_list TEXT, page_layer_list TEXT);
            CREATE TABLE PageEntity(id TEXT, paper_spec TEXT, paper_theme TEXT);
            CREATE TABLE StrokeEntity(id TEXT, page_id TEXT, layer_id TEXT, creation_time INTEGER, record_json TEXT, ink_stroke_json TEXT);
            CREATE TABLE ShapeEntity(id TEXT, page_id TEXT, layer_id TEXT, creation_time INTEGER);
            CREATE TABLE TextBoxEntity(id TEXT, page_id TEXT, layer_id TEXT, creation_time INTEGER, text TEXT, left REAL, top REAL, right REAL, bottom REAL, text_size REAL, line_height REAL, default_text_color INTEGER);
            CREATE TABLE ImageEntity(id TEXT, page_id TEXT, layer_id TEXT, creation_time INTEGER, uri TEXT, left REAL, top REAL, right REAL, bottom REAL, rotation REAL);
            CREATE TABLE CommentEntity(id TEXT, page_id TEXT, title TEXT, quote_id TEXT, creation_time INTEGER);
            CREATE TABLE QuoteEntity(id TEXT, layer_id TEXT, creation_time INTEGER, label_rect TEXT, rect_list TEXT, bg_color INTEGER, color INTEGER);
        """)
        db.execute("INSERT INTO NoteContentEntity VALUES(?, ?)", (json.dumps(["page1"]), json.dumps(["layer1"])))
        db.execute("INSERT INTO PageEntity VALUES(?,?,?)", ("page1", json.dumps({"width": 400, "height": 600}),
                   json.dumps({"baseTheme": {"color": -526353}, "paperStyle": {"type": "blank"}})))
        if rich:
            db.execute("INSERT INTO TextBoxEntity VALUES('text','page1','layer1',0,'Synthetic Notein',30,30,380,100,24,28,-16777216)")
            for index, width, color, y in [(1, 3, -16777216, 150), (2, 20, -256, 220)]:
                payload = {"type": 2 if index == 2 else 1, "width": width, "color": color,
                           "points": [{"x": 40 + k*45, "y": y + (k % 2)*20} for k in range(6)]}
                db.execute("INSERT INTO StrokeEntity VALUES(?, 'page1', 'layer1', ?, ?, NULL)", (f"stroke{index}", index, json.dumps(payload)))
            db.execute("INSERT INTO ImageEntity VALUES('image','page1','layer1',3,'picture.png',40,320,140,420,0)")
        db.commit()
        db.close()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(database.name, database.read_bytes())
            if rich:
                archive.writestr("picture.png", png_bytes())
    return path
