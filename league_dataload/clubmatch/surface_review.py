"""Re-surface a config's engine outputs into the REVIEW/<country>/football/
per-tier matched/missing CSVs, in place.

Regenerates the CONTENT of the existing REVIEW files from the latest
crosscheck.csv / missing_from_crm.csv, preserving their established filenames
(NN_mens_tierN_<league_slug>__{matched,missing}_in_hubspot.csv). Appends the
trailing `Associated_Teams` column (kept blank, matching the rollout contract).

Tier is parsed from each existing filename, so this works for any country
without re-deriving league slugs, and correctly reflects clubs that moved
between matched/missing after a re-match (e.g. once domains were resolved).

CLI:
  python3 -m clubsports.engine.surface_review --config soccer_luxembourg_men --country luxembourg
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

BASE = Path(__file__).parent.parent  # clubsports/
REPO = BASE.parent                   # Market-Research/

_MATCHED_COLS = [
    "Club_Name", "League", "Tier", "Level", "Gender", "Country",
    "Sport", "Org_Type", "Territory_Owner", "Truth_Domain", "LinkedIn",
    "Match_Status", "Match_Type", "Confidence", "Match_Rank",
    "CRM_Name", "CRM_Record_ID", "CRM_Owner", "CRM_Domain", "CRM_Country",
    "Active_Accounts", "Salesforce_ID", "Is_Duplicate", "Duplicate_Note",
    "LLM_Verified", "Verification_Method", "Associated_Teams",
]
_MISSING_COLS = [
    "Club_Name", "League", "Tier", "Level", "Gender", "Country", "Country_ISO2",
    "Sport", "Org_Type", "Territory_Owner", "Domain", "LinkedIn",
    "Season", "Notes", "Associated_Teams",
]


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write(path: Path, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r.setdefault("Associated_Teams", "")
            w.writerow(r)


_GENDER_BASE = {"mens": 0, "womens": 10, "youth": 20}


def _gender_of(config_id: str) -> str:
    low = config_id.lower()
    if "women" in low or "femin" in low or "ladies" in low:
        return "womens"
    if "youth" in low or "junior" in low or "primavera" in low or "u19" in low:
        return "youth"
    return "mens"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def _tier_rows(src: list[dict], tier: str, matched: bool) -> list[dict]:
    rows = [r for r in src if str(r.get("Tier", "")).strip() == tier]
    if matched:
        # crosscheck.csv holds ALL truth clubs (matched + "Not Found"); the
        # matched file keeps only rows with a real CRM link (incl. dup rows).
        rows = [r for r in rows if (r.get("CRM_Record_ID") or "").strip()]
    return rows


def surface(config_id: str, country: str, sport: str = "football") -> None:
    out_dir = BASE / sport / "data" / "output" / config_id
    crosscheck = _read(out_dir / "crosscheck.csv")
    missing = _read(out_dir / "missing_from_crm.csv")
    review = REPO / "REVIEW" / country / sport
    review.mkdir(parents=True, exist_ok=True)
    gender = _gender_of(config_id)
    gtok = f"_{gender}_"

    # Files this config OWNS = existing REVIEW files of THIS gender only.
    existing = [f for f in (sorted(review.glob("*__matched_in_hubspot.csv")) +
                            sorted(review.glob("*__missing_from_hubspot.csv")))
                if gtok in f.name]

    touched = 0
    if existing:
        # Regenerate in place (preserves established filenames/slugs).
        for f in existing:
            m = re.search(r"tier(\d)", f.name)
            if not m:
                continue
            tier = m.group(1)
            matched = f.name.endswith("__matched_in_hubspot.csv")
            rows = _tier_rows(crosscheck if matched else missing, tier, matched)
            _write(f, rows, _MATCHED_COLS if matched else _MISSING_COLS)
            touched += 1
    else:
        # New gender (women's/youth): create per-tier files with gender-distinct
        # naming so they don't collide with the men's files in this folder.
        tiers = sorted({int(r["Tier"]) for r in crosscheck
                        if str(r.get("Tier", "")).strip().isdigit()})
        for t in tiers:
            # league slug = most common League among this tier's rows
            leagues = [r.get("League", "") for r in crosscheck
                       if str(r.get("Tier", "")).strip() == str(t)]
            slug = _slug(max(set(leagues), key=leagues.count)) if leagues else f"tier{t}"
            nn = f"{_GENDER_BASE[gender] + t:02d}"
            base = f"{nn}_{gender}_tier{t}_{slug}"
            _write(review / f"{base}__matched_in_hubspot.csv",
                   _tier_rows(crosscheck, str(t), True), _MATCHED_COLS)
            _write(review / f"{base}__missing_from_hubspot.csv",
                   _tier_rows(missing, str(t), False), _MISSING_COLS)
            touched += 2
    print(f"[surface] {config_id} ({gender}): {'regenerated' if existing else 'created'} "
          f"{touched} files in {review}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="clubsports.engine.surface_review")
    ap.add_argument("--config", required=True)
    ap.add_argument("--country", required=True, help="REVIEW/<country> folder name")
    ap.add_argument("--sport", default="football")
    a = ap.parse_args()
    surface(a.config, a.country, a.sport)


if __name__ == "__main__":
    main()
