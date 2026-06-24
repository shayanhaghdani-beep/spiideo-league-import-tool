"""Multi-pass matcher: League name → existing SF account candidate(s).

Mirrors the structure of conference_crm_crosscheck.match_conferences (passes
1-7) but with leagues instead of NCAA conferences, and an in-memory list of
SF candidates fetched live via SOQL instead of an exported CRM CSV.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .normalize import (
    extract_acronym,
    is_junk,
    normalise_league,
    significant_words,
    strip_sport_division,
)
from .schema import DealAlias, LeagueAccount, ManualAccountMatch, SFLeagueCandidate
from .territory import country_signal, parse_territory


_MATCH_TYPE_SCORES = {
    "exact": 100,
    "normalised": 95,
    "alias": 90,
    "acronym": 80,
    "core": 78,            # conference "core" name match (sport/type-word stripped)
    "parenthetical": 75,
    "containment": 60,
    "fuzzy_overlap": 50,
}

# Generic tokens dropped when reducing a conference name to its identifying
# "core" (e.g. "Rocky Mountain Athletic Conference" → "rocky mountain").
_TYPE_TOKENS = {
    "conference", "league", "association", "athletic", "intercollegiate",
    "collegiate", "federation", "division", "the", "of", "and",
}


def _core(norm: str) -> str:
    """Reduce a normalised name to its identifying tokens (drop type words)."""
    return " ".join(t for t in norm.split() if t not in _TYPE_TOKENS)


# Connectors skipped when testing whether a trailing token is the name's acronym
# (so "University of Southern California USC" → USC, not UOSC).
_ACR_SKIP = {"of", "the", "and", "for", "in", "to", "on", "at"}


def _split_trailing_acronym(norm: str) -> tuple[str, str | None]:
    """If a normalised name ends in its own acronym, split it off.

    "coastal athletic association caa" → ("coastal athletic association", "caa")
    because CAA = initials of the preceding words. Returns (norm, None) when the
    trailing token isn't the acronym of what precedes it, so this never strips a
    real trailing word.
    """
    toks = norm.split()
    if len(toks) < 3:
        return norm, None
    last = toks[-1]
    if not (2 <= len(last) <= 6) or not last.isalpha():
        return norm, None
    base = toks[:-1]
    init_all = "".join(t[0] for t in base)
    init_sig = "".join(t[0] for t in base if t not in _ACR_SKIP)
    if last in (init_all, init_sig):
        return " ".join(base), last
    return norm, None


def confidence_label(match_type: str) -> str:
    score = _MATCH_TYPE_SCORES.get(match_type, 40)
    if score >= 90:
        return "High"
    if score >= 70:
        return "Medium"
    return "Low"


# Hand-curated aliases for leagues that go by multiple names. Extend as needed.
# Key = canonical league name (as it appears in the forecast).
# Values = alternative names that might appear in Salesforce.
LEAGUE_ALIASES: dict[str, list[str]] = {
    # "AE" is too short for the acronym passes (2 chars), but it's the rep's
    # shorthand for the America East Conference (already in SF).
    "AE": ["America East Conference"],
}

# Disambiguation for acronyms shared by several CRM accounts. Maps a forecast
# acronym (lowercased, space-free) → the normalised CRM name to prefer.
# e.g. both "Southland Conference (SLC)" and "Sun-Lakes Conference (SLC)" carry
# the acronym SLC; for these collegiate plans "SLC" always means Southland.
#
# The collegiate entries below were resolved by reasoning about each rep's book:
# every conference the rep forecasts is the same NCAA division, so the colliding
# acronym must be the candidate at that division (e.g. Needham is all-D1 → SWAC
# = Southwestern, the D1 conference, not Scenic West, the NJCAA one).
ACRONYM_OVERRIDES: dict[str, str] = {
    "slc": "southland conference",
    "swac": "southwestern athletic conference",      # D1 (rep book all D1); not Scenic West (NJCAA)
    "mac": "mid american conference",                # D1/FBS (rep book all D1); not Middle Atlantic (D3)
    "gnac": "great northwest athletic conference",   # D2 West-coast book; not Great Northeast (D3, NE)
}

# Acronym collisions that flip on the NCAA division tagged in the forecast name.
# {acronym: {division: preferred normalised CRM name}}. Checked before the flat
# ACRONYM_OVERRIDES so the same acronym can resolve differently per division.
# MIAA is the canonical case: "MIAA - D2" = Mid-America (D2), "MIAA - D3" =
# Michigan (D3) — same initials, different conference, decided by the tag.
DIVISION_OVERRIDES: dict[str, dict[str, str]] = {
    "miaa": {
        "d2": "mid america intercollegiate athletics association",
        "d3": "michigan intercollegiate athletic association",
    },
}


_DIVISION_RE = re.compile(r"\b(d\s*-?\s*(?:iii|ii|i|[123])|naia|njcaa)\b", re.I)


def _extract_division(name: str) -> str:
    """Pull an NCAA division marker from a forecast name → 'd1'/'d2'/'d3'/'naia'/'njcaa'.

    Handles arabic ("D2", "- D2") and roman ("DII", "D III") forms. Returns ''
    when no division is tagged.
    """
    m = _DIVISION_RE.search(name.lower())
    if not m:
        return ""
    tok = m.group(1).replace(" ", "").replace("-", "")
    for roman, arabic in (("diii", "d3"), ("dii", "d2"), ("di", "d1")):
        if tok == roman:
            return arabic
    return tok

# Match types precise enough that two forecast aliases resolving to the same SF
# account means they're genuinely the same conference (so they may share it).
# Only weaker passes enforce one-account-per-league exclusivity.
_WEAK_EXCLUSIVE_TYPES = {"containment", "fuzzy_overlap"}


@dataclass(frozen=True)
class MatchResult:
    """The best match (if any) for a single LeagueAccount against SF candidates."""
    match_status: str          # 'matched' | 'ambiguous' | 'unmatched'
    match_type: str            # name of the pass that fired ('' if unmatched)
    confidence: int
    confidence_label: str
    matched_sf_ids: list[str]
    matched_sf_names: list[str]
    note: str
    matched_associated_deals: int = 0   # existing deals on the best matched account
    matched_active_arr: str = ""
    matched_hs_record_ids: list[str] = field(default_factory=list)  # HubSpot Record IDs of matched candidates
    match_source: str = ""              # 'HubSpot + Salesforce' | 'Salesforce only' | 'HubSpot only (no SF Account ID)'


def _dedup_keep_order(items: list[str]) -> list[str]:
    """De-duplicate a list of strings while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _classify_source(has_sf: bool, has_hs: bool) -> str:
    """Label a match by which system(s) supplied an identifier.

    'HubSpot only (no SF Account ID)' is the duplicate-risk case: the league
    exists in HubSpot but carries no Salesforce Account ID, so importing it
    blindly would create a fresh SF account that may already be tracked.
    """
    if has_sf and has_hs:
        return "HubSpot + Salesforce"
    if has_sf:
        return "Salesforce only"
    if has_hs:
        return "HubSpot only (no SF Account ID)"
    return ""


