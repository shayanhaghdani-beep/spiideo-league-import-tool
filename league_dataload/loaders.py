"""Loaders for the manual curation tables (ported from the engine's load_deals).

Both files are header-driven CSVs; blank rows and rows whose first cell starts
with '#' are skipped so the files can carry comments. A missing file → empty
list (both features are opt-in).
"""
from __future__ import annotations

import csv
from pathlib import Path

from .schema import DealAlias, ManualAccountMatch


def load_manual_account_ids(path: Path) -> list[ManualAccountMatch]:
    """Read the manual league→existing-CRM-account override table.

    CSV columns (only ``league`` + ``sf_account_id`` required):
        league, sf_account_id, hs_record_id, note
    """
    if not path.exists():
        return []
    out: list[ManualAccountMatch] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            league = (row.get("league") or "").strip()
            if not league or league.startswith("#"):
                continue
            out.append(ManualAccountMatch(
                league=league,
                sf_account_id=(row.get("sf_account_id") or "").strip(),
                hs_record_id=(row.get("hs_record_id") or "").strip(),
                note=(row.get("note") or "").strip(),
            ))
    return out


def load_deal_aliases(path: Path) -> list[DealAlias]:
    """Read the manual league→partner-account alias table.

    CSV columns (all optional except ``league``):
        league, company_name, company_record_id, reason
    """
    if not path.exists():
        return []
    out: list[DealAlias] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            league = (row.get("league") or "").strip()
            if not league or league.startswith("#"):
                continue
            out.append(DealAlias(
                league=league,
                company_name=(row.get("company_name") or "").strip(),
                company_record_id=(row.get("company_record_id") or "").strip(),
                reason=(row.get("reason") or "").strip(),
            ))
    return out
