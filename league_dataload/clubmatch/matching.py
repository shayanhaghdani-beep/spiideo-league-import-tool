"""7-pass match funnel — generic version of conference_crm_crosscheck.match_conferences.

Inputs are agnostic of "conference" vs "club" — they're just `truth` rows
(authoritative roster) and `crm` rows. Each pass widens the net; the first
pass that fires wins, all subsequent passes only run if no match was found.

Match types and scores (carried over from NCAA pipeline):
    exact          100   identical strings
    normalised      95   identical after normalise()
    prefix_canon    92   identical after prefix-position canonicalisation
                          (new for clubs: "FC Bayern" == "Bayern FC")
    alias           90   explicit alias map
    acronym         80   acronym match with word-overlap guard
    parenthetical   75   parenthetical content matches
    containment     60   one name contained in the other (>=8 chars)
    fuzzy_overlap   50   >=80% significant-word overlap
"""

from __future__ import annotations

import re
import urllib.parse
from collections import defaultdict
from typing import Callable

from .normalise import canonicalise_prefix_position, is_close_match


MATCH_TYPE_SCORES: dict[str, int] = {
    "exact_domain": 100,
    "exact": 100,
    "normalised": 95,
    "prefix_canon": 92,
    "alias": 90,
    "acronym": 80,
    "parenthetical": 75,
    "containment": 60,
    "fuzzy_overlap": 50,
    "llm_suggested": 55,
}


def clean_domain(domain: str) -> str:
    """Strip protocol/www/path from a domain string for comparison.

    "https://www.manutd.com/" → "manutd.com"
    "Manutd.com"               → "manutd.com"
    """
    if not domain:
        return ""
    d = domain.lower().strip()
    if d.startswith(("http://", "https://")):
        try:
            d = urllib.parse.urlparse(d).hostname or ""
        except Exception:
            return ""
    d = d.lstrip(".").rstrip("/").split("/")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def domains_match(d1: str, d2: str) -> bool:
    """Return True if two domains refer to the same org (subdomain-tolerant)."""
    a, b = clean_domain(d1), clean_domain(d2)
    if not a or not b:
        return False
    if a == b:
        return True
    return a.endswith("." + b) or b.endswith("." + a)

# Words that don't help disambiguate two entity names (filler). Kept config-
# extensible because soccer-specific stopwords differ from NCAA conference
# stopwords ("athletic", "association").
DEFAULT_STOPWORDS = {
    "the", "of", "and", "for", "club", "football", "soccer",
}


def confidence_label(match_type: str) -> str:
    score = MATCH_TYPE_SCORES.get(match_type, 40)
    if score >= 90:
        return "High"
    if score >= 70:
        return "Medium"
    return "Low"


def _significant_words(norm: str, stopwords: set[str]) -> set[str]:
    return {w for w in norm.split() if len(w) > 2 and w not in stopwords}


def _extract_paren_content(name: str) -> str | None:
    """Pull trailing '(...)' content if present."""
    m = re.search(r"\(([^)]+)\)\s*$", name)
    return m.group(1).strip() if m else None


def _extract_acronym(name: str) -> str | None:
    """Trailing parenthetical of <=12 chars treated as acronym (e.g. '(SEC)')."""
    inner = _extract_paren_content(name)
    if inner and len(inner) <= 12:
        return inner.lower()
    return None


