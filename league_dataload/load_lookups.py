"""In-memory joins over LookupSource data (the subset the importer needs)."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .sources import LookupSource


def build_users_by_name(source: LookupSource) -> dict[str, list[dict[str, Any]]]:
    """{user_name: [user_row, ...]}. Filters out inactive users unless IsActive is blank."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source.fetch_users():
        name = (row.get("Name") or "").strip()
        if not name:
            continue
        active = (row.get("IsActive") or "").strip().lower()
        # Treat blank / "true" / "1" as active; only "false" / "0" filtered
        if active in ("false", "0", "no"):
            continue
        out[name].append(row)
    return out
