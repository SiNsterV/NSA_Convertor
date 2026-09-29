from pathlib import Path

import pytest

from nsa_app.auth import parse_folder
from nsa_app.drive import DriveClient, FOLDER, SourceItem
from nsa_app.engine import sync
from nsa_app.models import AuthenticationRequired, SyncConfig
from tests.fixtures import make_nsa


def test_folder_url_validation():
    value = "1rH0XV439y1LVcTHmJf5YAFJOoImWhPUd"
    assert parse_folder("https://drive.google.com/drive/folders/" + value + "?usp=sharing") == value
    assert parse_folder(value) == value
    with pytest.raises(ValueError):
        parse_folder("https://evil.example/drive/folders/" + value)
    with pytest.raises(ValueError):
        parse_folder("abc' or true")


class FakeDrive:
    items = []
    downloads = 0
    broken = False

    def __init__(self, *args):
        pass

    def inventory(self, *args):
        return self.items

    def download(self, item, destination, progress):
        type(self).downloads += 1
        if self.broken:
            raise OSError("download interrupted")
        make_nsa(destination)


def test_drive_failure_and_fresh_inventory(tmp_path):
    cfg = SyncConfig(provider="gdrive", source="synthetic-folder", output_dir=str(tmp_path / "pdfs"))
    FakeDrive.items = [SourceItem("id1", "one.nsa", Path("one.nsa"))]
    FakeDrive.broken = False
    assert sync(cfg, drive_factory=FakeDrive).converted == 1
    previous = (Path(cfg.output_dir) / "one.pdf").read_bytes()
    FakeDrive.broken = True
    result = sync(cfg, drive_factory=FakeDrive)
    assert result.status == "partial_failure"
    assert (Path(cfg.output_dir) / "one.pdf").read_bytes() == previous
    FakeDrive.items = []
    assert sync(cfg, drive_factory=FakeDrive).status == "no_files"
    FakeDrive.broken = False


def test_revoked_access_never_reports_success(tmp_path):
    cfg = SyncConfig(provider="gdrive", source="synthetic-folder", output_dir=str(tmp_path / "pdfs"))
    def denied(*args):
        raise AuthenticationRequired("Reconnect")
    result = sync(cfg, drive_factory=denied)
    assert result.status == "authentication_required"
    assert result.last_success is None


def test_pagination_and_duplicate_drive_folders():
    client = object.__new__(DriveClient)
    client.folder = lambda folder_id: {"id": folder_id}
    def children(folder, token=None):
        if folder == "root" and not token:
            return {"files": [{"id": "f1", "name": "Class", "mimeType": FOLDER}], "nextPageToken": "page2"}
        if folder == "root":
            return {"files": [{"id": "f2", "name": "Class", "mimeType": FOLDER}]}
        return {"files": [{"id": folder + "note", "name": "note.nsa", "mimeType": "application/zip"}]}
    client.children = children
    items = client.inventory("root", "noteshelf")
    assert len(items) == 2
    assert len({item.relative.as_posix() for item in items}) == 2
