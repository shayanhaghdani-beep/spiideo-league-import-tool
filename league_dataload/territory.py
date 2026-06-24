"""Map a rep's Territory cell → list of countries for crosscheck disambiguation.

Reps write Territory in shorthand (DACH, Nordics, UK / Polen). To use HubSpot's
``Country`` field as a confidence signal, we need to expand these into
canonical country names. The mapping is intentionally generous — better to
include too many candidate countries than to wrongly exclude a match.

Used for the country-boost / country-conflict signal in the league matcher.
"""
from __future__ import annotations

import re


# Curated region/territory → country list. Lowercase keys for matching.
REGION_TO_COUNTRIES: dict[str, list[str]] = {
    "nordics": ["sweden", "denmark", "norway", "finland", "iceland"],
    "scandinavia": ["sweden", "denmark", "norway"],
    "dach": ["germany", "austria", "switzerland"],
    "benelux": ["belgium", "netherlands", "luxembourg"],
    "iberia": ["spain", "portugal"],
    "europe": [],  # too broad to be useful as a constraint
    "emea": [],
    "apac": ["australia", "japan", "south korea", "china", "india", "new zealand"],
    "anz": ["australia", "new zealand"],
}


# Common rep shortforms → canonical country names
COUNTRY_ALIASES: dict[str, str] = {
    "uk": "united kingdom",
    "u.k.": "united kingdom",
    "england": "united kingdom",
    "scotland": "united kingdom",
    "wales": "united kingdom",
    "polen": "poland",         # German spelling (Morgan typed this)
    "deutschland": "germany",
    "italien": "italy",
    "italia": "italy",
    "españa": "spain",
    "espana": "spain",
    "portuguese": "portugal",
    "usa": "united states",
    "us": "united states",
    "u.s.a.": "united states",
}


# Whitelist of recognized country names (lowercase). Anything not in here is
# silently dropped from territory parsing so junk tokens ('Ice Hockey',
# 'football', 'media') don't pollute the country signal.
COUNTRY_WHITELIST: set[str] = {
    # Europe
    "albania", "andorra", "armenia", "austria", "azerbaijan", "belarus",
    "belgium", "bosnia and herzegovina", "bulgaria", "croatia", "cyprus",
    "czech republic", "czechia", "denmark", "estonia", "finland", "france",
    "georgia", "germany", "greece", "hungary", "iceland", "ireland", "italy",
    "kazakhstan", "kosovo", "latvia", "liechtenstein", "lithuania",
    "luxembourg", "malta", "moldova", "monaco", "montenegro", "netherlands",
    "north macedonia", "norway", "poland", "portugal", "romania", "russia",
    "san marino", "serbia", "slovakia", "slovenia", "spain", "sweden",
    "switzerland", "turkey", "ukraine", "united kingdom", "vatican city",
    # Americas
    "argentina", "brazil", "canada", "chile", "colombia", "mexico", "peru",
    "uruguay", "united states", "venezuela",
    # MEA & APAC
    "australia", "china", "india", "indonesia", "iran", "iraq", "israel",
    "japan", "jordan", "lebanon", "malaysia", "new zealand", "pakistan",
    "philippines", "qatar", "saudi arabia", "singapore", "south africa",
    "south korea", "thailand", "uae", "united arab emirates", "vietnam",
}


# Split tokens on /, -, ,, &, " and ", or whitespace
_SPLIT_RE = re.compile(r"\s*[/&,\-]\s*|\s+and\s+", re.IGNORECASE)


def parse_territory(territory: str) -> set[str]:
    """Return a set of canonical lowercase country names implied by the territory cell.

    Empty set means 'no constraint' (we won't apply country boost/demote).
    """
    if not territory:
        return set()
    countries: set[str] = set()

    # Strip parenthetical notes ('Italy/Greece (football, basketball)' →
    # 'Italy/Greece')
    s = re.sub(r"\([^)]*\)", "", territory).strip()
    if not s:
        return set()

    raw_tokens = _SPLIT_RE.split(s)
    for tok in raw_tokens:
        t = tok.strip().lower()
        if not t:
            continue
        # Region shorthand
        if t in REGION_TO_COUNTRIES:
            countries.update(REGION_TO_COUNTRIES[t])
            continue
        # Country alias
        if t in COUNTRY_ALIASES:
            countries.add(COUNTRY_ALIASES[t])
            continue
        # Direct country name — only accept if in the whitelist (so sport names
        # like 'Ice Hockey' or 'football' get silently dropped)
        if t in COUNTRY_WHITELIST:
            countries.add(t)
    return countries


def normalise_country(country: str) -> str:
    """Lowercase + alias-resolve a single country string.

    Returns "" if the input isn't a recognised country — that way candidate
    records with junk values (like "United states" with weird casing or
    placeholders) don't trigger spurious country conflicts.
    """
    s = (country or "").strip().lower()
    if not s:
        return ""
    resolved = COUNTRY_ALIASES.get(s, s)
    if resolved in COUNTRY_WHITELIST:
        return resolved
    return ""


def country_signal(territory_countries: set[str], candidate_country: str) -> int:
    """Compute a country-match signal.

    Returns:
      +20 if the candidate's country is one of the territory's countries
      -40 if territory has countries AND candidate has a country AND they conflict
        0 otherwise (one or both sides lack country info → don't penalize)
    """
    cand = normalise_country(candidate_country)
    if not territory_countries:
        return 0    # no territory constraint
    if not cand:
        return 0    # candidate doesn't know its country
    if cand in territory_countries:
        return 20
    return -40
