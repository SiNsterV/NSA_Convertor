from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pymupdf
import pytest

from nsa_app.engine import build_notein_folder_paths, file_hash, notein_folder_path, render, sync
from nsa_app.models import AuthenticationRequired, SyncConfig
from nsa_app.safety import OutputLock, contained, safe_component, validate_archive
from nsa_app.storage import profile_dir
from tests.fixtures import make_nsa, make_notein


def config(tmp_path, note_format="noteshelf"):
    source = tmp_path / "source"
    source.mkdir()
    return SyncConfig(source=str(source), output_dir=str(tmp_path / "pdfs"), note_format=note_format)


def test_incremental_changes_missing_destination_force_and_renderer(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    source = make_nsa(Path(cfg.source) / "Class" / "lesson.nsa")
    assert sync(cfg).converted == 1
    pdf = Path(cfg.output_dir) / "Class" / "lesson.pdf"
    assert pdf.is_file()
    first = pdf.stat().st_mtime_ns
    assert sync(cfg).status == "up_to_date"
    assert pdf.stat().st_mtime_ns == first
    make_nsa(source, pages=2)
    assert sync(cfg).converted == 1
    with pymupdf.open(pdf) as document:
        assert document.page_count == 2
    pdf.unlink()
    assert sync(cfg).converted == 1
    assert sync(replace(cfg, force=True)).converted == 1
    monkeypatch.setattr("nsa_app.engine.RENDERER_VERSION", "next-renderer")
    assert sync(cfg).converted == 1
    new = replace(cfg, output_dir=str(tmp_path / "elsewhere"))
    assert sync(new).converted == 1


def test_missing_and_renamed_sources_preserve_pdfs(tmp_path):
    cfg = config(tmp_path)
    source = make_nsa(Path(cfg.source) / "old.nsa")
    assert sync(cfg).converted == 1
    source.rename(source.with_name("new.nsa"))
    assert sync(cfg).converted == 1
    assert {p.name for p in Path(cfg.output_dir).glob("*.pdf")} == {"old.pdf", "new.pdf"}
    source.with_name("new.nsa").unlink()
    assert sync(cfg).status == "no_files"
    assert len(list(Path(cfg.output_dir).glob("*.pdf"))) == 2


def test_bad_backup_does_not_abort_batch_or_advance_success(tmp_path):
    cfg = config(tmp_path)
    good = make_nsa(Path(cfg.source) / "good.nsa")
    assert sync(cfg).status == "success"
    before = json.loads((profile_dir(cfg) / "state.json").read_text())["last_success"]
    (good.parent / "broken.nsa").write_bytes(b"not a zip")
    result = sync(cfg)
    assert (result.status, result.failed, result.skipped) == ("partial_failure", 1, 1)
    assert result.last_success == before


@pytest.mark.parametrize("mode", ["raise", "cancel", "mutate", "permission", "disk_full"])
def test_failure_preserves_previous_pdf(tmp_path, mode):
    cfg = config(tmp_path)
    source = make_nsa(Path(cfg.source) / "note.nsa")
    assert sync(cfg).converted == 1
    pdf = Path(cfg.output_dir) / "note.pdf"
    before = pdf.read_bytes()
    stop = [False]
    def renderer(snapshot, output, note_format):
        render(snapshot, output, note_format)
        if mode == "raise":
            raise ValueError("synthetic error")
        if mode == "permission":
            raise PermissionError("locked")
        if mode == "disk_full":
            raise OSError(28, "disk full")
        if mode == "cancel":
            stop[0] = True
        if mode == "mutate":
            make_nsa(source, pages=3)
    result = sync(replace(cfg, force=True), cancelled=lambda: stop[0], renderer=renderer)
    assert result.status in ("partial_failure", "cancelled")
    assert pdf.read_bytes() == before
    assert not list(Path(cfg.output_dir).glob(".nsa-*.pdf"))


def test_notein_name_collision_is_stable(tmp_path):
    cfg = config(tmp_path, "notein")
    make_notein(Path(cfg.source) / "note.notein")
    make_notein(Path(cfg.source) / "note.notein.zip")
    result = sync(cfg)
    assert result.converted == 2, result
    names = sorted(p.name for p in Path(cfg.output_dir).glob("*.pdf"))
    assert len(set(names)) == 2
    assert sync(cfg).skipped == 2
    assert sorted(p.name for p in Path(cfg.output_dir).glob("*.pdf")) == names


def test_notein_metadata_rebuilds_nested_and_trash_paths(tmp_path):
    root = tmp_path / "source"
    root.mkdir()

    def bundle(name, member, metadata):
        path = root / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member, json.dumps(metadata))
        return path

    bundle("folder-root.notein", "folder_root.json", {"id": "root", "title": "School"})
    bundle("folder-child.notein", "folder_child.json", {"id": "child", "title": "Math", "parentId": "root"})
    note = bundle("note.notein", "note_meta.json", {"parentId": "child"})
    trash = bundle("trash.notein", "note_meta.json", {"parentId": "child", "inTrashBin": True})

    folder_paths = build_notein_folder_paths([*root.iterdir()])
    assert folder_paths == {"root": Path("School"), "child": Path("School") / "Math"}
    assert notein_folder_path(note, folder_paths) == Path("School") / "Math"
    assert notein_folder_path(trash, folder_paths) == Path("_Trash")


