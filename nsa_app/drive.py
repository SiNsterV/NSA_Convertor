from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import socket
import time

from .auth import credentials
from .models import AuthenticationRequired
from .safety import check_cancel, safe_component

FOLDER = "application/vnd.google-apps.folder"


@dataclass
class SourceItem:
    key: str
    name: str
    relative: Path
    path: Path | None = None
    checksum: str | None = None
    modified: str = ""


def candidate(name: str, note_format: str, mime: str = "") -> bool:
    lower = name.lower()
    if note_format == "noteshelf":
        return lower.endswith(".nsa")
    return (lower.endswith((".notein", ".zip")) or ("." not in name and mime in
            ("application/zip", "application/octet-stream", "application/x-zip-compressed")))


def execute(request):
    from googleapiclient.errors import HttpError
    for attempt in range(4):
        check_cancel()
        try:
            return request.execute(num_retries=0)
        except HttpError as exc:
            status = exc.resp.status
            if status == 401:
                raise AuthenticationRequired("Google access has expired. Reconnect your account.") from exc
            if status not in (429, 500, 502, 503, 504) and not (status == 403 and b"rateLimitExceeded" in exc.content):
                raise
            if attempt == 3:
                raise
        except (TimeoutError, ConnectionError, socket.timeout, OSError):
            if attempt == 3:
                raise
        for _ in range(10 * 2**attempt):
            check_cancel()
            time.sleep(0.1)


class DriveClient:
    def __init__(self, oauth_client: str | None = None, interactive: bool = False):
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build
        http = AuthorizedHttp(credentials(oauth_client, interactive), http=httplib2.Http(timeout=30))
        self.service = build("drive", "v3", http=http, cache_discovery=False)

    def folder(self, folder_id: str) -> dict:
        item = execute(self.service.files().get(fileId=folder_id, fields="id,name,mimeType,trashed", supportsAllDrives=True))
        if item.get("trashed") or item.get("mimeType") != FOLDER:
            raise ValueError("Choose an accessible Google Drive folder that is not in the trash.")
        return item

    def children(self, folder_id: str, token: str | None = None, folders_only: bool = False) -> dict:
        query = f"'{folder_id}' in parents and trashed = false"
        if folders_only:
            query += f" and mimeType = '{FOLDER}'"
        return execute(self.service.files().list(
            q=query, pageToken=token, pageSize=100, orderBy="folder,name",
            fields="nextPageToken,files(id,name,mimeType,md5Checksum,modifiedTime)",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ))

    def inventory(self, folder_id: str, note_format: str, recursive: bool = True) -> list[SourceItem]:
        self.folder(folder_id)
        result = []
        pending = [(folder_id, Path())]
        visited = set()
        while pending:
            check_cancel()
            parent, relative = pending.pop()
            if parent in visited:
                continue
            visited.add(parent)
            children = []
            token = None
            while True:
                response = self.children(parent, token)
                children.extend(response.get("files", []))
                token = response.get("nextPageToken")
                if not token:
                    break
            # Folder identity suffixes prevent merging duplicate Drive folders.
            folders = [c for c in children if c["mimeType"] == FOLDER]
            counts = {}
            for folder in folders:
                name = safe_component(folder["name"]).casefold()
                counts[name] = counts.get(name, 0) + 1
            for item in sorted(children, key=lambda x: x["id"]):
                if item["mimeType"] == FOLDER:
                    name = safe_component(item["name"])
                    if counts[name.casefold()] > 1:
                        name += "-" + hashlib.sha256(item["id"].encode()).hexdigest()[:8]
                    if recursive:
                        pending.append((item["id"], relative / name))
                elif candidate(item["name"], note_format, item["mimeType"]):
                    result.append(SourceItem(item["id"], item["name"], relative / safe_component(item["name"]),
                                             checksum=item.get("md5Checksum"), modified=item.get("modifiedTime", "")))
        return result

    def download(self, item: SourceItem, destination: Path, progress=lambda current, total: None) -> None:
        from googleapiclient.http import MediaIoBaseDownload
        from googleapiclient.errors import HttpError
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with temporary.open("wb") as stream:
                downloader = MediaIoBaseDownload(stream, self.service.files().get_media(fileId=item.key), chunksize=1024 * 1024)
                done = False
                while not done:
                    check_cancel()
                    status, done = downloader.next_chunk(num_retries=3)
                    if status:
                        progress(status.resumable_progress, status.total_size or 0)
            check_cancel()
            if item.checksum:
                with temporary.open("rb") as stream:
                    checksum = hashlib.file_digest(stream, "md5").hexdigest()
                if checksum != item.checksum:
                    raise ValueError("The backup changed during download. Sync again to retry.")
            temporary.replace(destination)
        except HttpError as exc:
            if exc.resp.status == 401:
                raise AuthenticationRequired("Google access has expired. Reconnect your account.") from exc
            raise
        finally:
            temporary.unlink(missing_ok=True)
