from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil

from . import RENDERER_VERSION
from .drive import DriveClient, SourceItem, candidate
from .models import AuthenticationRequired, Cancelled, FileResult, ProgressEvent, SyncConfig, SyncResult
from .safety import OutputLock, atomic_pdf, check_cancel, contained, job_context, safe_component
from .storage import atomic_json, profile_dir, read_json, setup_logging


def file_hash(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check_cancel()
            digest.update(chunk)
    return digest.hexdigest()


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, AuthenticationRequired):
        return str(exc)
    if isinstance(exc, PermissionError):
        return "Access denied. Close the PDF if it is open, or choose a writable folder, then retry."
    if isinstance(exc, FileNotFoundError):
        return "A source file or folder is no longer available. Check the selected folder and retry."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "There is not enough disk space. Free some space and retry."
    if type(exc).__name__ == "HttpError":
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (403, 404):
            return "Google Drive denied access or could not find the folder/file. Check sharing permissions."
        return "Google Drive could not complete the request. Check your connection and retry."
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "The connection timed out. Check your internet connection and retry."
    return str(exc) or "This backup could not be converted. Try exporting a fresh backup."


def local_inventory(config: SyncConfig) -> list[SourceItem]:
    root = Path(config.source).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("The source folder does not exist or is not accessible.")
    output = Path(config.output_dir).expanduser().resolve()
    if output == root:
        raise ValueError("Choose a PDF destination different from the source folder.")
    result = []
    def on_error(error):
        raise error
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=on_error):
        check_cancel()
        dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink()
                         and (Path(folder) / d).resolve() != output and not d.startswith(".notein_extract_"))
        for name in sorted(files):
            source = Path(folder) / name
            if source.is_symlink():
                continue
            is_candidate = candidate(name, config.note_format, "application/zip")
            if is_candidate:
                relative = source.relative_to(root)
                result.append(SourceItem(relative.as_posix(), name, relative, source))
        if not config.recursive:
            break
    return result


def output_relative(item: SourceItem, state: dict, output: Path, note_format: str) -> Path:
    stem = Path(item.name).stem
    if note_format == "notein" and item.name.lower().endswith(".notein.zip"):
        stem = Path(stem).stem
    parts = [safe_component(part) for part in item.relative.parent.parts]
    desired = Path(*parts) / (safe_component(stem) + ".pdf")
    allocation_key = item.key + "\n" + desired.as_posix()
    allocated = state.setdefault("names", {})
    if allocation_key in allocated:
        return Path(allocated[allocation_key])
    reserved = {p.casefold() for p in allocated.values()}
    target = desired
    # Never replace a PDF that this profile has not allocated.
    counter = 0
    while target.as_posix().casefold() in reserved or contained(output, target).exists():
        suffix = hashlib.sha256((item.key + str(counter)).encode()).hexdigest()[:10]
        target = desired.with_name(desired.stem + "-" + suffix + ".pdf")
        counter += 1
    allocated[allocation_key] = target.as_posix()
    return target


def render(source: Path, output: Path, note_format: str) -> None:
    if note_format == "notein":
        from notein_extract import notein_to_pdf
        notein_to_pdf(source, output, verbose=False)
    else:
        from nsa_convertor import nsa_to_pdf
        nsa_to_pdf(str(source), str(output), verbose=False, desired_highlighter_ratio=5.0,
                   highlighter_opacity=0.35, smooth=True, epsilon=0.8)


