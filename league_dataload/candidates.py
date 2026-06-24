"""Build the unified league-candidate pool: HubSpot ∪ Salesforce, deduped.

Combines two sources so a forecast league counts as "existing" if it's found in
EITHER system — which is what prevents creating duplicate accounts:

  1. HubSpot curated export (data/hubspot_leagues.csv) — PRIMARY. Carries both
     the Salesforce Account ID and the HubSpot Record ID. Rows with an EMPTY SF
     Account ID are kept on purpose, so a league that exists in HubSpot but not
     in Salesforce still matches (flagged) rather than re-created.
  2. The LookupSource Accounts (CSV export or live `sf` query) — SECONDARY.
     Fills in accounts that exist in Salesforce but aren't in the curated
     HubSpot list (e.g. collegiate conferences).

``merge_candidates`` dedupes by Salesforce Account ID (HubSpot wins on
collision), then HS Record IDs are back-filled on SF-only candidates from the
broader company export (data/hubspot_company_ids.csv).
"""
from __future__ import annotations

import os

from . import config
from .load_hubspot import (
    attach_record_ids,
    load_hubspot_leagues,
    load_hubspot_record_ids,
    merge_candidates,
)
from .schema import SFLeagueCandidate
from .sources import LookupSource


# Words that, when present in Org_Type__c, mark an account as a league.
DEFAULT_LEAGUE_ORG_TYPES = ["league", "conference", "federation", "association"]


def fetch_existing_leagues(source: LookupSource) -> list[SFLeagueCandidate]:
    """Filter the LookupSource Accounts to those that look like leagues."""
    org_types_env = os.environ.get("LOOKUP_LEAGUE_ORG_TYPES", "").strip()
    allowed = (
        [t.strip().lower() for t in org_types_env.split(",") if t.strip()]
        if org_types_env
        else DEFAULT_LEAGUE_ORG_TYPES
    )

    out: list[SFLeagueCandidate] = []
    for row in source.fetch_accounts():
        org_type = (row.get("Org_Type__c") or "").strip()
        name = (row.get("Name") or "").strip()
        if not name:
            continue
        if org_type:
            if any(t in org_type.lower() for t in allowed):
                out.append(_to_candidate(row))
            continue
        # Fallback: name-based heuristic (mirror the Org_Type keyword set plus a
        # couple of name-only cues) for accounts with an empty Org_Type.
        n = name.lower()
        name_kws = set(allowed) | {"liga", "serie"}
        if any(kw in n for kw in name_kws):
            out.append(_to_candidate(row))
    return out


def _to_candidate(row: dict) -> SFLeagueCandidate:
    return SFLeagueCandidate(
        sf_id=(row.get("Id") or "").strip(),
        name=(row.get("Name") or "").strip(),
        org_type=(row.get("Org_Type__c") or "").strip(),
        sport=(row.get("Sport__c") or "").strip(),
        country=(row.get("BillingCountry") or "").strip(),
        source="salesforce",
    )


def gather_candidates(source: LookupSource) -> tuple[list[SFLeagueCandidate], tuple[int, int, int]]:
    """Return (candidates, (hs_count, sf_count, merged_count))."""
    hubspot_path = config.hubspot_leagues_csv()
    hs_candidates = load_hubspot_leagues(hubspot_path) if hubspot_path.exists() else []
    sf_candidates = fetch_existing_leagues(source)
    merged = merge_candidates(hs_candidates, sf_candidates)
    # Back-fill HS Record IDs on SF-only candidates from the broader company export.
    name_to_record_id = load_hubspot_record_ids(config.hubspot_company_ids_csv())
    attach_record_ids(merged, name_to_record_id)
    return merged, (len(hs_candidates), len(sf_candidates), len(merged))