def test_notein_blob_stroke_is_rendered(tmp_path):
    cfg = config(tmp_path, "notein")
    make_notein(Path(cfg.source) / "note.notein", blob=True)

    assert sync(cfg).converted == 1
    with pymupdf.open(Path(cfg.output_dir) / "note.pdf") as document:
        assert len(document[0].get_drawings()) >= 2


def test_notein_pdf_background_is_rendered(tmp_path):
    cfg = config(tmp_path, "notein")
    make_notein(Path(cfg.source) / "note.notein", pdf_background=True)

    assert sync(cfg).converted == 1
    with pymupdf.open(Path(cfg.output_dir) / "note.pdf") as document:
        assert "Embedded PDF background" in document[0].get_text()


def test_unowned_pdf_is_not_overwritten(tmp_path):
    cfg = config(tmp_path)
    make_nsa(Path(cfg.source) / "note.nsa")
    output = Path(cfg.output_dir)
    output.mkdir()
    (output / "note.pdf").write_bytes(b"user file")
    assert sync(cfg).converted == 1
    assert (output / "note.pdf").read_bytes() == b"user file"


def test_directory_lock_between_processes(tmp_path):
    with OutputLock(tmp_path):
        code = "from nsa_app.safety import OutputLock; from pathlib import Path;\nwith OutputLock(Path(" + repr(str(tmp_path)) + ")): pass"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert "Another conversion" in result.stderr
    with OutputLock(tmp_path):
        pass


@pytest.mark.parametrize("name", ["../outside", "C:/outside", "/outside", "folder/../../outside"])
def test_unsafe_archives_are_rejected(tmp_path, name):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, "no")
    with zipfile.ZipFile(path) as archive, pytest.raises(ValueError):
        validate_archive(archive)


def test_archive_bomb_and_containment(tmp_path):
    path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large", b"0" * 2_000_000)
    with zipfile.ZipFile(path) as archive, pytest.raises(ValueError):
        validate_archive(archive)
    with pytest.raises(ValueError):
        contained(tmp_path, Path("../outside"))
    assert safe_component("CON.txt") == "_CON.txt"
    assert safe_component('bad:name?. ') == "bad_name_"


def test_gui_state_does_not_read_legacy_by_default(tmp_path):
    cfg = config(tmp_path)
    make_nsa(Path(cfg.source) / "note.nsa")
    legacy = tmp_path / "sync_state.json"
    legacy.write_text("private unrelated state")
    assert sync(cfg).converted == 1
    assert legacy.read_text() == "private unrelated state"


def test_nonexistent_source_reports_failure(tmp_path):
    result = sync(SyncConfig(source=str(tmp_path / "missing"), output_dir=str(tmp_path / "pdfs")))
    assert result.status == "failed"
    assert result.last_success is None