def _alias_account(
    league: LeagueAccount,
    aliases: list[DealAlias],
    sf_by_hs: dict[str, SFLeagueCandidate],
    sf_by_name: dict[str, SFLeagueCandidate],
) -> tuple[SFLeagueCandidate | None, DealAlias | None]:
    """If a manual alias maps this league to an account present in the candidate
    pool, return ``(candidate, alias)``; else ``(None, None)``.

    The alias's ``league`` token must appear as a contiguous token-run in the
    league's display/normalised name (same containment rule the deal scan uses).
    The target account is resolved by HubSpot Record ID first, then company name.
    First resolvable alias wins.
    """
    names = [normalise_league(league.display_name), league.normalized_name]
    for al in aliases:
        an = normalise_league(al.league)
        if not an:
            continue
        atoks = an.split()
        if not any(_find_token_run(atoks, nm.split()) >= 0 for nm in names if nm):
            continue
        cand = sf_by_hs.get(al.company_record_id) if al.company_record_id else None
        if cand is None and al.company_name:
            cand = sf_by_name.get(normalise_league(al.company_name))
        if cand is not None:
            return cand, al
    return None, None


def _find_token_run(needle: list[str], hay: list[str]) -> int:
    """Index where ``needle`` tokens first appear as a contiguous run in ``hay``,
    else -1."""
    if not needle or len(needle) > len(hay):
        return -1
    for i in range(len(hay) - len(needle) + 1):
        if hay[i:i + len(needle)] == needle:
            return i
    return -1


