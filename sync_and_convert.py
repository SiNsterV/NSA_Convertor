#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Automated sync script for Noteshelf .nsa files from cloud storage.
Supports: Google Drive (gdrive), Local file sync
"""

import os
import sys
import time
import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

# Import conversion function from our converter
from nsa_convertor import nsa_to_pdf

from dotenv import load_dotenv

load_dotenv()

DEFAULT_PATH = os.getenv("DEFAULT_PATH", r"./output")
NO_CONFIRMATION = os.getenv("NO_CONFIRMATION", "0") == "1"
NOTEIN_EXTENSIONS = {".notein", ".zip"}
NOTEIN_ZIP_MIME_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
}


def output_stem_for_source(source_file: Path, notein: bool = False) -> str:
    if notein and source_file.suffix.lower() == ".zip" and source_file.stem.lower().endswith(".notein"):
        return Path(source_file.stem).stem
    return source_file.stem


def is_notein_bundle(path: Path) -> bool:
    if not path.is_file() or not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return any(
                Path(name).name.startswith("note_database")
                and Path(name).name.endswith("_db")
                for name in zf.namelist()
            )
    except Exception:
        return False


def is_notein_drive_candidate(file: dict) -> bool:
    name = file.get("name", "")
    lower_name = name.lower()
    suffix = Path(name).suffix.lower()
    if lower_name.endswith(".nsa"):
        return False
    if suffix in NOTEIN_EXTENSIONS or lower_name.endswith(".notein.zip"):
        return True
    return "." not in name and file.get("mimeType") in NOTEIN_ZIP_MIME_TYPES


class CloudSyncManager:
    """Base class for cloud storage sync managers"""
    
    def __init__(self, local_dir: str, output_dir: str, state_file: str = "sync_state.json", notein: bool = False):
        self.local_dir = Path(local_dir)
        self.output_dir = Path(output_dir)
        self.state_file = Path(state_file)
        self.notein = notein
        self.file_label = "Notein" if notein else "NSA"
        self.source_extension = "Notein bundles" if notein else ".nsa files"
        self.state = self._load_state()
        
        # Create directories if they don't exist
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def _load_state(self) -> Dict:
        """Load sync state from JSON file with corruption recovery"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                # Corrupted state file - rename and start fresh
                corrupt_path = self.state_file.with_suffix('.json.corrupt')
                self.state_file.rename(corrupt_path)
                print(f"Warning: Corrupted state file moved to {corrupt_path}")
        return {"files": {}, "last_sync": None}
    
    def _save_state(self):
        """Save sync state to JSON file atomically"""
        temp_file = self.state_file.with_suffix('.json.tmp')
        try:
            with open(temp_file, 'w') as f:
                json.dump(self.state, f, indent=2)
            # Atomic rename
            temp_file.replace(self.state_file)
        except Exception as e:
            print(f"Warning: Failed to save state: {e}")
            if temp_file.exists():
                temp_file.unlink()
    
    def _file_hash(self, filepath: Path) -> str:
        """Calculate MD5 hash of file"""
        hash_md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    
    def _needs_conversion(self, source_file: Path, pdf_path: Optional[Path] = None) -> bool:
        """Check if source file needs conversion"""
        # Normalize path for consistent state keys
        file_key = str(source_file.resolve())
        if pdf_path is None:
            pdf_file = self.output_dir / (output_stem_for_source(source_file, self.notein) + ".pdf")
        else:
            pdf_file = pdf_path
        
        # Convert if PDF doesn't exist
        if not pdf_file.exists():
            return True
        
        # Convert if file hash changed since last conversion
        current_hash = self._file_hash(source_file)
        converted_hash = self.state["files"].get(file_key, {}).get("converted_hash")
        
        return current_hash != converted_hash
    
    def convert_file(self, source_file: Path, pdf_path: Optional[Path] = None, verbose: bool = True, metadata: dict = None) -> bool:
        """Convert single source file to PDF"""
        nsa_file = source_file
        try:
            if metadata is None:
                metadata = {}
                
            if pdf_path is None:
                pdf_file = self.output_dir / (output_stem_for_source(source_file, self.notein) + ".pdf")
            else:
                pdf_file = pdf_path
            
            # Create parent directory if it doesn't exist
            pdf_file.parent.mkdir(parents=True, exist_ok=True)
            
            if verbose:
                print(f"Converting: {source_file.name}")
            
            if self.notein:
                from notein_extract import notein_to_pdf

                notein_to_pdf(source_file, pdf_file, verbose=False)
            else:
                nsa_to_pdf(
                    str(source_file),
                    str(pdf_file),
                    verbose=False,  # Don't show conversion details to avoid clutter
                    desired_highlighter_ratio=5.0,
                    highlighter_opacity=0.35,
                    smooth=True,
                    epsilon=0.8
                )
            
            if verbose:
                # Show relative path from output_dir
                try:
                    rel_path = pdf_file.relative_to(self.output_dir)
                    print(f"  ✓ Saved to: {rel_path}")
                except ValueError:
                    print(f"  ✓ Saved to: {pdf_file.name}")
            
            # Update state - track conversion hash separately
            file_key = str(source_file.resolve())
            current_hash = self._file_hash(source_file)
            existing_state = self.state["files"].get(file_key, {})
            
            self.state["files"][file_key] = {
                **existing_state,  # Preserve download_hash and other metadata
                "converted_hash": current_hash,
                "last_converted": datetime.now().isoformat(),
                "pdf_path": str(pdf_file),
            }
            if metadata:
                self.state["files"][file_key]["gdrive_md5"] = metadata.get('md5_checksum')
                self.state["files"][file_key]["gdrive_modified"] = metadata.get('modified_time')
            self._save_state()
            
            return True
        except Exception as e:
            print(f"❌ Error converting {nsa_file.name}: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def sync_local_files(self, verbose: bool = True) -> int:
        """Process all source files in local directory recursively"""
        converted = 0
        if self.notein:
            nsa_files = [
                path for path in self.local_dir.rglob("*")
                if path.is_file() and is_notein_bundle(path)
            ]
        else:
            nsa_files = list(self.local_dir.rglob("*.nsa"))
        
        if verbose:
            print(f"Found {len(nsa_files)} {self.source_extension} in {self.local_dir}")
        
        for nsa_file in nsa_files:
            # Mirror folder structure in output
            try:
                rel_path = nsa_file.relative_to(self.local_dir)
                pdf_path = self.output_dir / rel_path.parent / (output_stem_for_source(nsa_file, self.notein) + ".pdf")
            except ValueError:
                pdf_path = self.output_dir / (output_stem_for_source(nsa_file, self.notein) + ".pdf")
            
            if self._needs_conversion(nsa_file, pdf_path):
                if self.convert_file(nsa_file, pdf_path, verbose=verbose):
                    converted += 1
        
        self.state["last_sync"] = datetime.now().isoformat()
        self._save_state()
        
        return converted


class GoogleDriveSync(CloudSyncManager):
    """Google Drive sync implementation"""
    
    def __init__(
        self,
        local_dir: str,
        output_dir: str,
        folder_id: Optional[str] = None,
        recursive: bool = True,
        notein: bool = False,
    ):
        super().__init__(local_dir, output_dir, notein=notein)
        self.folder_id = folder_id
        self.recursive = recursive
        self.service = None
        self.file_metadata = {}  # Track folder paths for each file
    
    def authenticate(self):
        """Authenticate with Google Drive API"""
        try:
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
            import pickle
            
            SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
            creds = None
            
            # Token file stores user's access and refresh tokens
            if os.path.exists('token.pickle'):
                with open('token.pickle', 'rb') as token:
                    creds = pickle.load(token)
            
            # If no valid credentials, let user log in
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        'credentials.json', SCOPES)
                    creds = flow.run_local_server(port=0)
                
                # Save credentials for next run
                with open('token.pickle', 'wb') as token:
                    pickle.dump(creds, token)
            
            self.service = build('drive', 'v3', credentials=creds)
            return True
        except ImportError:
            print("Error: Google Drive API not installed.")
            print("Install with: pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client")
            return False
        except Exception as e:
            print(f"Authentication error: {e}")
            return False
    
    def _get_all_folders_recursive(self, parent_id: str, parent_path: str = "") -> Dict[str, str]:
        """Get all folder IDs recursively under a parent folder with their paths
        Returns: Dict mapping folder_id to folder_path
        """
        folder_map = {parent_id: parent_path}
        
        # Get subfolders with pagination
        query = f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder'"
        page_token = None
        subfolders = []
        
        while True:
            results = self.service.files().list(
                q=query,
                fields="nextPageToken, files(id, name)",
                pageToken=page_token
            ).execute()
            
            subfolders.extend(results.get('files', []))
            page_token = results.get('nextPageToken')
            if not page_token:
                break
        
        # Recursively get subfolders
        for folder in subfolders:
            folder_path = os.path.join(parent_path, folder['name']) if parent_path else folder['name']
            subfolder_map = self._get_all_folders_recursive(folder['id'], folder_path)
            folder_map.update(subfolder_map)
        
        return folder_map
    
    def _display_folder_structure(self, folder_map: Dict[str, str]) -> None:
        """Display folder structure in a tree-like format"""
        print("\n" + "="*60)
        print("Google Drive Folder Structure:")
        print("="*60)
        
        # Get unique folder paths and sort them
        folder_paths = [path for path in folder_map.values() if path]
        folder_paths.sort()
        
        if not folder_paths:
            print("  (Root folder only)")
        else:
            # Build a tree structure
            tree = {}
            for path in folder_paths:
                parts = path.split(os.sep)
                current = tree
                for part in parts:
                    if part not in current:
                        current[part] = {}
                    current = current[part]
            
            # Display the tree
            def print_tree(node, prefix="", is_last=True):
                items = list(node.items())
                for i, (name, children) in enumerate(items):
                    is_last_item = (i == len(items) - 1)
                    connector = "└── " if is_last_item else "├── "
                    print(f"{prefix}{connector}{name}/")
                    
                    if children:
                        extension = "    " if is_last_item else "│   "
                        print_tree(children, prefix + extension, is_last_item)
            
            print_tree(tree)
        
        print("="*60)
    
    def _ask_confirmation(self) -> bool:
        """Ask user for confirmation to proceed"""
        if NO_CONFIRMATION:
            return True
        
        while True:
            print("\nDisable this prompt by setting NO_CONFIRMATION=1 in your environment variables.")
            response = input("\nDo you want to proceed with syncing these folders? (yes/no): ").strip().lower()
            if response in ['yes', 'y']:
                return True
            elif response in ['no', 'n']:
                return False
            else:
                print("Please answer 'yes' or 'no'")
    
    def download_files(self, verbose: bool = True) -> int:
        """Download source files from Google Drive"""
        if not self.service:
            if not self.authenticate():
                return 0
        
        try:
            from googleapiclient.http import MediaIoBaseDownload
            import io
            
            # Get all folder IDs to search (recursive or single)
            folder_map = {}  # Maps folder_id to folder_path
            if self.folder_id:
                if self.recursive:
                    if verbose:
                        print(f"Searching folder and all subfolders...")
                    folder_map = self._get_all_folders_recursive(self.folder_id)
                    if verbose:
                        print(f"Found {len(folder_map)} folders to search")
                        # Display folder structure
                        self._display_folder_structure(folder_map)
                        # Ask for confirmation
                        if not self._ask_confirmation():
                            print("Sync cancelled by user.")
                            return 0
                else:
                    folder_map = {self.folder_id: ""}
            
            # Query for source files with pagination
            if folder_map:
                # Build query for multiple folders
                folder_ids = list(folder_map.keys())
                folder_queries = [f"'{fid}' in parents" for fid in folder_ids]
                query = f"({' or '.join(folder_queries)}) and mimeType != 'application/vnd.google-apps.folder'"
            else:
                query = "mimeType != 'application/vnd.google-apps.folder'"
            
            # Paginated file listing
            files = []
            page_token = None
            
            while True:
                results = self.service.files().list(
                    q=query,
                    fields="nextPageToken, files(id, name, mimeType, modifiedTime, md5Checksum, parents)",
                    pageSize=1000,
                    pageToken=page_token
                ).execute()
                
                if self.notein:
                    batch_files = [f for f in results.get('files', []) if is_notein_drive_candidate(f)]
                else:
                    # Filter for .nsa files (case-insensitive, exact)
                    batch_files = [f for f in results.get('files', []) if f['name'].lower().endswith('.nsa')]
                files.extend(batch_files)
                
                page_token = results.get('nextPageToken')
                if not page_token:
                    break
            
            downloaded = 0
            skipped = 0
            
            if verbose:
                print(f"Found {len(files)} {self.source_extension} on Google Drive")
            
            for file in files:
                # Determine folder path for this file
                folder_path = ""
                if 'parents' in file and file['parents']:
                    parent_id = file['parents'][0]  # Use first parent
                    # Skip files not in our target folder tree
                    if parent_id not in folder_map:
                        continue
                    folder_path = folder_map.get(parent_id, "")
                
                # Mirror folder structure locally to avoid name collisions
                if folder_path:
                    local_path = self.local_dir / folder_path / file['name']
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                else:
                    local_path = self.local_dir / file['name']
                
                # Store metadata for later conversion (use resolved path as key)
                resolved_path = local_path.resolve()
                self.file_metadata[str(resolved_path)] = {
                    'folder_path': folder_path,
                    'file_id': file['id'],
                    'md5_checksum': file.get('md5Checksum'),
                    'modified_time': file.get('modifiedTime')
                }
                
                # Check if we need to download this file
                state_key = str(resolved_path)
                stored_state = self.state["files"].get(state_key, {})
                stored_checksum = stored_state.get("gdrive_md5")
                current_checksum = file.get('md5Checksum')
                
                # Skip download if file exists locally and hasn't changed on GDrive
                if (local_path.exists() and 
                    current_checksum and 
                    stored_checksum == current_checksum):
                    if verbose:
                        display_path = str(Path(folder_path) / file['name']) if folder_path else file['name']
                        print(f"Skipping {display_path} (unchanged)")
                    skipped += 1
                    
                    # Update state to ensure metadata is current (preserve existing hashes)
                    self.state["files"][state_key] = {
                        **stored_state,
                        "gdrive_md5": current_checksum,
                        "gdrive_modified": file.get('modifiedTime')
                    }
                    continue
                
                # Download file atomically using .part file
                if verbose:
                    display_path = str(Path(folder_path) / file['name']) if folder_path else file['name']
                    print(f"Downloading {display_path}...")
                
                part_path = local_path.with_suffix(local_path.suffix + '.part')
                try:
                    request = self.service.files().get_media(fileId=file['id'])
                    fh = io.FileIO(part_path, 'wb')
                    downloader = MediaIoBaseDownload(fh, request)
                    
                    done = False
                    while not done:
                        status, done = downloader.next_chunk()
                        if verbose and status:
                            print(f"  Progress: {int(status.progress() * 100)}%")
                    
                    fh.close()
                    
                    # Atomic rename
                    part_path.replace(local_path)
                except Exception as e:
                    if part_path.exists():
                        part_path.unlink()
                    raise e
                
                # Update state after download - track download_hash separately
                local_hash = self._file_hash(local_path)
                self.state["files"][state_key] = {
                    **stored_state,
                    "download_hash": local_hash,
                    "gdrive_md5": current_checksum,
                    "gdrive_modified": file.get('modifiedTime'),
                    "last_downloaded": datetime.now().isoformat()
                }
                
                downloaded += 1
            
            # Save state after all downloads
            self._save_state()
            
            if verbose:
                print(f"\nSummary: Downloaded {downloaded}, Skipped {skipped} (unchanged)")
            
            return downloaded
        except Exception as e:
            print(f"Error downloading files: {e}")
            return 0
    
    def sync(self, verbose: bool = True) -> tuple[int, int]:
        """Download and convert files"""
        downloaded = self.download_files(verbose)
        
        # Convert only files that were downloaded from the target folder
        # (those in file_metadata, not all files in local_dir)
        converted = 0
        nsa_files = [Path(p) for p in self.file_metadata.keys()]
        if self.notein:
            nsa_files = [path for path in nsa_files if is_notein_bundle(path)]
        
        if verbose:
            print(f"\nProcessing {len(nsa_files)} {self.source_extension} for conversion")
        
        for nsa_file in nsa_files:
            # Get folder path from metadata using resolved path
            resolved_path = nsa_file.resolve()
            metadata = self.file_metadata.get(str(resolved_path), {})
            folder_path = metadata.get('folder_path', '')
            
            # Construct PDF output path with folder structure
            if folder_path:
                pdf_path = self.output_dir / folder_path / (output_stem_for_source(nsa_file, self.notein) + ".pdf")
            else:
                pdf_path = self.output_dir / (output_stem_for_source(nsa_file, self.notein) + ".pdf")
            
            # Check if conversion is needed
            if self._needs_conversion(nsa_file, pdf_path):
                if self.convert_file(nsa_file, pdf_path, verbose=verbose, metadata=metadata):
                    converted += 1
        
        self.state["last_sync"] = datetime.now().isoformat()
        self._save_state()
        
        return downloaded, converted


