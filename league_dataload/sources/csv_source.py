"""Local-CSV backend for LookupSource.

Reads existing-SF Accounts and Users from CSV exports you drop into ``data/``
(Salesforce report exports, Xappex dumps, or ``sf data query --result-format
csv``). Headers are mapped to standard SF field names via ``base.map_row``, so
any common export header works (see DEFAULT_*_MAP for accepted variants, or
override per-field in .env).

A missing file is treated as "no rows" rather than an error — the importer can
still run a HubSpot-only crosscheck (the curated ``hubspot_leagues.csv`` already
carries SF Account IDs), and rep resolution simply reports unmatched reps.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .base import AccountRow, UserRow, get_map, map_row


class CsvLookupSource:
    """LookupSource backed by local CSV exports (accounts + users)."""

    def __init__(self, accounts_csv: Path | None, users_csv: Path | None):
        self._accounts_csv = accounts_csv
        self._users_csv = users_csv

    def fetch_accounts(self) -> list[AccountRow]:
        return self._read(self._accounts_csv, "accounts")

    def fetch_users(self) -> list[UserRow]:
        return self._read(self._users_csv, "users")

    @staticmethod
    def _read(path: Path | None, table: str) -> list[dict]:
        if path is None or not Path(path).exists():
            return []
        col_map = get_map(table)
        out: list[dict] = []
        with Path(path).open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                out.append(map_row(raw, col_map))
        return out
