"""Unicode-aware text normalisation for club names.

Ported from crm_validators.normalise() with three additions for global clubs:
  1. Unicode NFKD fold + diacritic strip (München → munchen, Atlético → atletico)
  2. Common club-prefix normalisation (FC, AFC, CF, SC, AC, etc. — preserved
     but moved to a stable position so "FC Barcelona" and "Barcelona FC" match)
  3. Configurable replacements / abbreviations passed in from a config so each
     (sport × region) can layer in its own (e.g. "Real" not stripped for Spain).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# ---------------------------------------------------------------------------
# Universal replacements — apply for all configs
# ---------------------------------------------------------------------------

_UNIVERSAL_REPLACEMENTS: list[tuple[str, str]] = [
    (r"^the\b", ""),
    (r"&", "and"),
    (r"-", " "),
    (r"\u2013", " "),  # en dash
    (r"\u2014", " "),  # em dash
    (r"'", ""),
    (r"\u2019", ""),   # right single quote
    (r"\.", ""),
    (r",", ""),
    (r"/", " "),
]

# Legal-entity suffixes appearing in CRM company names but not in truth-source
# club names. Stripped after lowercasing/folding so the regex is ASCII-safe.
# Order matters: longer phrases first so "co ltd" is removed before "ltd".
_LEGAL_SUFFIXES: list[str] = [
    r"\bsociedad\s+anonima\s+deportiva\b",
    r"\bsocieta\s+sportiva\s+dilettantistica\b",
    r"\bfootball\s+and\s+athletic\s+co\s+ltd\b",
    r"\bfootball\s+club\s+limited\b",
    r"\bs\s*p\s*a\b",          # S.p.A.
    r"\bs\s*r\s*l\b",          # S.r.l.
    r"\bs\s*a\s*s\b",          # S.A.S.
    r"\bs\s*a\s*d\b",          # S.A.D.
    r"\bs\s*l\s*u\b",          # S.L.U.
    r"\bsa\b",
    r"\bag\b",
    r"\bgmbh\b",
    r"\be\s*v\b",              # e.V.
    r"\bkg\b",
    r"\bco\s+ltd\b",
    r"\blimited\b",
    r"\bltd\b",
    r"\bcompany\b",
    r"\bclub\b(?=\s+atletico|\s+atlético)",  # only as a leading "Club Atlético" stripped
]
_LEGAL_SUFFIX_PATTERNS = [(p, "") for p in _LEGAL_SUFFIXES]

# Club-name prefix tokens. We do NOT strip them — they carry signal — but we
# canonicalise their position so "FC Bayern München" and "Bayern München FC"
# normalise to the same form.
_CLUB_PREFIXES = {
    "fc", "afc", "cf", "sc", "ac", "as", "rc", "rcd",
    "sd", "ud", "ca", "cd", "sv", "sk", "if", "tj",
    "fk", "ks", "us", "is", "bk", "ik", "ff", "bsc",
    "vfb", "vfl", "tsv", "tsg", "1899", "1860",
}


def strip_invisible(name: str) -> str:
    """Remove zero-width / BOM / Unicode format (Cf) characters.

    CRM exports often carry a trailing U+200B (e.g. "NK Rudeš​") which survives
    NFKD folding and silently breaks alias/normalised matching. Dropping the
    whole Cf category covers ZWSP/ZWNJ/ZWJ/BOM (U+200B/C/D, U+FEFF) plus soft
    hyphen and the like.
    """
    return "".join(ch for ch in name if unicodedata.category(ch) != "Cf")


def fold_unicode(name: str) -> str:
    """NFKD-decompose then strip combining marks.

    Bayern München → Bayern Munchen
    Atlético       → Atletico
    Mönchengladbach → Monchengladbach
    """
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalise(
    name: str,
    extra_replacements: Iterable[tuple[str, str]] = (),
    fold: bool = True,
) -> str:
    """Lowercase, strip punctuation/filler, optionally fold diacritics.

    Args:
        name: raw entity name.
        extra_replacements: per-config (pattern, replacement) tuples applied
            after the universal set (e.g. {"R\\.M\\.": "real madrid"}).
        fold: if True (default for clubs), strip diacritics. Disable for
            scripts where folding loses information (CJK).
    """
    if not name:
        return ""
    s = strip_invisible(name)
    if fold:
        s = fold_unicode(s)
    s = s.lower()
    for pattern, replacement in _UNIVERSAL_REPLACEMENTS:
        s = re.sub(pattern, replacement, s)
    for pattern, replacement in _LEGAL_SUFFIX_PATTERNS:
        s = re.sub(pattern, replacement, s)
    for pattern, replacement in extra_replacements:
        s = re.sub(pattern, replacement, s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def canonicalise_prefix_position(normalised: str) -> str:
    """Move recognised club prefixes to the front in alphabetical-token order.

    "bayern munchen fc"  → "fc bayern munchen"
    "fc bayern munchen"  → "fc bayern munchen"  (unchanged)
    "real madrid cf"     → "cf real madrid"

    Operates on already-normalised (lowercase, ASCII-folded) input.
    Used as an additional matching key, not a replacement for normalise().
    """
    tokens = normalised.split()
    if not tokens:
        return normalised
    prefixes = [t for t in tokens if t in _CLUB_PREFIXES]
    body = [t for t in tokens if t not in _CLUB_PREFIXES]
    if not prefixes:
        return normalised
    return " ".join(prefixes + body)


def is_close_match(a: str, b: str) -> bool:
    """Edit-distance match for minor typos (Levenshtein <= 2 for long names).

    Lifted unchanged from crm_validators.is_close_match — it's already generic.
    """
    if a == b:
        return True
    if len(a) < 8 or len(b) < 8:
        return False
    if abs(len(a) - len(b)) > 2:
        return False
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for j in range(1, len(b) + 1):
        curr = [j] + [0] * len(a)
        for i in range(1, len(a) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[i] = min(curr[i - 1] + 1, prev[i] + 1, prev[i - 1] + cost)
        prev = curr
    threshold = 2 if len(a) >= 12 else 1
    return prev[len(a)] <= threshold
