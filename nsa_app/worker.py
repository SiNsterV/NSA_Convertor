"""Spawn-safe worker entry points. No Qt objects cross this boundary."""
from dataclasses import asdict

from .auth import disconnect
from .drive import DriveClient
from .engine import friendly_error, sync
from .models import Cancelled, SyncConfig
from .safety import job_context


def run_job(kind: str, payload: dict, messages, cancel) -> None:
    try:
        with job_context(cancel.is_set):
            if kind == "sync":
                result = sync(SyncConfig(**payload), lambda event: messages.put(("progress", asdict(event))), cancel.is_set)
                value = asdict(result)
            elif kind == "connect":
                client = DriveClient(interactive=True)
                value = client.folder("root")
            elif kind == "folders":
                client = DriveClient()
                value = client.children(payload["folder_id"], payload.get("token"), folders_only=True)
            elif kind == "folder":
                value = DriveClient().folder(payload["folder_id"])
            elif kind == "disconnect":
                value = {"revoked": disconnect()}
            else:
                raise ValueError("Unknown worker operation")
        messages.put(("result", value))
    except Cancelled as exc:
        messages.put(("error", {"message": str(exc), "type": "Cancelled"}))
    except Exception as exc:
        messages.put(("error", {"message": friendly_error(exc), "type": type(exc).__name__}))