def main():
    parser = argparse.ArgumentParser(
        description="Sync Noteshelf .nsa files or Notein bundles from cloud storage and convert to PDF"
    )
    parser.add_argument(
        "--provider",
        choices=["local", "gdrive", "dropbox", "onedrive", "webdav"],
        default="local",
        help="Cloud storage provider"
    )
    parser.add_argument(
        "--local-dir",
        default=None,
        help="Local directory for synced source files (default: ./nsa_files, or ./notein_files with --notein)"
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_PATH,
        help="Output directory for PDFs"
    )
    parser.add_argument(
        "--folder-id",
        help="Google Drive folder ID (optional)"
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Don't search subfolders (only search specified folder)"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch mode - continuously sync at interval"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Sync interval in seconds (default: 300 = 5 minutes)"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress output"
    )
    parser.add_argument(
        "--notein",
        action="store_true",
        help="Sync and convert Notein export bundles instead of Noteshelf .nsa files"
    )
    
    args = parser.parse_args()
    if args.local_dir is None:
        args.local_dir = "./notein_files" if args.notein else "./nsa_files"
    verbose = not args.quiet
    
    # Validate provider
    if args.provider in {"dropbox", "onedrive", "webdav"}:
        print(f"Error: Provider '{args.provider}' is not implemented yet.")
        print("Currently supported: local, gdrive")
        sys.exit(1)
    
    # Initialize sync manager based on provider
    if args.provider == "gdrive":
        recursive = not args.no_recursive
        manager = GoogleDriveSync(args.local_dir, args.output_dir, args.folder_id, recursive, notein=args.notein)
    else:
        manager = CloudSyncManager(args.local_dir, args.output_dir, notein=args.notein)
    
    def sync_once():
        """Perform one sync operation"""
        if verbose:
            print(f"\n{'='*60}")
            print(f"Sync started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"{'='*60}")
        
        if args.provider == "gdrive":
            downloaded, converted = manager.sync(verbose)
            if verbose:
                print(f"\nDownloaded: {downloaded} files")
                print(f"Converted: {converted} files")
        else:
            # Local mode - just convert existing files
            converted = manager.sync_local_files(verbose)
            if verbose:
                print(f"\nConverted: {converted} files")
        
        if verbose:
            print(f"Output directory: {manager.output_dir}")
    
    # Run sync
    if args.watch:
        if verbose:
            print(f"Watch mode enabled - syncing every {args.interval} seconds")
            print("Press Ctrl+C to stop")
        
        try:
            while True:
                sync_once()
                if verbose:
                    print(f"\nWaiting {args.interval} seconds until next sync...")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            if verbose:
                print("\nSync stopped by user")
    else:
        sync_once()


if __name__ == "__main__":
    main()
