#!/usr/bin/env python3
"""Compatible command-line entry point for the desktop app's sync engine."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import time

from dotenv import load_dotenv

from nsa_app.auth import credentials, parse_folder
from nsa_app.engine import friendly_error, sync
from nsa_app.models import ProgressEvent, SyncConfig


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Sync Noteshelf or Notein backups and convert them to PDF")
    parser.add_argument("--provider", choices=["local", "gdrive", "dropbox", "onedrive", "webdav"], default="local")
    parser.add_argument("--local-dir", default=None, help="Local source folder, or Google Drive download cache")
    parser.add_argument("--output-dir", default=os.getenv("DEFAULT_PATH", "./output"))
    parser.add_argument("--folder-id", help="Google Drive folder ID or folder URL; defaults to My Drive")
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--watch", action="store_true", help="CLI only: repeat sync until Ctrl+C")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--notein", action="store_true")
    parser.add_argument("--force", action="store_true", help="Reconvert unchanged notes")
    parser.add_argument("--credentials", default="credentials.json", help="Your Google Desktop OAuth client file (CLI only)")
    args = parser.parse_args(argv)
    if args.provider not in ("local", "gdrive"):
        parser.error(f"Provider '{args.provider}' is not implemented. Use local or gdrive.")
    if args.interval < 1:
        parser.error("--interval must be at least 1 second")
    local_dir = args.local_dir or ("./notein_files" if args.notein else "./nsa_files")
    try:
        source = parse_folder(args.folder_id or "root") if args.provider == "gdrive" else local_dir
        config = SyncConfig(provider=args.provider, note_format="notein" if args.notein else "noteshelf",
                            source=source, output_dir=args.output_dir, recursive=not args.no_recursive,
                            force=args.force, cache_dir=local_dir if args.provider == "gdrive" else None,
                            legacy_state=str(Path("sync_state.json").resolve()),
                            oauth_client=str(Path(args.credentials).resolve()) if args.provider == "gdrive" else None)
        if args.provider == "gdrive":
            if not args.quiet and os.getenv("NO_CONFIRMATION", "0") != "1":
                response = input(f"Sync Google Drive folder {source} and convert its backups? [y/N] ")
                if response.strip().lower() not in ("y", "yes"):
                    print("Sync cancelled.")
                    return 0
            credentials(config.oauth_client, interactive=True)
        def progress(event: ProgressEvent):
            if not args.quiet and event.stage in ("scanning", "file", "finished"):
                print(f"{event.file + ': ' if event.file else ''}{event.message}")
        while True:
            result = sync(config, progress)
            failed = result.status in ("failed", "partial_failure", "authentication_required")
            if not args.quiet:
                print(f"Downloaded: {result.downloaded}; converted: {result.converted}; skipped: {result.skipped}; failed: {result.failed}")
                print(f"Output directory: {Path(args.output_dir).resolve()}")
            elif failed:
                print(result.message)
            if not args.watch:
                return 1 if failed else 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Sync stopped.")
        return 130
    except Exception as exc:
        print(f"Error: {friendly_error(exc)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