def crosscheck_leagues(
    leagues: list[LeagueAccount],
    sf_candidates: list[SFLeagueCandidate],
    aliases: list[DealAlias] | None = None,
    manual_matches: list[ManualAccountMatch] | None = None,
) -> None:
    """Mutate each LeagueAccount in place with crosscheck results.

    The matcher uses a **composite confidence score**:

        base score (from match type)
        + 20 if candidate.country is in the forecast's territory countries
        - 40 if candidate.country exists AND conflicts with the territory

    A match is kept only if the composite score is ≥ 45 (Medium threshold).
    Below that, the league is left unmatched so the human can review.

    Also enforces **pass-exclusivity**: once a SF Account ID has been claimed
    by one league, no later league can use the same one.
    """
    aliases = aliases or []
    # Hand-curated league→account overrides (manual_account_ids.csv), keyed by
    # normalised league name for exact lookup in the per-league loop.
    manual_by_norm: dict[str, ManualAccountMatch] = {}
    for mm in (manual_matches or []):
        if mm.sf_account_id:
            manual_by_norm[normalise_league(mm.league)] = mm
    sf_by_id = {c.sf_id: c for c in sf_candidates if c.sf_id}
    # Lets a human paste a HubSpot Record ID into the GTM DB
    # 'SF League / Account ID' column; we translate it to the linked SF account.
    sf_by_hs = {c.hs_record_id: c for c in sf_candidates if c.hs_record_id}
    # Resolve manual partner/federation aliases (league_deal_aliases.csv) to an
    # account in the pool, so the same hand-curated mapping that links DEALs also
    # promotes the league to a matched ACCOUNT (one source of truth, not two).
    sf_by_name = {normalise_league(c.name): c for c in sf_candidates if c.name}

    valid: list[SFLeagueCandidate] = []
    for c in sf_candidates:
        junk, _ = is_junk(c.name)
        if not junk:
            valid.append(c)

    by_exact: dict[str, list[SFLeagueCandidate]] = defaultdict(list)
    by_norm: dict[str, list[SFLeagueCandidate]] = defaultdict(list)
    by_acr: dict[str, list[SFLeagueCandidate]] = defaultdict(list)
    by_acr_norm: dict[str, list[SFLeagueCandidate]] = defaultdict(list)
    by_paren: dict[str, list[SFLeagueCandidate]] = defaultdict(list)
    by_full_initials: dict[str, list[SFLeagueCandidate]] = defaultdict(list)
    by_core: dict[str, list[SFLeagueCandidate]] = defaultdict(list)
    by_core_ns: dict[str, list[SFLeagueCandidate]] = defaultdict(list)

    for c in valid:
        if not c.name:
            continue
        by_exact[c.name].append(c)
        cnorm = normalise_league(c.name)
        by_norm[cnorm].append(c)
        core = _core(cnorm)
        if core and len(core) >= 3:
            by_core[core].append(c)
            # Space-free core so a concatenated forecast spelling ("Lonestar")
            # reaches a spaced CRM core ("lone star"). Guarded to ≥5 chars to
            # avoid short-token collisions.
            core_ns = core.replace(" ", "")
            if len(core_ns) >= 5:
                by_core_ns[core_ns].append(c)
        acr = extract_acronym(c.name)
        if acr:
            by_acr[acr].append(c)
            # Space-insensitive acronym key so forecast "Pac 12" reaches
            # the CRM acronym "(Pac-12)", and "NE10" reaches "(NE10)".
            by_acr_norm[normalise_league(acr).replace(" ", "")].append(c)
        m = re.search(r"\(([^)]+)\)\s*$", c.name)
        if m and len(m.group(1).strip()) > 6:
            by_paren[normalise_league(m.group(1))].append(c)
        # Build first-letters acronym index: 'Fédération Française de Basket-Ball'
        # → 'ffdbb' → useful for matching forecast acronyms like 'FFBB'.
        initials = _initials_of(c.name)
        if initials and len(initials) >= 3:
            by_full_initials[initials].append(c)

    # Pass-exclusivity: track which SF Ids are already claimed
    claimed_sf_ids: set[str] = set()

    for league in leagues:
        # 0. Hand-curated override (manual_account_ids.csv) → trust it. This is
        #    the canonical home for reviewer-entered IDs and wins over the
        #    automatic passes. The account may NOT be in the candidate pool
        #    (an SF-only account); we still mark it matched (no new account is
        #    created) and carry whatever HubSpot Record ID is known so its deals
        #    can link.
        mm = manual_by_norm.get(normalise_league(league.display_name)) \
            or manual_by_norm.get(league.normalized_name)
        if mm is not None:
            cand = sf_by_id.get(mm.sf_account_id)
            hs = (cand.hs_record_id if cand else "") or mm.hs_record_id
            claimed_sf_ids.add(mm.sf_account_id)
            note = "Matched via manual account-ID override (manual_account_ids.csv)"
            if mm.note:
                note += f": {mm.note}"
            _apply_match(league, MatchResult(
                match_status="matched",
                match_type="manual",
                confidence=100,
                confidence_label="High",
                matched_sf_ids=[mm.sf_account_id],
                matched_sf_names=[cand.name] if cand else [],
                note=note,
                matched_associated_deals=cand.associated_deals if cand else 0,
                matched_active_arr=cand.active_arr if cand else "",
                matched_hs_record_ids=[hs] if hs else [],
                match_source=_classify_source(True, bool(hs)),
            ))
            continue

        # 1. Pre-existing ID in the GTM DB column → trust it (claim it).
        #    Accepts EITHER a Salesforce Account ID OR a HubSpot Record ID, so a
        #    human reviewer can paste whichever id they have on hand.
        manual_id = league.sf_league_account_id.strip()
        sf = sf_by_id.get(manual_id) or sf_by_hs.get(manual_id) if manual_id else None
        if sf is not None:
            claimed_sf_ids.add(sf.sf_id)
            via = "SF Account ID" if manual_id in sf_by_id else "HubSpot Record ID"
            _apply_match(league, MatchResult(
                match_status="matched",
                match_type="exact",
                confidence=100,
                confidence_label="High",
                matched_sf_ids=[sf.sf_id] if sf.sf_id else [],
                matched_sf_names=[sf.name],
                note=f"Used {via} from the forecast row",
                matched_associated_deals=sf.associated_deals,
                matched_active_arr=sf.active_arr,
                matched_hs_record_ids=[sf.hs_record_id] if sf.hs_record_id else [],
                match_source=_classify_source(bool(sf.sf_id), bool(sf.hs_record_id)),
            ))
            continue

        # 2. Manual partner/federation alias → trust it as an account match.
        acand, al = _alias_account(league, aliases, sf_by_hs, sf_by_name)
        if acand is not None:
            if acand.sf_id:
                claimed_sf_ids.add(acand.sf_id)
            reason = (al.reason or "").strip()
            _apply_match(league, MatchResult(
                match_status="matched",
                match_type="alias",
                confidence=100,
                confidence_label="High",
                matched_sf_ids=[acand.sf_id] if acand.sf_id else [],
                matched_sf_names=[acand.name],
                note=f"Matched via partner/federation alias '{al.league}'"
                     + (f": {reason}" if reason else ""),
                matched_associated_deals=acand.associated_deals,
                matched_active_arr=acand.active_arr,
                matched_hs_record_ids=[acand.hs_record_id] if acand.hs_record_id else [],
                match_source=_classify_source(bool(acand.sf_id), bool(acand.hs_record_id)),
            ))
            continue

        result = _match_one(
            league, by_exact, by_norm, by_acr, by_acr_norm, by_paren,
            by_full_initials, by_core, by_core_ns,
            claimed_sf_ids=claimed_sf_ids,
        )
        # Claim the SF Id only for weak-pass matches, so no later league reuses
        # a fuzzy/containment hit. Strong, precise matches (exact/normalised/
        # acronym/core/parenthetical) may be shared — two such forecast aliases
        # resolving to the same account just means it's the same conference.
        if (result.match_status == "matched" and result.matched_sf_ids
                and result.match_type in _WEAK_EXCLUSIVE_TYPES):
            best_id = result.matched_sf_ids[0]
            if best_id:
                claimed_sf_ids.add(best_id)
        _apply_match(league, result)


