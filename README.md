# Noteshelf to PDF Converter 📝➡️📄

## What is This?

This tool converts Noteshelf (Android) notes to PDF format. It extracts your handwritten notes from `.nsa` files and creates clean PDF documents. You can also sync files directly from Google Drive.

New desktop app ready to use!

## Why?

Noteshelf on Android itself supports sync and backup to multiple platforms. However, it doesn't offer any way of opening these synced files, the only usage is meant for backup and uploading it back to Noteshelf.

The only other possible way of getting all your notes onto PC is exporting them one by one everytime you modify any of your notes, and I didn't take that as a possibility.

## Quick Start

### Prerequisites

- Python 3.12 or higher
- uv / pip (Python package manager)


### Installation

This project uses [uv](https://github.com/astral-sh/uv) for fast Python package management.

**Install uv** (if you don't have it):
```bash
# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Install dependencies:**
```bash
uv sync
```

### Usage (Gdrive sync recommended)

#### Local usage

Setup Google drive, then start the desktop version
```bash
uv run desktop.py
```

Next, follow the setup - choose the sync type, folder and sync everything!

## Google Drive Sync

Automatically download and convert synced notes from Google Drive.

### Setup (One Time)

1. **Create Google Cloud Project**:
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a new project
   - Enable the Google Drive API

2. **Create OAuth Credentials**:
   - Navigate to "APIs & Services" → "Credentials"
   - Click "Create Credentials" → "OAuth 2.0 Client ID"
   - Select "Desktop app" as application type
   - Download the credentials file

3. **Configure the Tool**:
   - Save the downloaded file as `credentials.json` in the project directory
   - Go to "OAuth consent screen" → "Test users"
   - Add your Google account as a test user

## CLI Usage

- RECOMMENDED: Specify `DEFAULT_PATH` in `.env` or add `--output-dir ".\FOLDER"` parameter. Check `.env.example` for more information.

```bash
uv run sync_and_convert.py --provider gdrive --folder-id "YOUR_FOLDER_ID"
```

**How to find your folder ID:**
Open your folder in Google Drive. The URL looks like:
```
https://drive.google.com/drive/folders/1a2b3c4d5e...
                                        ↑ This is your folder ID
```

The folder ID should be of the one at the top of the structure, I have this structure

My Disk/Noteshelf 3 Android/My/personal/names/of/folders

and the folder ID is of `Noteshelf 3 Android`, but of course you can set any folder you want.

The first time you run this, a browser window opens for authorization. After that, it remembers your login.

### What Happens?

1. All `.nsa` files download to local folder `nsa_files/` in the same structure from your GDrive
2. Files convert to PDFs in your specified folder from `DEFAULT_PATH` or in default `pdf_output/`, or other specified folder using `--output-dir`
3. Based on hashes, only modified (or missing) files are downloaded
4. Only changed files are re-converted (based on file hashes from GDrive and local changes)

### Local Mode

Already have `.nsa` files on your computer?

```bash
uv run sync_and_convert.py --provider local --local-dir "C:\Path\To\Notes"
```

## Options

You can customize conversion settings:

```bash
uv run nsa_convertor.py notes.nsa -o notes.pdf \
  --highlighter-opacity 0.3 \
  --highlighter-ratio 6.0 \
  --epsilon 1.0
```

- `--highlighter-opacity`: How transparent highlighters appear (0-1, default: 0.38)
- `--highlighter-ratio`: How much wider highlighters are vs pens (default: 5.0)
- `--epsilon`: Smoothing level for strokes (default: 0.8)
- `--no-smooth`: Turn off smoothing
- `--quiet`: Less output
- `--notein`: support for notein

For Notein backups, PDFs are placed in the folder structure described by the
backup metadata. Notes in Notein's trash are written under `_Trash/`.

## How It Works

Noteshelf `.nsa` files are ZIP archives containing:
- **Document.plist**: Page info and metadata
- **Templates/**: Background PDFs
- **Annotations/**: SQLite databases with your strokes and drawings

The converter:
1. Extracts the template PDFs
2. Reads your handwriting data from SQLite
3. Intelligently detects highlighters vs pens
4. Draws everything on the template
5. Outputs a final PDF

## Troubleshooting

**"No credentials.json found"**: Download OAuth credentials from Google Cloud Console

**"Permission denied"**: Make sure you added yourself as a test user

**Weird colors**: Try adjusting `--highlighter-opacity` and `--highlighter-ratio`

## About

Created because no existing solution could convert Noteshelf files properly.