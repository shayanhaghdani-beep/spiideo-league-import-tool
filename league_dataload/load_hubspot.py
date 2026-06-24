"""Load HubSpot's curated 'Leagues and National Federations' export → SFLeagueCandidate list.

HubSpot keeps a hand-curated list of every account tagged as a League or
Federation, each linked back to the Salesforce Account ID. That's a much
more reliable crosscheck source than searching the full 13k-row Accounts tab
with name-substring heuristics — when a forecast league matches a HubSpot
name, we can trust the linked SF Account ID directly.

Expected columns (from the standard HubSpot CRM export):
    Record ID, Company/Customer name, Company/Customer Domain Name,
    League or Conference, Country, State/Region,
    Number of associated account (active & cancelled),
    Active ARR (new), Salesforce Account ID

We only need ``Company/Customer name`` (for matching) and
``Salesforce Account ID`` (for the resolved SF Id). Country / Domain are
preserved as supplementary metadata.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .schema import SFLeagueCandidate


def load_hubspot_leagues(path: Path) -> list[SFLeagueCandidate]:
    """Read the HubSpot export CSV → SFLeagueCandidate list."""
    if not path.exists():
        return []

    out: list[SFLeagueCandidate] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Company/Customer name") or "").strip()
            sf_id = (row.get("Salesforce Account ID") or "").strip()
            hs_record_id = (row.get("Record ID") or "").strip()
            if not name:
                continue
            # If there's no linked SF Account ID, we still keep the candidate
            # so the matcher can flag "exists in HubSpot but missing in SF".
            out.append(SFLeagueCandidate(
                sf_id=sf_id,
                name=name,
                org_type="League/Federation (HubSpot)",
                sport="",
                country=(row.get("Country") or "").strip(),
                source="hubspot",
                hs_record_id=hs_record_id,
                associated_deals=_to_int(row.get("Number of Associated Deals")),
                associated_accounts=_to_int(
                    row.get("Number of associated account (active & cancelled)")
                ),
                active_arr=(row.get("Active ARR (new)") or "").strip(),
            ))
    return out


def load_hubspot_record_ids(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Build name → HubSpot Record ID lookups from the broader company export.

    A second, broader HubSpot export (``hubspot_company_ids.csv``) carries every
    company's Record ID + name but NOT the Salesforce Account ID. We use it only
    to back-fill the HS Record ID on candidates that came from the Salesforce
    Accounts tab (e.g. collegiate conferences HubSpot tracks but that aren't in
    the curated league-linkage export).

    Returns ``(by_raw, by_norm)``:
      * ``by_raw`` keys on the exact (case-folded) name, so siblings that differ
        only by parenthetical acronym — "Southern Conference (SoCon)" vs
        "Southern Conference (NJCAA)" — each resolve uniquely.
      * ``by_norm`` keys on the normalised name as a looser fallback.
    In both maps, a key mapping to >1 distinct Record ID is dropped — we never
    guess when ambiguous.
    """
    from .normalize import normalise_league  # local import to avoid cycles

    if not path.exists():
        return {}, {}

    raw: dict[str, set[str]] = {}
    norm: dict[str, set[str]] = {}
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Company/Customer name") or "").strip()
            rid = (row.get("Record ID") or "").strip()
            if not name or not rid:
                continue
            # Skip explicitly deprecated rows ("... do not use").
            if "do not use" in name.lower():
                continue
            raw.setdefault(name.casefold(), set()).add(rid)
            norm.setdefault(normalise_league(name), set()).add(rid)

    by_raw = {k: next(iter(v)) for k, v in raw.items() if len(v) == 1}
    by_norm = {k: next(iter(v)) for k, v in norm.items() if len(v) == 1}
    return by_raw, by_norm


def attach_record_ids(
    candidates: list[SFLeagueCandidate],
    lookups: tuple[dict[str, str], dict[str, str]],
) -> int:
    """Back-fill ``hs_record_id`` on candidates that lack one, matched by name.

    Tries the exact (case-folded) name first, then the normalised name. Mutates
    candidates in place. Returns the number of candidates enriched.
    """
    from .normalize import normalise_league  # local import to avoid cycles

    by_raw, by_norm = lookups
    if not by_raw and not by_norm:
        return 0
    n = 0
    for c in candidates:
        if c.hs_record_id or not c.name:
            continue
        rid = by_raw.get(c.name.casefold()) or by_norm.get(normalise_league(c.name))
        if rid:
            c.hs_record_id = rid
            n += 1
    return n


def _to_int(value: object) -> int:
    """Parse '12.0' / '12' / '' → int (0 on failure)."""
    s = str(value or "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def merge_candidates(
    primary: list[SFLeagueCandidate],
    secondary: list[SFLeagueCandidate],
) -> list[SFLeagueCandidate]:
    """Merge two candidate lists, with `primary` winning on Salesforce ID collision.

    Used to combine HubSpot candidates (more curated, primary) with the
    name-heuristic candidates from the All accounts tab (broader coverage,
    secondary). Returns a list with unique sf_id entries; entries with empty
    sf_id are kept distinct (by name).
    """
    seen_ids: set[str] = set()
    seen_unmapped_names: set[str] = set()
    out: list[SFLeagueCandidate] = []

    for c in primary:
        if c.sf_id:
            if c.sf_id in seen_ids:
                continue
            seen_ids.add(c.sf_id)
        else:
            key = c.name.strip().lower()
            if key in seen_unmapped_names:
                continue
            seen_unmapped_names.add(key)
        out.append(c)

    for c in secondary:
        if c.sf_id and c.sf_id in seen_ids:
            continue
        if c.sf_id:
            seen_ids.add(c.sf_id)
        else:
            key = c.name.strip().lower()
            if key in seen_unmapped_names:
                continue
            seen_unmapped_names.add(key)
        out.append(c)

    return out