def _initials_of(name: str) -> str:
    """First chars of significant words. 'Fédération Française de Basket-Ball' → 'ffbb';
    'Tier 1 Elite Hockey League' → 't1ehl'.

    Drops common stopwords ('de', 'la', 'of', 'the', 'and') so French/Italian
    federation names produce sensible initials. Numeric tokens are kept (so an
    acronym like 'T1EHL' reaches a name with an embedded digit).
    """
    skip = {"de", "la", "le", "of", "the", "and", "et", "du", "des", "den"}
    norm = normalise_league(name)
    parts = [p for p in re.split(r"[\s\-]+", norm) if p and p not in skip]
    return "".join(p[0] for p in parts if p[0].isalnum())


_MEDIUM_THRESHOLD = 45   # min composite score to keep a match


def _match_one(
    league: LeagueAccount,
    by_exact: dict[str, list[SFLeagueCandidate]],
    by_norm: dict[str, list[SFLeagueCandidate]],
    by_acr: dict[str, list[SFLeagueCandidate]],
    by_acr_norm: dict[str, list[SFLeagueCandidate]],
    by_paren: dict[str, list[SFLeagueCandidate]],
    by_full_initials: dict[str, list[SFLeagueCandidate]],
    by_core: dict[str, list[SFLeagueCandidate]],
    by_core_ns: dict[str, list[SFLeagueCandidate]],
    claimed_sf_ids: set[str],
) -> MatchResult:
    name = league.display_name
    norm = league.normalized_name
    # Sport/division-stripped view of the forecast name, used by the
    # conference-aware passes below ("SoCon Basketball" → "socon").
    clean = normalise_league(strip_sport_division(name))
    clean_core = _core(clean)
    # Acronym key built from the core so leftover connectors/type words don't
    # break it ("SUNYAC MIH & WIH" → core "sunyac" → acronym key "sunyac").
    clean_acr = clean_core.replace(" ", "")

    # Aggregate territory countries across all forecast rows for this league
    territory_countries: set[str] = set()
    for fr in league.forecast_rows:
        territory_countries |= parse_territory(getattr(fr, "territory", ""))

    seen: set[str] = set()
    hits: list[tuple[SFLeagueCandidate, str]] = []

    def add(c: SFLeagueCandidate, mtype: str) -> None:
        # Exclusivity only blocks reuse of ids claimed by weak prior matches.
        if (mtype in _WEAK_EXCLUSIVE_TYPES and c.sf_id
                and c.sf_id in claimed_sf_ids):
            return
        if c.sf_id in seen:
            return
        seen.add(c.sf_id)
        hits.append((c, mtype))

    # Pass 1: Exact
    for c in by_exact.get(name, []):
        add(c, "exact")

    # Pass 2: Normalised
    for c in by_norm.get(norm, []):
        add(c, "normalised")

    # Pass 3: Explicit aliases
    if name in LEAGUE_ALIASES:
        for alias in LEAGUE_ALIASES[name]:
            for c in by_exact.get(alias, []):
                add(c, "alias")
            for c in by_norm.get(normalise_league(alias), []):
                add(c, "alias")

    # Pass 4: Acronym — extract from the forecast league name if present
    league_acr = extract_acronym(name)
    if league_acr:
        ncaa_sig = significant_words(norm)
        for c in by_acr.get(league_acr, []):
            if _acronym_safe(c, ncaa_sig):
                add(c, "acronym")
        for c in by_exact.get(league_acr.upper(), []):
            add(c, "acronym")

    # Pass 5: Parenthetical content match
    for c in by_paren.get(norm, []):
        add(c, "parenthetical")

    # ----- Conference-aware passes (use the sport/division-stripped name) -----
    # These let "SoCon Basketball", "SLC Football", "Pac 12 Soccer",
    # "RMAC - D2" etc. reach the bare CRM conference account.
    if clean and clean != norm:
        # 5a: normalised match on the cleaned name
        for c in by_norm.get(clean, []):
            add(c, "normalised")
        # 5b: parenthetical match on the cleaned name
        for c in by_paren.get(clean, []):
            add(c, "parenthetical")

    # 5c: Bare-acronym — treat the cleaned forecast name as an acronym and look
    # it up against the CRM's parenthetical acronyms ("SLC" → "Southland
    # Conference (SLC)", "SoCon" → "Southern Conference (SoCon)").
    if 3 <= len(clean_acr) <= 8 and clean_acr.isalnum():
        # Division-tagged collision first ("MIAA - D2" → Mid-America), then the
        # flat per-acronym override (rep-portfolio / region inference).
        division = _extract_division(name)
        override = None
        if clean_acr in DIVISION_OVERRIDES and division:
            override = DIVISION_OVERRIDES[clean_acr].get(division)
        if override is None:
            override = ACRONYM_OVERRIDES.get(clean_acr)
        # Prefer explicit parenthetical acronyms; only fall back to coincidental
        # first-initials when no CRM account spells the acronym out. (Stops
        # 'OHL' tying Ontario Hockey League against Okanagan/Optibet initials.)
        acr_cands = by_acr_norm.get(clean_acr, []) or by_full_initials.get(clean_acr, [])
        if override:
            acr_cands = [c for c in acr_cands
                         if normalise_league(c.name) == override]
        for c in acr_cands:
            add(c, "acronym")

    # 5d: Conference core — identifying tokens match after dropping sport and
    # generic type words ("Empire 8 MIH" → "empire 8" == core of
    # "Empire 8 Athletic Conference"; "Big10" → "big 10" == core of
    # "Big Ten Conference").
    if clean_core and len(clean_core) >= 3:
        for c in by_core.get(clean_core, []):
            add(c, "core")
        # Space-free core fallback ("Lonestar" → "lone star" core).
        clean_core_ns = clean_core.replace(" ", "")
        if len(clean_core_ns) >= 5:
            for c in by_core_ns.get(clean_core_ns, []):
                add(c, "core")

    # 5e: Bare trailing acronym — the forecast name appends its own acronym with
    # no parentheses ("Coastal Athletic Association CAA"). Strip it and match the
    # base name, and also look the acronym up against CRM parenthetical acronyms.
    deacr, trailing_acr = _split_trailing_acronym(clean)
    if trailing_acr:
        for c in by_norm.get(deacr, []):
            add(c, "normalised")
        for c in by_paren.get(deacr, []):
            add(c, "parenthetical")
        deacr_core = _core(deacr)
        if deacr_core and len(deacr_core) >= 3:
            for c in by_core.get(deacr_core, []):
                add(c, "core")
        for c in by_acr_norm.get(trailing_acr, []):
            add(c, "acronym")

    # Pass 6: Containment — whole-word boundaries, both sides ≥ 4 chars
    if not hits and len(norm) >= 8:
        for crm_norm, candidates in by_norm.items():
            if len(crm_norm) < 4:
                continue
            if _whole_word_contains(crm_norm, norm) or _whole_word_contains(norm, crm_norm):
                for c in candidates:
                    add(c, "containment")
                if hits:
                    break

    # Pass 7: Fuzzy word overlap (80%+)
    if not hits:
        league_words = significant_words(norm)
        if len(league_words) >= 2:
            for crm_norm, candidates in by_norm.items():
                crm_words = significant_words(crm_norm)
                if not crm_words:
                    continue
                overlap = len(league_words & crm_words) / max(len(league_words), len(crm_words))
                if overlap >= 0.8:
                    for c in candidates:
                        add(c, "fuzzy_overlap")

    if not hits:
        return MatchResult(
            match_status="unmatched",
            match_type="",
            confidence=0,
            confidence_label="",
            matched_sf_ids=[],
            matched_sf_names=[],
            note="No matching account found",
        )

    # --- Composite scoring: base + country signal per hit ---
    # Country signal applies different rules per match-type strength:
    #   - For high-confidence name passes (exact/normalised/alias), country
    #     can ONLY ADD points (+20). It can't veto a perfect name match,
    #     because HubSpot's Country field is often mis-tagged (we've seen
    #     'Portuguese Basketball Federation' wrongly labelled country=US, etc.)
    #   - For weaker passes (acronym/containment/fuzzy), country can ADD or
    #     SUBTRACT — there it's a critical disambiguator
    NAME_PASSES = {"exact", "normalised", "alias"}
    scored_hits: list[tuple[SFLeagueCandidate, str, int]] = []
    for c, mtype in hits:
        base = _MATCH_TYPE_SCORES.get(mtype, 0)
        ccy_raw = country_signal(territory_countries, c.country)
        ccy = max(0, ccy_raw) if mtype in NAME_PASSES else ccy_raw
        scored_hits.append((c, mtype, base + ccy))

    # Keep only hits at/above the Medium threshold
    above_threshold = [h for h in scored_hits if h[2] >= _MEDIUM_THRESHOLD]
    if not above_threshold:
        # Best hit existed but was demoted out of consideration (country conflict
        # or low-confidence match without supporting signal)
        best = max(scored_hits, key=lambda h: h[2])
        return MatchResult(
            match_status="unmatched",
            match_type=best[1],
            confidence=max(0, best[2]),
            confidence_label="",
            matched_sf_ids=[],
            matched_sf_names=[],
            note=(
                f"Best candidate {best[0].name!r} demoted "
                f"({best[1]}, base={_MATCH_TYPE_SCORES.get(best[1], 0)}, "
                f"composite={best[2]}); below Medium threshold"
            ),
        )

    # Pick the highest composite score
    best = max(above_threshold, key=lambda h: h[2])
    best_score = best[2]
    best_type = best[1]
    best_winners = [h for h in above_threshold if h[2] == best_score]
    # Tied winners that are the SAME account duplicated in the CRM aren't a real
    # ambiguity — collapse to one. We compare on the NORMALISED name, so rows
    # that differ only in punctuation/formatting ("USA South Athletic Conference"
    # vs "USA-South Athletic Conference") collapse, while genuinely different
    # conferences sharing an acronym ("Mid-American" vs "Middle Atlantic") stay
    # ambiguous because their normalised names differ.
    winner_names = {normalise_league(h[0].name) for h in best_winners}

    if len(above_threshold) == 1:
        status = "matched"
        note = f"Single match via {best_type} (score {best_score})"
    elif len(best_winners) == 1:
        status = "matched"
        note = (f"Best match via {best_type} (score {best_score}); "
                f"{len(above_threshold)-1} weaker hit(s)")
    elif len(winner_names) == 1:
        status = "matched"
        note = (f"Match via {best_type} (score {best_score}); "
                f"{len(best_winners)} duplicate CRM rows for the same account")
    else:
        status = "ambiguous"
        note = (f"{len(best_winners)} candidates tied at score {best_score} "
                f"via {best_type}; manual review required")

    # If we have country signal that actually fired, mention it
    country_note = ""
    if territory_countries and best[0].country:
        sig = country_signal(territory_countries, best[0].country)
        if sig > 0:
            country_note = f"; country match: {best[0].country}"
        elif sig < 0:
            country_note = f"; country conflict ({best[0].country} vs {territory_countries})"
    note += country_note

    # Order ids by composite score desc, then by how many Spiideo accounts hang
    # off the candidate (HubSpot 'Number of associated accounts', then deals), so
    # when duplicate CRM rows tie on score we surface the most-populated record
    # first — that's the one to keep/import against. Falls back to stable order
    # when the counts are unknown (0/0), e.g. SF-Accounts-tab duplicates.
    above_threshold.sort(
        key=lambda h: (-h[2], -h[0].associated_accounts, -h[0].associated_deals)
    )
    best_candidate = above_threshold[0][0]
    # Consolidate IDs across all kept hits: drop empties (a HubSpot-only
    # candidate has no SF Id), de-dup, and keep order. This means a league found
    # in BOTH pools surfaces its real SF Account ID and its HS Record ID cleanly,
    # and a "matched" league never emits a blank Account ID into the upsert CSVs.
    clean_sf_ids = _dedup_keep_order([c.sf_id for c, _, _ in above_threshold if c.sf_id])
    clean_hs_ids = _dedup_keep_order(
        [c.hs_record_id for c, _, _ in above_threshold if c.hs_record_id]
    )
    return MatchResult(
        match_status=status,
        match_type=best_type,
        confidence=best_score,
        confidence_label="High" if best_score >= 90 else ("Medium" if best_score >= 70 else "Low"),
        matched_sf_ids=clean_sf_ids,
        # Names stay unfiltered (all kept hits, score-sorted) so a HubSpot-only
        # match — which has no SF Id — still shows the matched account name.
        matched_sf_names=[c.name for c, _, _ in above_threshold],
        note=note,
        matched_associated_deals=best_candidate.associated_deals,
        matched_active_arr=best_candidate.active_arr,
        matched_hs_record_ids=clean_hs_ids,
        match_source=_classify_source(bool(clean_sf_ids), bool(clean_hs_ids)),
    )