def match(
    truth_rows: list[dict],
    crm_rows: list[dict],
    *,
    truth_name_key: str = "name",
    truth_domain_key: str = "domain",
    crm_name_key: str = "Company/Customer name",
    crm_id_key: str = "Record ID",
    crm_domain_key: str = "Company/Customer Domain Name",
    normaliser: Callable[[str], str],
    aliases: dict[str, list[str]] | None = None,
    acronyms: dict[str, str] | None = None,
    stopwords: set[str] | None = None,
) -> tuple[
    dict[str, list[dict]],  # matches: {truth_name: [{'crm_row', 'match_type'}]}
    list[str],              # unmatched truth names
    list[dict],             # orphans: CRM rows not matched to any truth row
]:
    """Run 7-pass match. CRM rows are assumed already pre-filtered for junk.

    Args:
        truth_rows: authoritative roster (each must have `truth_name_key`).
        crm_rows: HubSpot CRM rows.
        normaliser: callable returning normalised string for matching.
        aliases: {truth_name: [crm_name_variant, ...]}.
        acronyms: {normalised_truth_name: "acr"} for acronym pass.
        stopwords: union'd with DEFAULT_STOPWORDS for word-overlap passes.
    """
    aliases = aliases or {}
    acronyms = acronyms or {}
    stops = DEFAULT_STOPWORDS | (stopwords or set())

    # ---- Build CRM indices ----
    crm_by_exact: dict[str, list[dict]] = defaultdict(list)
    crm_by_norm: dict[str, list[dict]] = defaultdict(list)
    crm_by_prefix_canon: dict[str, list[dict]] = defaultdict(list)
    crm_by_acronym: dict[str, list[dict]] = defaultdict(list)
    crm_by_paren: dict[str, list[dict]] = defaultdict(list)
    crm_by_domain: dict[str, list[dict]] = defaultdict(list)

    for row in crm_rows:
        name = (row.get(crm_name_key) or "").strip()
        if not name:
            continue
        crm_by_exact[name].append(row)
        norm = normaliser(name)
        crm_by_norm[norm].append(row)
        crm_by_prefix_canon[canonicalise_prefix_position(norm)].append(row)
        acr = _extract_acronym(name)
        if acr:
            crm_by_acronym[acr].append(row)
        paren = _extract_paren_content(name)
        if paren and len(paren) > 6:
            crm_by_paren[normaliser(paren)].append(row)
        cdomain = clean_domain(row.get(crm_domain_key) or "")
        if cdomain:
            crm_by_domain[cdomain].append(row)

    # ---- Match each truth row through the funnel ----
    matches: dict[str, list[dict]] = {}
    unmatched: list[str] = []
    matched_crm_ids: set[str] = set()

    for truth in truth_rows:
        truth_name = (truth.get(truth_name_key) or "").strip()
        if not truth_name:
            continue

        seen_ids: set[str] = set()
        results: list[dict] = []

        def _add(crm_row: dict, match_type: str) -> None:
            rid = crm_row.get(crm_id_key, "")
            if rid in seen_ids:
                return
            seen_ids.add(rid)
            matched_crm_ids.add(rid)
            results.append({"crm_row": crm_row, "match_type": match_type})

        norm = normaliser(truth_name)
        prefix_canon = canonicalise_prefix_position(norm)

        # Pass 0: exact_domain — strongest signal. If truth has an authoritative
        # domain (Wikidata / football-data) that matches a CRM domain, the
        # entities are the same regardless of name spelling.
        truth_domain = clean_domain(truth.get(truth_domain_key) or "")
        if truth_domain:
            for row in crm_by_domain.get(truth_domain, []):
                _add(row, "exact_domain")
            # Also subdomain-tolerant: any CRM domain whose root matches
            for cdomain, rows in crm_by_domain.items():
                if cdomain == truth_domain:
                    continue
                if domains_match(cdomain, truth_domain):
                    for row in rows:
                        _add(row, "exact_domain")

        # Pass 1: exact
        for row in crm_by_exact.get(truth_name, []):
            _add(row, "exact")

        # Pass 2: normalised
        for row in crm_by_norm.get(norm, []):
            _add(row, "normalised")

        # Pass 3: prefix-canonical (new for clubs)
        for row in crm_by_prefix_canon.get(prefix_canon, []):
            _add(row, "prefix_canon")

        # Pass 4: explicit aliases
        for alias in aliases.get(truth_name, []):
            for row in crm_by_exact.get(alias, []):
                _add(row, "alias")
            for row in crm_by_norm.get(normaliser(alias), []):
                _add(row, "alias")

        # Pass 5: acronym match (with word-overlap safety)
        acr = acronyms.get(norm) or acronyms.get(truth_name.lower())
        if acr:
            truth_sigs = _significant_words(norm, stops)

            def _acr_safe(crm_row: dict) -> bool:
                cname = crm_row.get(crm_name_key, "")
                cbase = re.sub(r"\s*\([^)]*\)\s*$", "", cname).strip()
                if cbase.isupper() and len(cbase) <= 6:
                    return True
                cnorm = normaliser(cname)
                if cnorm == acr.lower():
                    return True
                csigs = _significant_words(cnorm, stops)
                if not csigs or not truth_sigs:
                    return False
                overlap = len(truth_sigs & csigs)
                return overlap > min(len(truth_sigs), len(csigs)) * 0.5

            for row in crm_by_acronym.get(acr.lower(), []):
                if _acr_safe(row):
                    _add(row, "acronym")
            for variant in (acr, acr.upper(), acr.lower()):
                for row in crm_by_exact.get(variant, []):
                    if _acr_safe(row):
                        _add(row, "acronym")

        # Pass 6: parenthetical
        for row in crm_by_paren.get(norm, []):
            _add(row, "parenthetical")

        # Pass 7: containment (only if still unmatched)
        # Both sides must be ≥8 chars — otherwise empty/short CRM names like
        # "." or "FC" yield false positives via empty-string containment.
        if not results:
            for cnorm_key, rows in crm_by_norm.items():
                if (
                    len(norm) >= 8
                    and len(cnorm_key) >= 8
                    and (norm in cnorm_key or cnorm_key in norm)
                ):
                    for row in rows:
                        _add(row, "containment")
                    if results:
                        break

        # Pass 8: fuzzy word overlap (only if still unmatched)
        if not results:
            truth_words = _significant_words(norm, stops)
            if len(truth_words) >= 2:
                for cnorm_key, rows in crm_by_norm.items():
                    cwords = _significant_words(cnorm_key, stops)
                    if cwords and truth_words:
                        overlap = (
                            len(truth_words & cwords)
                            / max(len(truth_words), len(cwords))
                        )
                        if overlap >= 0.8:
                            for row in rows:
                                _add(row, "fuzzy_overlap")

        # Pass 9 (last resort): edit-distance close match for typos
        if not results:
            for cnorm_key, rows in crm_by_norm.items():
                if is_close_match(norm, cnorm_key):
                    for row in rows:
                        _add(row, "fuzzy_overlap")
                    if results:
                        break

        if results:
            matches[truth_name] = results
        else:
            unmatched.append(truth_name)

    # ---- Orphans ----
    orphans: list[dict] = []
    for row in crm_rows:
        rid = row.get(crm_id_key, "")
        if rid and rid not in matched_crm_ids:
            orphans.append(row)

    return matches, unmatched, orphans


def rank_matches(
    match_list: list[dict],
    *,
    crm_active_key: str = "Number of associated account (active & cancelled)",
) -> list[dict]:
    """Sort multi-match candidates so [0] is the primary, rest are duplicates.

    Order: highest match-type score, then most active accounts.
    """
    def _key(m: dict) -> tuple[int, float]:
        score = -MATCH_TYPE_SCORES.get(m["match_type"], 0)
        active = m["crm_row"].get(crm_active_key, "0") or "0"
        try:
            active_f = -float(active)
        except (TypeError, ValueError):
            active_f = 0.0
        return (score, active_f)

    return sorted(match_list, key=_key)
