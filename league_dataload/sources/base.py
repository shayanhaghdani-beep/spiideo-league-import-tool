"""LookupSource protocol + header mapping for the league-deals importer.

A LookupSource is a read-only view over the *existing* Salesforce data the
crosscheck needs:

  - Accounts — to find league/conference/federation accounts already in SF
  - Users    — to resolve a rep's name to their SF User Id (Opportunity Owner)

Every backend (CSV export, ``sf`` CLI, in-memory stub) returns plain dicts with
**standard SF field names**. Backends map their own column headers to these
standards via ``map_row`` so the rest of the app never cares where data came
from.

This is the trimmed-down sibling of the engine's ``lookups`` package — only the
two object types the focused deal importer actually consumes.
"""
from __future__ import annotations

import os
from typing import Protocol


# Standard field names each backend must emit.
#   Account: Id, Name, Org_Type__c, Sport__c, BillingCountry
#   User:    Id, Name, IsActive, Email
AccountRow = dict
UserRow = dict


class LookupSource(Protocol):
    """Read-only view over the existing-SF data the crosscheck needs."""

    def fetch_accounts(self) -> list[AccountRow]: ...
    def fetch_users(self) -> list[UserRow]: ...


# ---------------------------------------------------------------------------
# Header → standard-field mapping (ported from the engine's column_mapping.py,
# trimmed to accounts + users). First header found in the row wins; matching is
# case-insensitive after stripping whitespace. Override a single header via
# .env, e.g. LOOKUP_ACCOUNTS_ID_HEADER="Salesforce Account ID".

DEFAULT_ACCOUNTS_MAP: dict[str, list[str]] = {
    "Id": ["Id", "Account ID", "AccountID", "Account Id", "Salesforce Account ID", "SF Account ID"],
    "Name": ["Name", "Account Name", "Account: Account Name"],
    "BillingCountry": ["BillingCountry", "Billing Country"],
    "Org_Type__c": ["Org_Type__c", "Org Type", "Account: Org Type"],
    "Sport__c": ["Sport__c", "Sport", "Account: Sport"],
}

DEFAULT_USERS_MAP: dict[str, list[str]] = {
    "Id": ["Id", "User ID", "UserID", "User Id"],
    "Name": ["Name", "Full Name", "User Name", "Rep Name"],
    "Email": ["Email", "User Email"],
    "IsActive": ["IsActive", "Active", "Is Active"],
}

_MAPS = {"accounts": DEFAULT_ACCOUNTS_MAP, "users": DEFAULT_USERS_MAP}


def _apply_env_override(table: str, mapping: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: list(v) for k, v in mapping.items()}
    for field in list(out.keys()):
        env_key = f"LOOKUP_{table.upper()}_{field.upper().rstrip('_')}_HEADER"
        env_key_clean = env_key.replace("__C_", "_").replace("__C", "")
        val = os.environ.get(env_key_clean)
        if val:
            out[field] = [val] + out[field]
    return out


def get_map(table: str) -> dict[str, list[str]]:
    """Return the column map for ``accounts`` or ``users`` with .env overrides."""
    return _apply_env_override(table, _MAPS[table])


def map_row(raw_row: dict, column_map: dict[str, list[str]]) -> dict[str, str]:
    """Pull standard fields out of a raw export row (case-insensitive headers)."""
    lower_index: dict[str, object] = {}
    for k, v in raw_row.items():
        if k is None:
            continue
        lower_index[str(k).strip().lower()] = v

    out: dict[str, str] = {}
    for std_field, candidates in column_map.items():
        value = ""
        for candidate in candidates:
            key = candidate.strip().lower()
            if key in lower_index:
                v = lower_index[key]
                if v is None:
                    continue
                s = str(v).strip()
                if s:
                    value = s
                    break
        out[std_field] = value
    return out


# ---------------------------------------------------------------------------
# In-memory stub (tests + --source none)


class StubLookupSource:
    """Empty/in-memory LookupSource. Used for tests and the --no-lookup path."""

    def __init__(self, accounts: list[AccountRow] | None = None,
                 users: list[UserRow] | None = None):
        self._accounts = accounts or []
        self._users = users or []

    def fetch_accounts(self) -> list[AccountRow]:
        return list(self._accounts)

    def fetch_users(self) -> list[UserRow]:
        return list(self._users)
