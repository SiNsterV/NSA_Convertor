from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlparse

from .models import AuthenticationRequired

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
SERVICE = "NoteBridge.GoogleDrive"


def client_path(override: str | None = None) -> Path:
    if override:
        path = Path(override)
    elif not getattr(sys, "frozen", False) and os.environ.get("NSA_OAUTH_CLIENT_FILE"):
        path = Path(os.environ["NSA_OAUTH_CLIENT_FILE"])
    elif not getattr(sys, "frozen", False):
        candidates = (Path.cwd() / "credentials.json", Path(__file__).resolve().parents[1] / "credentials.json")
        path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    else:
        path = Path(__file__).parent / "resources" / "oauth_client.json"
    if not path.is_file():
        raise AuthenticationRequired(
            "Google sign-in is not configured in this build. You can use Local folder now. "
            "Maintainers: follow docs/RELEASING.md to configure the desktop OAuth client."
        )
    return path


def load_client(override: str | None = None) -> dict:
    data = json.loads(client_path(override).read_text(encoding="utf-8"))
    client = data.get("installed", {})
    if not client.get("client_id") or client.get("auth_uri") != "https://accounts.google.com/o/oauth2/auth" or client.get("token_uri") != "https://oauth2.googleapis.com/token":
        raise ValueError("Use a Google OAuth client JSON downloaded for a Desktop app.")
    return data


def credential_key(data: dict) -> str:
    return hashlib.sha256(data["installed"]["client_id"].encode()).hexdigest()


def credential_store():
    # Explicit OS backend: never fall back to plaintext keyring plugins.
    if os.name == "nt":
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()
    import keyring
    backend = keyring.get_keyring()
    if backend.priority <= 0 or "plaintext" in type(backend).__module__.lower():
        raise RuntimeError("A secure system credential store is required for Google sign-in.")
    return backend


def credentials(override: str | None = None, interactive: bool = False):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from google_auth_oauthlib.flow import InstalledAppFlow

    data = load_client(override)
    key = credential_key(data)
    token_file = os.environ.get("NOTEBRIDGE_TOKEN_FILE")
    backend = None if token_file else credential_store()
    token_path = Path(token_file) if token_file else None
    raw = token_path.read_text(encoding="utf-8") if token_path and token_path.is_file() else backend.get_password(SERVICE, key) if backend else None
    creds = None
    if raw:
        try:
            creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
        except (ValueError, KeyError):
            creds = None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise AuthenticationRequired("Your Google session needs attention. Click Reconnect to sign in.")
        flow = InstalledAppFlow.from_client_config(data, SCOPES, autogenerate_code_verifier=True)
        creds = flow.run_local_server(
            host="127.0.0.1", port=int(os.environ.get("NOTEBRIDGE_OAUTH_PORT", "0")),
            open_browser=os.environ.get("NOTEBRIDGE_OAUTH_OPEN_BROWSER", "1") != "0", timeout_seconds=180,
            authorization_prompt_message="Complete sign-in in your browser.",
            success_message="NoteBridge is connected. You can close this browser tab.",
            access_type="offline", prompt="consent",
        )
    serialized = creds.to_json()
    if token_path:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(serialized, encoding="utf-8")
        if os.name != "nt":
            token_path.chmod(0o600)
    else:
        backend.set_password(SERVICE, key, serialized)
    return creds


def disconnect(override: str | None = None) -> bool:
    import requests
    key = credential_key(load_client(override))
    token_file = os.environ.get("NOTEBRIDGE_TOKEN_FILE")
    backend = None if token_file else credential_store()
    token_path = Path(token_file) if token_file else None
    raw = token_path.read_text(encoding="utf-8") if token_path and token_path.is_file() else backend.get_password(SERVICE, key) if backend else None
    revoked = True
    try:
        if raw:
            data = json.loads(raw)
            token = data.get("refresh_token") or data.get("token")
            if token:
                response = requests.post("https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=15)
                revoked = response.status_code == 200
    except (requests.RequestException, ValueError):
        revoked = False
    finally:
        if token_path:
            token_path.unlink(missing_ok=True)
        elif raw:
            backend.delete_password(SERVICE, key)
    return revoked


def parse_folder(value: str) -> str:
    value = value.strip()
    if value == "root":
        return value
    if value.startswith(("https://", "http://")):
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname not in ("drive.google.com", "www.drive.google.com"):
            raise ValueError("Paste a Google Drive folder link, or its folder ID.")
        match = re.search(r"/folders/([A-Za-z0-9_-]+)", parsed.path)
        value = match.group(1) if match else parse_qs(parsed.query).get("id", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", value):
        raise ValueError("That is not a valid Google Drive folder link or folder ID.")
    return value
