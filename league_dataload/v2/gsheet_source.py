"""Google Sheets source for the importer — READ, plus writes to a tiny ALLOWLIST of columns.

⛔ RULE (Shayan, 2026-06-24): the tool must not edit a Google Sheet, except it may write to these
columns and NOTHING else:
    * "SF OPP LINK" — paste an opportunity's Salesforce link.
    * "Tax ID"      — backfill a club's Swedish org-nr, ONLY when the cell is blank.

Enforcement:
  * READING uses read-only scopes only (`spreadsheets.readonly` + `drive.readonly`).
  * The ONLY writer is `write_column`, which refuses any column not in `WRITABLE_COLUMNS`; the
    target cells come from the pure `_plan_cells` (which also enforces the allowlist) and each is
    written with a single `update_cell` — never a range, never another column. Do NOT add a
    column to `WRITABLE_COLUMNS` without explicit sign-off, and do NOT add any other write path.

Needs `gspread` + `google-auth`, and a service-account key with access to the sheet (Viewer to
read, Editor to write). Key PATH from the caller / `$GOOGLE_SHEETS_CREDENTIALS` — never hardcoded.
"""
from __future__ import annotations

import re

from .load_mcs import HEADER_MARKER, ClubRecord, parse_rows

# READ scopes: read-only only. Never add a writable scope here.
READONLY_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)
# WRITE scope: used ONLY by write_column, which only ever touches an allowlisted column.
WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

OPP_LINK_COLUMN = "SF OPP LINK"
TAX_ID_COLUMN = "Tax ID"
# The ONLY sheet columns the tool may ever write. Nothing else, ever.
WRITABLE_COLUMNS = (OPP_LINK_COLUMN, TAX_ID_COLUMN)


def sheet_id(url_or_id: str) -> str:
    """Accept a full Google Sheets URL or a bare spreadsheet id; return the id."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id or "")
    return m.group(1) if m else (url_or_id or "").strip()


def opp_link_url(opp_id: str) -> str:
    """The Salesforce Lightning opp URL pasted into SF OPP LINK (matches the sheet's format)."""
    return (f"https://spiideo.lightning.force.com/lightning/r/Opportunity/"
            f"{(opp_id or '').strip()}/view")


def _authorize(creds_path: str, scopes):
    from google.oauth2.service_account import Credentials
    import gspread
    creds = Credentials.from_service_account_file(creds_path, scopes=list(scopes))
    return gspread.authorize(creds)


def _worksheet(sh, tab: str | None):
    if tab:
        return sh.worksheet(tab)
    for w in sh.worksheets():
        vals = w.get_all_values()
        if any(HEADER_MARKER in (c or "") for row in vals for c in row):
            return w
    raise ValueError(f"no worksheet containing a {HEADER_MARKER!r} header in {sh.title!r}")


# --- READ (read-only) ------------------------------------------------------

def read_rows(url_or_id: str, creds_path: str, tab: str | None = None) -> list[list[str]]:
    """Raw cell rows of ONE tab (READ-ONLY)."""
    sh = _authorize(creds_path, READONLY_SCOPES).open_by_key(sheet_id(url_or_id))
    return _worksheet(sh, tab).get_all_values()


def load_mcs_gsheet(url_or_id: str, creds_path: str, tab: str | None = None) -> list[ClubRecord]:
    """Read a filled Main Camera Sheet straight from a Google Sheet (READ-ONLY) into ClubRecords."""
    return parse_rows(read_rows(url_or_id, creds_path, tab))


# --- WRITE (allowlisted columns only) --------------------------------------

def _plan_cells(rows: list[list[str]], column: str, values_by_team: dict,
                only_if_blank: bool = True) -> list:
    """PURE: from raw sheet rows, return [(row_1based, col_1based, current, value)] to write into
    `column` ONLY. Refuses any column not in WRITABLE_COLUMNS. `col_1based` is ALWAYS `column`'s
    index — this can never target another column. `values_by_team`: stripped Team Name -> value."""
    if column not in WRITABLE_COLUMNS:
        raise ValueError(f"{column!r} is not writable; allowed columns: {WRITABLE_COLUMNS}")
    hi = next(i for i, r in enumerate(rows)
              if any((c or "").strip() == HEADER_MARKER for c in r))
    hdr = [(c or "").strip() for c in rows[hi]]
    if column not in hdr:
        raise ValueError(f"no {column!r} column in the sheet")
    wcol, tcol = hdr.index(column), hdr.index(HEADER_MARKER)
    plan = []
    for i in range(hi + 1, len(rows)):
        r = rows[i]
        team = (r[tcol] if tcol < len(r) else "").strip()
        if not team:
            continue                        # never match blank template rows (guards an "" key)
        val = values_by_team.get(team)
        if not val:
            continue
        cur = (r[wcol] if wcol < len(r) else "").strip()
        if only_if_blank and cur:
            continue
        plan.append((i + 1, wcol + 1, cur, val))       # col is ALWAYS the allowlisted column
    return plan


def preview_writes(url_or_id: str, creds_path: str, tab: str | None, column: str,
                   values_by_team: dict, only_if_blank: bool = True) -> list:
    """READ-ONLY: what write_column WOULD write, without writing. [(row, col, current, value)]."""
    return _plan_cells(read_rows(url_or_id, creds_path, tab), column, values_by_team, only_if_blank)


def write_column(url_or_id: str, creds_path: str, tab: str, column: str, values_by_team: dict,
                 only_if_blank: bool = True) -> list:
    """Write `values_by_team` into ONE allowlisted `column` (SF OPP LINK or Tax ID) — never any
    other column/cell. One `update_cell` per matched row. Returns [(row, value)] written."""
    if column not in WRITABLE_COLUMNS:
        raise ValueError(f"{column!r} is not writable; allowed columns: {WRITABLE_COLUMNS}")
    rows = read_rows(url_or_id, creds_path, tab)                # read-only read to plan the writes
    plan = _plan_cells(rows, column, values_by_team, only_if_blank)
    hi = next(i for i, r in enumerate(rows) if any((c or "").strip() == HEADER_MARKER for c in r))
    col_1based = [(c or "").strip() for c in rows[hi]].index(column) + 1
    assert all(col == col_1based for _, col, _, _ in plan), \
        f"refusing to write outside the {column!r} column"
    ws = _worksheet(_authorize(creds_path, (WRITE_SCOPE,)).open_by_key(sheet_id(url_or_id)), tab)
    written = []
    for row, col, _cur, val in plan:
        ws.update_cell(row, col, val)          # writes EXACTLY one cell, in the allowlisted column
        written.append((row, val))
    return written


def write_opp_links(url_or_id, creds_path, tab, links_by_team, only_if_blank=True) -> list:
    """Convenience wrapper: write opp SF links into the SF OPP LINK column only."""
    return write_column(url_or_id, creds_path, tab, OPP_LINK_COLUMN, links_by_team, only_if_blank)
