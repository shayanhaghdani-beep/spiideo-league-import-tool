"""League name normalization + junk filtering.

Ported from Market-Research/conference_crm_crosscheck.py (Apr 2025), with the
'conference'-specific tokens swapped/extended for league context. All logic is
stdlib-only — no fuzzy libraries.

Reference: conference_crm_crosscheck.py:67-190
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Normalisation


# Number words → digits. We canonicalise WORD→DIGIT (not the reverse) so
# "Big Ten" and "Big 10" / "Big10" all collapse to "big 10", while "Big 12"
# stays "big 12" on both the forecast and CRM sides.
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20",
}


def normalise_league(name: str) -> str:
    """Lowercase, strip suffixes, expand abbreviations.

    Examples:
      "The Big Ten Conference (B1G)" → "big 10 conference"
      "Big10"                        → "big 10"
      "Conf. of Eastern Athletics"   → "league of eastern athletic"
      "Premier League"               → "premier league"
    """
    s = (name or "").strip()
    # Strip parenthetical suffix (acronym or note)
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    s = s.lower()
    s = re.sub(r"^the\s+", "", s)
    s = s.replace("&", "and")
    # Common abbreviations
    s = re.sub(r"\bconf\.\s*$", "conference", s)
    s = re.sub(r"\bconf\b", "conference", s)
    s = re.sub(r"\bconferences\b", "conference", s)
    s = re.sub(r"\bleagues\b", "league", s)
    s = re.sub(r"\bassoc\.\b", "association", s)
    s = re.sub(r"\bassocs\b", "association", s)
    s = re.sub(r"\bath\.\b", "athletic", s)
    s = re.sub(r"\bath\b", "athletic", s)
    s = re.sub(r"\bintercol\.\b", "intercollegiate", s)
    s = re.sub(r"\bdiv\.\b", "division", s)
    s = re.sub(r"\bnatl\.\b", "national", s)
    # Normalise punctuation
    s = re.sub(r"-", " ", s)
    s = re.sub(r"[.,]", " ", s)
    # Split letter↔digit runs so "big10"/"b1g"/"ne10" tokenise consistently
    # on both forecast and CRM sides ("big10" → "big 10", "b1g" → "b 1 g").
    s = re.sub(r"(?<=[a-z])(?=\d)", " ", s)
    s = re.sub(r"(?<=\d)(?=[a-z])", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Canonicalise spelled-out numbers to digits ("ten" → "10")
    if s:
        s = " ".join(_NUMBER_WORDS.get(tok, tok) for tok in s.split())
    return s


# Sport / division tokens that appear appended to a conference name in the
# forecast (e.g. "SoCon Basketball", "RMAC - D2", "NESCAC MSC"). Stripping them
# lets the bare conference name match the CRM account.
_SPORT_STRIP_TOKENS = {
    "football", "basketball", "baseball", "soccer", "volleyball", "hockey",
    "lacrosse", "softball", "gymnastics", "gym", "tennis", "wrestling",
    "rowing", "swimming", "golf", "ice", "field", "multi", "sport",
    "multisport",
    # Common sport abbreviations used in the collegiate plans
    "msc", "wsc", "msoc", "wsoc", "mih", "wih", "fh", "vb", "bb",
    "wbb", "mbb", "wvb", "mvb", "wbk", "mbk",
}


def strip_sport_division(name: str) -> str:
    """Remove trailing/embedded sport + division markers from a league name.

    "SoCon Basketball" → "socon"; "RMAC - D2" → "rmac";
    "Pac 12 Soccer" → "pac 12"; "NESCAC MSC" → "nescac".

    Returns a lowercased, space-collapsed string (NOT fully normalised — feed
    the result to ``normalise_league``). Never returns empty: if every token
    would be stripped, falls back to the lightly-cleaned original.
    """
    s = (name or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[-.,]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    original = s
    # Division markers: D1/D2/D3, DI/DII/DIII, NAIA, NJCAA, JUCO
    s = re.sub(r"\bd\s?(?:1|2|3|iii|ii|i)\b", " ", s)
    s = re.sub(r"\b(?:naia|njcaa|juco)\b", " ", s)
    # Gender qualifiers
    s = re.sub(r"\b(?:men'?s|women'?s|m and w|mens|womens)\b", " ", s)
    # Sport tokens (whole-word)
    toks = [t for t in s.split() if t not in _SPORT_STRIP_TOKENS]
    s = " ".join(toks)
    s = re.sub(r"\s+", " ", s).strip()
    return s or original


def extract_acronym(name: str) -> str | None:
    """Pull a parenthetical acronym from a name: 'Big Ten (B1G)' → 'b1g'."""
    m = re.search(r"\(([^)]+)\)\s*$", name or "")
    if not m:
        return None
    inner = m.group(1).strip()
    if 1 < len(inner) <= 12:
        return inner.lower()
    return None


def significant_words(normalized: str) -> set[str]:
    """Return meaningful tokens for word-overlap comparisons.

    Drops stopwords and overly-short tokens that would otherwise inflate matches.
    """
    stop = {
        "the", "of", "and", "for", "in", "to", "on", "at",
        "conference", "league", "athletic", "association",
        "intercollegiate", "collegiate", "division",
    }
    return {w for w in normalized.split() if len(w) > 2 and w not in stop}


# ---------------------------------------------------------------------------
# Junk detection — non-league CRM entries
#
# A league should never be confused with a sport-specific noise entry like
# "Men's Soccer" or a test/junk record. These patterns mirror those in
# conference_crm_crosscheck.py:137-167 but trimmed to what's relevant for leagues.

_JUNK_PATTERNS = [
    # Sport-line / discipline-only entries (not a real league/account)
    r"\bmen'?s\s+soccer\b",
    r"\bwomen'?s\s+soccer\b",
    r"\bmen'?s\s+lacrosse\b",
    r"\bwomen'?s\s+lacrosse\b",
    r"\bmen'?s\s+hockey\b",
    r"\bwomen'?s\s+hockey\b",
    r"\bmen'?s\s+basketball\b",
    r"\bwomen'?s\s+basketball\b",
    # Test / housekeeping entries
    r"^test\b",
    r"\bmerge\b",
    r"\bdelete\b",
    r"^deal\s+done\b",
    r"^abcx$",
    r"^sport$",
    r"^pobox$",
]
_JUNK_RE = [re.compile(p, re.IGNORECASE) for p in _JUNK_PATTERNS]


def is_junk(name: str) -> tuple[bool, str]:
    """Return (is_junk, reason). Used to filter SF candidates before matching."""
    if not name or not name.strip():
        return True, "empty name"
    for pattern in _JUNK_RE:
        if pattern.search(name):
            return True, f"matches junk pattern: {pattern.pattern}"
    stripped = name.strip()
    if len(stripped) <= 2:
        return True, f"too-short name: {stripped}"
    return False, ""
