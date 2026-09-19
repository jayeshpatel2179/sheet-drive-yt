#!/usr/bin/env python3
"""
Google Sheet -> YouTube download (best quality) -> Google Drive.

Sheet layout (row 1 = header):
    A: Title           (yours, ignored)
    B: Link            (YouTube URL, you fill this)
    C: Status          (script fills: DONE / FAILED: reason / OLD-SKIPPED)

A row is processed only if column B is empty AND its video ID isn't already
in processed.json, so old links and duplicates are never fetched twice.

Usage:
    python sheet_to_drive.py --mark-existing   # once: mark current rows as old
    python sheet_to_drive.py                   # process new rows once (for Task Scheduler)
    python sheet_to_drive.py --watch 5         # keep polling every 5 minutes
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yt_dlp
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import gspread

# ---- CONFIG: edit these -----------------------------------------------------
SPREADSHEET_ID = "1ashg8m0YFCvI1e9QtkBg2cDBP7HWkaoBmGTn1TekWV4"   # the long id in the sheet URL
WORKSHEET_NAME = "Sheet1"
DRIVE_FOLDER_ID = "1N_r9hKIXVMKG9DoePoRnn-VFFmOx2CLn"  # all videos go in this one folder
# -----------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
BASE = Path(__file__).parent
CLIENT_SECRET = BASE / "credentials.json"   # OAuth desktop client from Google Cloud
TOKEN_FILE = BASE / "token.json"            # created on first login
STATE_FILE = BASE / "processed.json"        # {video_id: drive_link}

# Sheet columns (1-based): A Title | B Link | C Status
URL_COL, STATUS_COL = 2, 3
URL_IDX, STATUS_IDX = URL_COL - 1, STATUS_COL - 1


def col_letter(n: int) -> str:
    return chr(ord("A") + n - 1)

VIDEO_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})"
)


def get_creds() -> Credentials:
    creds = None
    # Hosted mode (Railway): token comes from an env var, no browser login.
    token_env = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_env:
        info = json.loads(token_env)
        if not info.get("refresh_token"):
            sys.exit("GOOGLE_TOKEN_JSON has no refresh_token; regenerate token.json.")
        creds = Credentials.from_authorized_user_info(info, SCOPES)
        if not creds.valid:
            creds.refresh(Request())
        return creds
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(
                port=8080, access_type="offline", prompt="consent"  # force a refresh_token
            )  # must match the redirect URI in Google Cloud
        TOKEN_FILE.write_text(creds.to_json())
    return creds


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def video_id(url: str):
    m = VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def download_best(url: str, out_dir: Path) -> Path:
    opts = {
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",   # needs ffmpeg
        "outtmpl": str(out_dir / "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}  # lets yt-dlp solve YouTube JS challenges
    cookies = os.environ.get("YT_COOKIES")  # optional: contents of a cookies.txt
    if cookies:
        cookie_file = out_dir / "cookies.txt"
        cookie_file.write_text(cookies)
        opts["cookiefile"] = str(cookie_file)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info)).with_suffix(".mp4")


def upload_to_drive(drive, path: Path) -> str:
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True)
    meta = {"name": path.name, "parents": [DRIVE_FOLDER_ID]}
    req = drive.files().create(body=meta, media_body=media, fields="id,webViewLink")
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["webViewLink"]


def mark_existing(ws) -> None:
    rows = ws.get_all_values()[1:]
    updates = []
    for i, row in enumerate(rows, start=2):
        url = row[URL_IDX].strip() if len(row) > URL_IDX else ""
        status = row[STATUS_IDX].strip() if len(row) > STATUS_IDX else ""
        if url and not status:
            updates.append({"range": f"{col_letter(STATUS_COL)}{i}", "values": [["OLD-SKIPPED"]]})
    if updates:
        ws.batch_update(updates)
    print(f"Marked {len(updates)} existing row(s) as OLD-SKIPPED.")


def process_once(ws, drive) -> None:
    state = load_state()
    rows = ws.get_all_values()[1:]
    for i, row in enumerate(rows, start=2):
        url = row[URL_IDX].strip() if len(row) > URL_IDX else ""
        status = row[STATUS_IDX].strip() if len(row) > STATUS_IDX else ""
        if not url or status:
            continue

        vid = video_id(url)
        if not vid:
            ws.update_cell(i, STATUS_COL, "FAILED: not a YouTube URL")
            continue
        if vid in state:
            ws.update_cell(i, STATUS_COL, "DUPLICATE")
            continue

        ws.update_cell(i, STATUS_COL, "PROCESSING")
        tmp = Path(tempfile.mkdtemp(prefix="ytdl_"))
        try:
            print(f"Row {i}: downloading {url}")
            file_path = download_best(url, tmp)
            print(f"Row {i}: uploading {file_path.name}")
            link = upload_to_drive(drive, file_path)
            state[vid] = link
            save_state(state)
            ws.update_cell(i, STATUS_COL, "DONE")
        except Exception as e:  # noqa: BLE001 - report any failure to the sheet
            print(f"Row {i}: failed: {e}")
            ws.update_cell(i, STATUS_COL, f"FAILED: {str(e)[:120]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--watch", type=float, metavar="MIN",
                   help="poll the sheet every MIN minutes instead of running once")
    p.add_argument("--mark-existing", action="store_true",
                   help="mark all current unprocessed rows as OLD-SKIPPED and exit")
    args = p.parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found on PATH (needed to merge best video+audio).")

    creds = get_creds()
    ws = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
    drive = build("drive", "v3", credentials=creds)

    if args.mark_existing:
        mark_existing(ws)
        return

    if args.watch:
        print(f"Watching sheet every {args.watch} min. Ctrl+C to stop.")
        while True:
            try:
                process_once(ws, drive)
            except Exception as e:  # noqa: BLE001 - keep the watcher alive
                print(f"Poll error: {e}")
            time.sleep(args.watch * 60)
    else:
        process_once(ws, drive)


if __name__ == "__main__":
    main()