def _whole_word_contains(outer: str, inner: str) -> bool:
    """True if `inner` appears in `outer` as a whole word (or word phrase).

    Uses simple whitespace boundaries — both sides are pre-normalized, so word
    boundaries are spaces only. Prevents 'german' from matching 'germany'
    while still allowing 'asobal' to match 'liga asobal'.
    """
    if inner == outer:
        return True
    return (
        outer.startswith(inner + " ")
        or outer.endswith(" " + inner)
        or (" " + inner + " ") in outer
    )


def _acronym_safe(c: SFLeagueCandidate, league_sig_words: set[str]) -> bool:
    """Block acronym collisions (e.g., AEC = America East ≠ Atlantic East).

    Mirrors conference_crm_crosscheck._acronym_safe (line 422). If the CRM
    candidate name is itself a short all-caps acronym, treat it as safe; else
    require >50% significant-word overlap with the forecast league name.
    """
    base = re.sub(r"\s*\([^)]*\)\s*$", "", c.name).strip()
    if base.isupper() and len(base) <= 6:
        return True
    crm_sig = significant_words(normalise_league(c.name))
    if not crm_sig or not league_sig_words:
        return False
    overlap = len(league_sig_words & crm_sig)
    return overlap > min(len(league_sig_words), len(crm_sig)) * 0.5


