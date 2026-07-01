"""Google Sheets source for the importer — READ, plus ONE narrowly-permitted write.

⛔ RULE (Shayan, 2026-06-24): the tool must not edit a Google Sheet, with EXACTLY ONE exception —
it may paste an opportunity's Salesforce link into the **"SF OPP LINK"** column. Nothing else in
the sheet may ever be written.

Enforcement:
  * READING uses read-only scopes only (`spreadsheets.readonly` + `drive.readonly`) — see
    `read_rows` / `load_mcs_gsheet`.
  * The ONE write (`write_opp_links`) targets ONLY the "SF OPP LINK" column: the target cells are
    computed by the pure `_plan_link_cells` (which can only ever return that column), and each is
    written with a single-cell `update_cell` — never a range, never another column. Do NOT add any
    other write, and do NOT widen what `write_opp_links` may touch.

Needs `gspread` + `google-auth`, and a service-account JSON key whose client_email has access to
the sheet (Viewer is enough to read; Editor is needed for the opp-link write). Key PATH comes from
the caller / `$GOOGLE_SHEETS_CREDENTIALS` — never hardcoded, never committed.
"""
from __future__ import annotations

import re

from .load_mcs import HEADER_MARKER, ClubRecord, parse_rows

# READ scopes: read-only only. Never add a writable scope here.
READONLY_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)
# WRITE scope: used ONLY by write_opp_links, which only ever touches the SF OPP LINK column.
WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

# The ONE column the tool is allowed to write.
OPP_LINK_COLUMN = "SF OPP LINK"


def sheet_id(url_or_id: str) -> str:
    """Accept a full Google Sheets URL or a bare spreadsheet id; return the id."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id or "")
    return m.group(1) if m else (url_or_id or "").strip()


def opp_link_url(opp_id: str) -> str:
    """The Salesforce Lightning opp URL we paste into the SF OPP LINK column (matches the
    format already used in the sheet)."""
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


# --- WRITE (the ONE permitted edit: SF OPP LINK column only) ----------------

def _plan_link_cells(rows: list[list[str]], links_by_team: dict, only_if_blank: bool = True) -> list:
    """PURE: from raw sheet rows, return [(row_1based, col_1based, current, url)] to write into
    the SF OPP LINK column ONLY. `col_1based` is ALWAYS the SF OPP LINK column — this function
    can never target any other column. `links_by_team`: stripped Team Name -> opp URL."""
    hi = next(i for i, r in enumerate(rows)
              if any((c or "").strip() == HEADER_MARKER for c in r))
    hdr = [(c or "").strip() for c in rows[hi]]
    if OPP_LINK_COLUMN not in hdr:
        raise ValueError(f"no {OPP_LINK_COLUMN!r} column in the sheet")
    lcol, tcol = hdr.index(OPP_LINK_COLUMN), hdr.index(HEADER_MARKER)
    plan = []
    for i in range(hi + 1, len(rows)):
        r = rows[i]
        team = (r[tcol] if tcol < len(r) else "").strip()
        url = links_by_team.get(team)
        if not url:
            continue
        cur = (r[lcol] if lcol < len(r) else "").strip()
        if only_if_blank and cur:
            continue
        plan.append((i + 1, lcol + 1, cur, url))   # col is ALWAYS the SF OPP LINK column
    return plan


def preview_opp_link_writes(url_or_id: str, creds_path: str, tab: str | None,
                            links_by_team: dict, only_if_blank: bool = True) -> list:
    """READ-ONLY: what write_opp_links WOULD write, without writing. [(row, col, current, url)]."""
    return _plan_link_cells(read_rows(url_or_id, creds_path, tab), links_by_team, only_if_blank)


def write_opp_links(url_or_id: str, creds_path: str, tab: str, links_by_team: dict,
                    only_if_blank: bool = True) -> list:
    """THE ONE PERMITTED WRITE (Shayan, 2026-06-24): paste opp Salesforce links into the
    'SF OPP LINK' column ONLY. Writes one cell per matched row via update_cell; never touches any
    other column/cell. Returns [(row, url)] actually written."""
    rows = read_rows(url_or_id, creds_path, tab)               # read-only read to plan the writes
    plan = _plan_link_cells(rows, links_by_team, only_if_blank)
    hdr = [(c or "").strip() for c in rows[next(
        i for i, r in enumerate(rows) if any((c or "").strip() == HEADER_MARKER for c in r))]]
    link_col_1based = hdr.index(OPP_LINK_COLUMN) + 1
    # Defense-in-depth: refuse to proceed if any planned cell is outside the SF OPP LINK column.
    assert all(col == link_col_1based for _, col, _, _ in plan), \
        "refusing to write outside the SF OPP LINK column"
    ws = _worksheet(_authorize(creds_path, (WRITE_SCOPE,)).open_by_key(sheet_id(url_or_id)), tab)
    written = []
    for row, col, _cur, url in plan:
        ws.update_cell(row, col, url)      # writes EXACTLY one cell, in the SF OPP LINK column
        written.append((row, url))
    return written