def sync(config: SyncConfig, emit=lambda event: None, cancelled=lambda: False,
         drive_factory=DriveClient, renderer=render) -> SyncResult:
    result = SyncResult()
    setup_logging()
    current_name = ""
    def page(current, total):
        emit(ProgressEvent("rendering", f"Page {current} of {total}", current_name, current, total))

    with job_context(cancelled, page):
        try:
            config.validate()
            check_cancel()
            output = Path(config.output_dir).expanduser().resolve()
            profile = profile_dir(config)
            state_path = profile / "state.json"
            state = read_json(state_path, {"version": 1, "files": {}, "names": {}})
            if state.get("version") != 1 or not isinstance(state.get("files"), dict) or not isinstance(state.get("names"), dict):
                raise ValueError("The saved sync state is not supported by this app version.")
            result.last_success = state.get("last_success")
            # A private profile keeps GUI downloads separate from CLI directories.
            cache = Path(config.cache_dir).resolve() if config.cache_dir else profile / "cache"
            cache.mkdir(parents=True, exist_ok=True)
            fingerprint = hashlib.sha256(json.dumps([RENDERER_VERSION, config.note_format, 5.0, 0.35, True, 0.8]).encode()).hexdigest()
            with OutputLock(output):
                emit(ProgressEvent("scanning", "Looking for backed-up notes…"))
                drive = drive_factory(config.oauth_client) if config.provider == "gdrive" else None
                items = drive.inventory(config.source, config.note_format, config.recursive) if drive else local_inventory(config)
                result.found = len(items)
                # Legacy state is read only for output ownership. Hashes are revalidated
                # with the new renderer fingerprint; never overwrite the legacy file.
                legacy = read_json(Path(config.legacy_state)) if config.legacy_state else {}
                for index, item in enumerate(items):
                    check_cancel()
                    current_name = item.relative.as_posix()
                    emit(ProgressEvent("processing", "Checking backup", current_name, index, len(items)))
                    try:
                        old = state["files"].get(item.key, {})
                        if not state.get("legacy_checked") and legacy:
                            legacy_source = item.path or (Path(config.cache_dir or "nsa_files") / item.relative)
                            entry = legacy.get("files", {}).get(str(legacy_source.resolve()), {})
                            old_pdf = Path(entry.get("pdf_path", ""))
                            if entry.get("pdf_path") and old_pdf.resolve().is_relative_to(output):
                                wanted = Path(*[safe_component(p) for p in item.relative.parent.parts]) / (safe_component(Path(item.name).stem) + ".pdf")
                                if old_pdf.resolve() == (output / wanted).resolve():
                                    state["names"][item.key + "\n" + wanted.as_posix()] = wanted.as_posix()
                        relative_pdf = output_relative(item, state, output, config.note_format)
                        destination = contained(output, relative_pdf)
                        # Persist allocations even when a job fails, keeping collision names stable.
                        atomic_json(state_path, state)
                        if drive:
                            cache_name = hashlib.sha256(item.key.encode()).hexdigest() + (".nsa" if config.note_format == "noteshelf" else ".notein")
                            source = contained(cache, Path(cache_name))
                            cache_valid = source.is_file() and item.checksum and file_hash(source) == item.checksum
                            if not cache_valid:
                                emit(ProgressEvent("downloading", "Downloading backup", current_name, index, len(items)))
                                drive.download(item, source, lambda current, total: emit(ProgressEvent("download", "Downloading", current_name, current, total)))
                                result.downloaded += 1
                        else:
                            source = item.path
                        before = file_hash(source)
                        if (not config.force and old.get("hash") == before and old.get("renderer") == fingerprint
                                and old.get("pdf") == relative_pdf.as_posix() and destination.is_file()
                                and old.get("pdf_hash") == file_hash(destination)):
                            result.skipped += 1
                            result.files.append(FileResult(current_name, "skipped", "Already up to date"))
                            emit(ProgressEvent("file", "Already up to date", current_name, index + 1, len(items), "skipped"))
                            continue
                        # Snapshot local inputs so a concurrently changing backup never
                        # feeds a partially updated ZIP to the renderer.
                        snapshot = profile / ("snapshot.nsa" if config.note_format == "noteshelf" else "snapshot.notein")
                        try:
                            shutil.copyfile(source, snapshot)
                            if file_hash(snapshot) != before:
                                raise ValueError("The backup changed while being read. Sync again to retry.")
                            with atomic_pdf(destination) as temporary:
                                renderer(snapshot, temporary, config.note_format)
                                check_cancel()
                                if file_hash(source) != before:
                                    raise ValueError("The backup changed during conversion. Sync again to retry.")
                            state["files"][item.key] = {"hash": before, "renderer": fingerprint,
                                "pdf": relative_pdf.as_posix(), "pdf_hash": file_hash(destination),
                                "modified": item.modified}
                            atomic_json(state_path, state)
                        finally:
                            snapshot.unlink(missing_ok=True)
                        result.converted += 1
                        result.files.append(FileResult(current_name, "converted", relative_pdf.as_posix()))
                        emit(ProgressEvent("file", "PDF saved", current_name, index + 1, len(items), "converted"))
                    except (Cancelled, AuthenticationRequired):
                        raise
                    except Exception as exc:
                        message = friendly_error(exc)
                        result.failed += 1
                        result.files.append(FileResult(current_name, "failed", message))
                        emit(ProgressEvent("file", message, current_name, index + 1, len(items), "failed"))
                check_cancel()
                state["legacy_checked"] = True
                if result.failed:
                    result.status = "partial_failure"
                    result.message = "Some notes could not be converted. Sync again to retry them."
                else:
                    result.status = "no_files" if not items else "up_to_date" if not result.converted else "success"
                    result.message = {"no_files": "No supported notes found. Check the format and backup folder.",
                                      "up_to_date": "Already up to date.", "success": "Your PDFs are ready."}[result.status]
                    result.last_success = datetime.now(timezone.utc).isoformat()
                    state["last_success"] = result.last_success
                atomic_json(state_path, state)
        except Cancelled as exc:
            result.status, result.message = "cancelled", str(exc)
        except AuthenticationRequired as exc:
            result.status, result.message = "authentication_required", friendly_error(exc)
        except Exception as exc:
            result.status, result.message = "failed", friendly_error(exc)
        logging.getLogger("nsa").info("job_status=%s converted=%d skipped=%d failed=%d", result.status, result.converted, result.skipped, result.failed)
        emit(ProgressEvent("finished", result.message, status=result.status))
    return result
