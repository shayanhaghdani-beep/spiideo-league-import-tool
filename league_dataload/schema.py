"""Dataclasses for the league-forecast pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ForecastRow:
    """One row of the GTM League DB CSV (one rep's forecast of one league)."""
    source_row_number: int           # 1-based row index in the CSV (after preamble)
    rep_name: str
    league: str                      # the league/competition name
    sport: str
    product: str                     # subscription product (matches pricebook)
    deal_type: str                   # "New", "Upsell", "Renewal", "Expansion", ...
    arr_eur: float                   # ARR in EUR (negotiated, not list)
    # Optional / informational
    territory: str = ""
    submitted_date: str = ""
    period: str = ""
    priority_rank: str = ""
    tier: str = ""
    gtm_motion: str = ""
    target_close: str = ""
    decision_maker: str = ""
    entry_point: str = ""
    competition_risk: str = ""
    marketing_ask: str = ""
    strategic_rationale: str = ""
    new_product_to_add: str = ""
    clubs_to_activate: str = ""
    prerequisites: str = ""
    notes: str = ""
    scope: str = ""
    sf_league_account_id: str = ""   # if rep already linked it
    competitor_renewal_year: str = ""
    product_feature_ask: str = ""
    master_opportunity: str = ""     # Master/Mother Opportunity ID this deal rolls up to

    # Filled in by resolve step
    resolved_rep_user_id: str = ""


@dataclass
class LeagueAccount:
    """Deduped view of one league across all reps that forecasted it."""
    display_name: str                # canonical name (chosen from reps' spellings)
    normalized_name: str
    sports: list[str] = field(default_factory=list)        # unique sports across rows
    rep_names: list[str] = field(default_factory=list)     # unique reps
    territories: list[str] = field(default_factory=list)   # unique territories across rows
    sf_league_account_id: str = ""                          # if any rep filled it
    forecast_rows: list[ForecastRow] = field(default_factory=list)

    # Match against existing SF league accounts (filled by crosschecker)
    match_status: str = "unmatched"            # 'matched', 'ambiguous', 'unmatched'
    match_source: str = ""                     # 'HubSpot + Salesforce' | 'Salesforce only' | 'HubSpot only (no SF Account ID)'
    match_type: str = ""                       # 'exact', 'normalised', 'acronym', ...
    match_confidence: int = 0                  # 0-100
    match_confidence_label: str = ""           # 'High', 'Medium', 'Low'
    matched_sf_ids: list[str] = field(default_factory=list)
    matched_sf_names: list[str] = field(default_factory=list)
    matched_hs_record_ids: list[str] = field(default_factory=list)  # HubSpot Record IDs of matched candidates
    crosscheck_note: str = ""
    # Deal-dedup signal: existing deals on the matched account (from HubSpot)
    matched_associated_deals: int = 0
    matched_active_arr: str = ""

    @property
    def primary_sport(self) -> str:
        return self.sports[0] if self.sports else ""

    @property
    def total_arr_eur(self) -> float:
        return sum(r.arr_eur for r in self.forecast_rows)


@dataclass
class HubspotDeal:
    """One deal row from the HubSpot 'deals for CRM matching' export.

    Linked to a league candidate via ``company_record_id`` (== the candidate's
    ``hs_record_id``) or, as a fallback, the company name.
    """
    deal_id: str
    name: str
    company_record_id: str
    company_name: str
    stage: str
    close_date: str            # original verbatim ('YYYY-MM-DD HH:MM' or '')
    close_period: str          # 'H2 2026' | '2027' (forecast window the close date falls in)
    amount: str
    owner: str
    create_date: str = ""      # original verbatim deal Create Date
    sf_opportunity_id: str = ""  # HubSpot 'Salesforce Opportunity ID' (existing SF Opp, if synced)

    @property
    def is_open(self) -> bool:
        """Open (in-flight) deal — not yet Closed Won/Lost."""
        return not self.stage.lower().startswith("closed")


@dataclass
class DealAlias:
    """Manual league→partner-account mapping for deal de-duplication.

    Some leagues have their deals filed under a DIFFERENT account than the
    league itself — a partner/reseller, a parent federation, or a differently
    spelled/translated entity (e.g. league 'FIPAV Femminile' → account
    'I.T. GARAGE snc'). These relationships carry no algorithmic name signal, so
    they're maintained by hand in ``engine/data/league_deal_aliases.csv`` and
    let the period-deal scan link such deals to the right league anyway.
    """
    league: str                  # league name (matched by normalised containment)
    company_name: str = ""       # partner/account name on the deal (optional)
    company_record_id: str = ""  # HubSpot Record ID of the partner account (optional)
    reason: str = ""             # why they differ (free text, surfaced in the report)


@dataclass
class ManualAccountMatch:
    """Hand-curated league→existing-CRM-account match (canonical manual input).

    The reviewer pastes an existing Salesforce Account ID (and optionally a
    HubSpot Record ID) for a league the automatic matcher missed. Maintained in
    ``engine/data/manual_account_ids.csv``; the matcher trusts it exactly like
    the GTM DB 'SF League / Account ID' column, so the league counts as MATCHED
    (no new account created) and — when the HubSpot Record ID is known — its
    HubSpot deals link automatically.
    """
    league: str                  # league display name (matched exact, then normalised)
    sf_account_id: str = ""      # 18-char Salesforce Account ID
    hs_record_id: str = ""       # optional HubSpot Record ID (enables deal linking)
    note: str = ""               # free text, surfaced in the crosscheck note


@dataclass
class SFLeagueCandidate:
    """A league-type Account already in Salesforce (or HubSpot-linked)."""
    sf_id: str
    name: str
    org_type: str = ""
    sport: str = ""
    country: str = ""
    source: str = ""               # 'hubspot' | 'salesforce' — which pool this candidate came from
    hs_record_id: str = ""         # HubSpot 'Record ID' (lets a human enter a HS id)
    associated_deals: int = 0      # HubSpot 'Number of Associated Deals'
    associated_accounts: int = 0   # HubSpot 'Number of associated account (active & cancelled)'
    active_arr: str = ""           # HubSpot 'Active ARR (new)' (verbatim)


@dataclass(frozen=True)
class Product:
    """One row from data/pricebook.csv (used to resolve Opp Product line items)."""
    pricebook_name: str          # "Younium-Spiideo AB"
    product_name: str            # "Spiideo Perform PRO" or "S-Line WIDE MK III (with mic)"
    product_id: str              # 01t...
    pricebook_entry_id: str      # 01u...
    currency: str                # "USD" / "EUR"
    list_price: float
    family: str                  # "subscription" | "camera"