def _apply_match(league: LeagueAccount, result: MatchResult) -> None:
    league.match_status = result.match_status
    league.match_source = result.match_source
    league.match_type = result.match_type
    league.match_confidence = result.confidence
    league.match_confidence_label = result.confidence_label
    league.matched_sf_ids = list(result.matched_sf_ids)
    league.matched_sf_names = list(result.matched_sf_names)
    league.matched_hs_record_ids = list(result.matched_hs_record_ids)
    league.crosscheck_note = result.note
    league.matched_associated_deals = result.matched_associated_deals
    league.matched_active_arr = result.matched_active_arr


# ---------------------------------------------------------------------------
# Dedup forecast rows → LeagueAccount


def _accumulate(la: LeagueAccount, r) -> None:
    la.forecast_rows.append(r)
    if r.sport and r.sport not in la.sports:
        la.sports.append(r.sport)
    if r.rep_name and r.rep_name not in la.rep_names:
        la.rep_names.append(r.rep_name)
    territory = getattr(r, "territory", "")
    if territory and territory not in la.territories:
        la.territories.append(territory)
    if r.sf_league_account_id and not la.sf_league_account_id:
        la.sf_league_account_id = r.sf_league_account_id


def dedupe_leagues(rows: list) -> list[LeagueAccount]:
    """One LeagueAccount per normalized league name (across all periods).

    Use for ACCOUNT creation + crosscheck — one SF account per league.
    """
    by_norm: dict[str, LeagueAccount] = {}
    for r in rows:
        norm = normalise_league(r.league)
        if not norm:
            continue
        la = by_norm.get(norm)
        if la is None:
            la = LeagueAccount(display_name=r.league.strip(), normalized_name=norm)
            by_norm[norm] = la
        _accumulate(la, r)
    return list(by_norm.values())


