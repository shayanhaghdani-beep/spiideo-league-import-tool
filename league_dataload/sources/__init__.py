"""Lookup-source backends for the league-deals importer.

Choose at runtime via ``--source``:
  - csv  → CsvLookupSource     (local report exports in data/)
  - sf   → SfCliLookupSource   (live `sf data query`)
  - none → StubLookupSource    (skip crosscheck + rep resolution)
"""
from .base import (
    AccountRow,
    LookupSource,
    StubLookupSource,
    UserRow,
    get_map,
    map_row,
)
from .csv_source import CsvLookupSource
from .sf_cli_source import SfCliError, SfCliLookupSource

__all__ = [
    "AccountRow",
    "UserRow",
    "LookupSource",
    "StubLookupSource",
    "CsvLookupSource",
    "SfCliLookupSource",
    "SfCliError",
    "get_map",
    "map_row",
]
