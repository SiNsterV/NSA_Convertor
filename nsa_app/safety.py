from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zipfile

from .models import Cancelled

_cancel = ContextVar("nsa_cancel", default=lambda: False)
_page = ContextVar("nsa_page", default=lambda current, total: None)
_held: dict[str, int] = {}


def check_cancel() -> None:
    if _cancel.get()():
        raise Cancelled("Sync cancelled. Previously completed PDFs were kept.")


def report_page(current: int, total: int) -> None:
    check_cancel()
    _page.get()(current, total)


@contextmanager
def job_context(cancel, page=lambda current, total: None):
    c = _cancel.set(cancel)
    p = _page.set(page)
    try:
        yield
    finally:
        _cancel.reset(c)
        _page.reset(p)


class OutputLock:
    """OS-backed lock released automatically when a process exits."""
    def __init__(self, directory: Path):
        self.directory = Path(directory).resolve()
        self.key = os.path.normcase(str(self.directory))
        self.stream = None

    def __enter__(self):
        if self.key in _held:
            _held[self.key] += 1
            return self
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stream = (self.directory / ".notebridge.lock").open("a+b")
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise RuntimeError("Another conversion is using this PDF folder. Wait for it to finish.") from exc
        _held[self.key] = 1
        return self

    def __exit__(self, *args):
        _held[self.key] -= 1
        if _held[self.key] == 0:
            del _held[self.key]
        if self.stream:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_UN)
            self.stream.close()


def safe_component(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .")
    if not value or value in (".", ".."):
        value = "Untitled"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I):
        value = "_" + value
    if len(value) > 100:
        value = value[:80] + "-" + hashlib.sha256(name.encode()).hexdigest()[:12]
    return value


def contained(root: Path, relative: Path) -> Path:
    root = root.resolve()
    target = (root / relative).resolve()
    if target == root or root not in target.parents:
        raise ValueError("A file path would leave the selected destination.")
    return target


def validate_archive(zf: zipfile.ZipFile) -> None:
    infos = zf.infolist()
    if len(infos) > 100_000 or sum(i.file_size for i in infos) > 2 * 1024**3:
        raise ValueError("This backup exceeds the beta archive limit (100,000 entries / 2 GiB expanded).")
    seen = set()
    for info in infos:
        check_cancel()
        name = info.filename.replace("\\", "/")
        parts = PurePosixPath(name).parts
        if (not parts or name.startswith("/") or ".." in parts or ":" in name
                or stat.S_ISLNK(info.external_attr >> 16)):
            raise ValueError("The backup contains an unsafe archive path.")
        normalized = "/".join(parts).casefold()
        if normalized in seen:
            raise ValueError("The backup contains duplicate archive paths.")
        seen.add(normalized)
        if info.flag_bits & 1:
            raise ValueError("Encrypted backup archives are not supported.")
        if info.file_size > 512 * 1024**2 or info.file_size / max(1, info.compress_size) > 1000:
            raise ValueError("An item in this backup exceeds the beta archive size limit.")


@contextmanager
def atomic_pdf(destination: str | Path):
    destination = Path(destination).resolve()
    with OutputLock(destination.parent):
        fd, name = tempfile.mkstemp(prefix=".nsa-", suffix=".pdf", dir=destination.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            yield temporary
            check_cancel()
            import pymupdf
            with pymupdf.open(temporary) as document:
                if document.page_count < 1:
                    raise ValueError("The converter did not produce any PDF pages.")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
