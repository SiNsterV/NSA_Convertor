from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class SyncConfig:
    provider: Literal["local", "gdrive"] = "local"
    note_format: Literal["noteshelf", "notein"] = "noteshelf"
    source: str = ""
    output_dir: str = ""
    source_name: str = ""
    recursive: bool = True
    force: bool = False
    # CLI overrides; the GUI never loads the repository's environment or files.
    cache_dir: str | None = None
    legacy_state: str | None = None
    oauth_client: str | None = None

    def validate(self) -> None:
        if self.provider not in ("local", "gdrive"):
            raise ValueError("Choose Local folder or Google Drive.")
        if self.note_format not in ("noteshelf", "notein"):
            raise ValueError("Choose Noteshelf or Notein.")
        if not self.source.strip() or not self.output_dir.strip():
            raise ValueError("Choose both a source folder and a PDF destination.")


@dataclass
class ProgressEvent:
    stage: str
    message: str = ""
    file: str = ""
    completed: int = 0
    total: int = 0
    status: str = ""


@dataclass
class FileResult:
    file: str
    status: str
    message: str = ""


@dataclass
class SyncResult:
    status: str = "success"
    converted: int = 0
    skipped: int = 0
    failed: int = 0
    found: int = 0
    downloaded: int = 0
    files: list[FileResult] = field(default_factory=list)
    message: str = ""
    last_success: str | None = None


class Cancelled(Exception):
    """A job was cancelled without committing its current output."""


class AuthenticationRequired(Exception):
    """The account needs an interactive browser sign-in."""
