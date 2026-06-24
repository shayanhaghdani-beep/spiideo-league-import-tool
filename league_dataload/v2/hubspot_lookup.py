"""HubSpot company lookup — a second candidate pool for the crosscheck.

Clubs frequently exist in HubSpot *before* they've synced to Salesforce. The
SF-only matcher would call those "new" and try to create a duplicate SF account
(which trips SF's duplicate rules). This loads a HubSpot companies export so the
crosscheck can flag clubs that already exist in HubSpot and SKIP creating them —
the operator syncs HubSpot→SF first, then re-runs.

CSV columns: Name, Domain, HubSpot Record ID.
Refresh: re-export companies from HubSpot (or regenerate via the HubSpot MCP).
See RUNBOOK. Stdlib only. Rows are returned in the clubmatch matcher's crm-row
shape (Name / Id / domain) so the SAME matcher used for Salesforce applies here.
"""
from __future__ import annotations

import csv
from pathlib import Path


def load_hubspot_companies(path: str | Path) -> list[dict]:
    """Return [{Name, Id, domain}] for the clubmatch matcher (crm-row shape)."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            out.append({
                "Name": name,
                "Id": (row.get("HubSpot Record ID") or "").strip(),
                "domain": (row.get("Domain") or "").strip().lower(),
            })
    return out