def dedupe_leagues_by_period(rows: list) -> list[LeagueAccount]:
    """One LeagueAccount per (normalized league name, period).

    Use for OPPORTUNITY creation — a league forecast in H2 2026 and 2027
    becomes two opps (on the same account). ARR is summed within each period.
    """
    by_key: dict[tuple[str, str], LeagueAccount] = {}
    for r in rows:
        norm = normalise_league(r.league)
        if not norm:
            continue
        period = getattr(r, "period", "") or ""
        key = (norm, period)
        la = by_key.get(key)
        if la is None:
            la = LeagueAccount(display_name=r.league.strip(), normalized_name=norm)
            by_key[key] = la
        _accumulate(la, r)
    return list(by_key.values())


def propagate_crosscheck(
    name_level: list[LeagueAccount],
    period_level: list[LeagueAccount],
) -> None:
    """Copy crosscheck results from name-deduped leagues onto period-deduped ones.

    The crosscheck runs once at the league-name level (one account per league);
    this fans the match result out to each (league, period) opportunity so they
    all carry the same matched SF Account ID / deal flags.
    """
    by_norm = {la.normalized_name: la for la in name_level}
    for pl in period_level:
        src = by_norm.get(pl.normalized_name)
        if not src:
            continue
        pl.match_status = src.match_status
        pl.match_source = src.match_source
        pl.match_type = src.match_type
        pl.match_confidence = src.match_confidence
        pl.match_confidence_label = src.match_confidence_label
        pl.matched_sf_ids = list(src.matched_sf_ids)
        pl.matched_sf_names = list(src.matched_sf_names)
        pl.matched_hs_record_ids = list(src.matched_hs_record_ids)
        pl.crosscheck_note = src.crosscheck_note
        pl.matched_associated_deals = src.matched_associated_deals
        pl.matched_active_arr = src.matched_active_arr
