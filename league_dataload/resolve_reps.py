"""Resolve Rep Name → SF User Id (from the LookupSource Users data).

Tolerates inconsistencies between the rep's nickname/short-form in the deal
sheet (e.g. 'Freddy', 'Brana', 'Morgan') and their canonical SF Full Name.
Resolution order:

  1. Exact case-sensitive match on Full Name
  2. Case-insensitive match
  3. First-name-only match (rep typed 'Freddy' → SF has 'Freddy Smith')
  4. Substring match either direction (rep 'Freddy' contained in 'Frederick X.')

Manual aliases can be set via REP_NAME_ALIASES env var:
    REP_NAME_ALIASES="Freddy=Federico Caini, Brana=Branislav Lazic"
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .load_lookups import build_users_by_name
from .sources import LookupSource
from .schema import ForecastRow


@dataclass
class RepResolutionReport:
    reps_matched: int = 0
    reps_unmatched: int = 0
    reps_ambiguous: int = 0
    rep_map: dict[str, str] = field(default_factory=dict)   # rep_name → User Id
    rep_resolved_name: dict[str, str] = field(default_factory=dict)  # rep_name → SF Full Name
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"  Reps: {self.reps_matched} matched, "
            f"{self.reps_ambiguous} ambiguous, "
            f"{self.reps_unmatched} unmatched"
        )


def _load_aliases() -> dict[str, str]:
    raw = os.environ.get("REP_NAME_ALIASES", "").strip()
    if not raw:
        return {}
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _find_match(rep_name: str, users_by_name: dict[str, list[dict]]) -> list[dict]:
    """Multi-pass user lookup. Returns 0, 1, or multiple matches."""
    # Pass 1: exact
    matches = users_by_name.get(rep_name, [])
    if matches:
        return matches
    lower = rep_name.lower()
    # Pass 2: case-insensitive exact
    matches = [
        rec for n, recs in users_by_name.items()
        if n.lower() == lower for rec in recs
    ]
    if matches:
        return matches
    # Pass 3: first-name-only ('Freddy' matches 'Freddy Smith')
    matches = [
        rec for n, recs in users_by_name.items()
        if n.lower().startswith(lower + " ") for rec in recs
    ]
    if matches:
        return matches
    # Pass 4: substring either direction (last resort — can be noisy)
    matches = [
        rec for n, recs in users_by_name.items()
        if (lower in n.lower() or n.lower() in lower) and len(lower) >= 4
        for rec in recs
    ]
    return matches


def resolve_reps(rows: list[ForecastRow], source: LookupSource) -> RepResolutionReport:
    report = RepResolutionReport()
    users_by_name = build_users_by_name(source)
    aliases = _load_aliases()

    unique_reps = sorted({r.rep_name for r in rows if r.rep_name})
    resolved: dict[str, str] = {}
    resolved_names: dict[str, str] = {}
    ambiguous: set[str] = set()

    for name in unique_reps:
        # Manual alias overrides everything
        if name in aliases:
            canonical = aliases[name]
            matches = users_by_name.get(canonical, [])
            if len(matches) == 1:
                resolved[name] = matches[0]["Id"]
                resolved_names[name] = canonical
                continue
            report.warnings.append(
                f"REP_NAME_ALIASES says {name!r} → {canonical!r}, "
                f"but {canonical!r} matches {len(matches)} Users"
            )
            continue

        matches = _find_match(name, users_by_name)
        if len(matches) == 1:
            resolved[name] = matches[0]["Id"]
            resolved_names[name] = matches[0].get("Name", "")
        elif len(matches) > 1:
            ambiguous.add(name)
            names_preview = ", ".join(m.get("Name", "?") for m in matches[:3])
            report.warnings.append(
                f"Rep {name!r} matches {len(matches)} Users ({names_preview}…) — "
                f"add REP_NAME_ALIASES=\"{name}=<exact full name>\" to disambiguate"
            )
        else:
            report.warnings.append(
                f"Rep {name!r} not found in Users — "
                f"add REP_NAME_ALIASES=\"{name}=<full name>\" to map manually"
            )

    report.rep_map = resolved
    report.rep_resolved_name = resolved_names

    for r in rows:
        if r.rep_name in resolved:
            r.resolved_rep_user_id = resolved[r.rep_name]
            report.reps_matched += 1
        elif r.rep_name in ambiguous:
            report.reps_ambiguous += 1
        else:
            report.reps_unmatched += 1
    return report
