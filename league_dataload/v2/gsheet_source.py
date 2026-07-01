"""READ-ONLY Google Sheets source for the importer.

⛔ HARD RULE (Shayan, 2026-06-24): this tool must NEVER, under any circumstance, write to /
edit a Google Sheet. That is enforced STRUCTURALLY here, not just by convention:

  * we authenticate with READ-ONLY OAuth scopes ONLY (spreadsheets.readonly + drive.readonly),
    so the access token is physically incapable of mutating a sheet; and
  * we only ever call read methods (`get_all_values`). Do NOT add any write/update/append/
    batch_update/insert call, and do NOT add a writable scope to READONLY_SCOPES. Ever.

Needs `gspread` + `google-auth`, and a service-account JSON key whose client_email has been
granted (Viewer) access to the sheet. The key PATH is supplied by the caller / the
`$GOOGLE_SHEETS_CREDENTIALS` env var -- never hardcoded, never committed.
"""
from __future__ import annotations

import re

from .load_mcs import HEADER_MARKER, ClubRecord, parse_rows

# READ-ONLY scopes only. Adding a writable scope here would violate the hard rule above.
READONLY_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)


def sheet_id(url_or_id: str) -> str:
    """Accept a full Google Sheets URL or a bare spreadsheet id; return the id."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id or "")
    return m.group(1) if m else (url_or_id or "").strip()


def _open(url_or_id: str, creds_path: str):
    # Imported lazily so the rest of the tool stays stdlib-only (CSV/xlsx path needs no google).
    from google.oauth2.service_account import Credentials
    import gspread
    creds = Credentials.from_service_account_file(creds_path, scopes=list(READONLY_SCOPES))
    return gspread.authorize(creds).open_by_key(sheet_id(url_or_id))


def read_rows(url_or_id: str, creds_path: str, tab: str | None = None) -> list[list[str]]:
    """Raw cell rows of ONE tab (READ-ONLY). If `tab` is None, auto-pick the worksheet whose
    cells contain the 'Team Name' header marker."""
    sh = _open(url_or_id, creds_path)
    if tab:
        return sh.worksheet(tab).get_all_values()
    for w in sh.worksheets():
        vals = w.get_all_values()
        if any(HEADER_MARKER in (c or "") for row in vals for c in row):
            return vals
    raise ValueError(f"no worksheet containing a {HEADER_MARKER!r} header in {sh.title!r}")


def load_mcs_gsheet(url_or_id: str, creds_path: str, tab: str | None = None) -> list[ClubRecord]:
    """Read a filled Main Camera Sheet straight from a Google Sheet (READ-ONLY) into
    ClubRecords -- same output as load_mcs() for a CSV/xlsx."""
    return parse_rows(read_rows(url_or_id, creds_path, tab))
