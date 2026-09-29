from __future__ import annotations

import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import tempfile

from .models import SyncConfig


def app_data() -> Path:
    root = os.environ.get("NOTEBRIDGE_DATA_DIR")
    path = Path(root) if root else Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "NoteBridge"
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return dict(default or {})
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value
    except (ValueError, UnicodeError):
        # Keep the original for local recovery; a new state is safe to regenerate.
        backup = path.with_suffix(path.suffix + ".corrupt")
        path.replace(backup)
        logging.getLogger("nsa").warning("Recovered an unreadable settings/state file")
        return dict(default or {})


def profile_id(config: SyncConfig) -> str:
    source = config.source if config.provider == "gdrive" else os.path.normcase(str(Path(config.source).resolve()))
    data = [config.provider, config.note_format, source, os.path.normcase(str(Path(config.output_dir).resolve()))]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:24]


def profile_dir(config: SyncConfig) -> Path:
    path = app_data() / "profiles" / profile_id(config)
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging() -> None:
    logger = logging.getLogger("nsa")
    if logger.handlers:
        return
    handler = RotatingFileHandler(app_data() / "app.log", maxBytes=512_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def export_diagnostics(destination: Path) -> None:
    """Export only allowlisted facts; never raw exceptions, paths, config or tokens."""
    import importlib.metadata
    import platform
    from . import __version__

    versions = {}
    for name in ("PyMuPDF", "PySide6", "google-api-python-client"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    logs = []
    for path in sorted(app_data().glob("app.log*")):
        # Application logging contains only status counters and exception class names.
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if " job_status=" in line:
                logs.append(line)
    atomic_json(destination, {"app_version": __version__, "os": platform.system(),
                              "os_release": platform.release(), "python": platform.python_version(),
                              "dependencies": versions, "events": logs[-200:]})
