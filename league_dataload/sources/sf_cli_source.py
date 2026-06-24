"""Live ``sf`` CLI backend for LookupSource.

Queries the Salesforce org directly via the Salesforce CLI (``sf data query
--json``) instead of a static export — useful when you want the crosscheck run
against *current* SF data. Requires the ``sf`` CLI authenticated to the target
org (default alias ``spiideo``; override with --target-org or SF_TARGET_ORG).

Only league-ish Accounts are pulled (Org_Type or Name keyword filter) to keep
the result set small; ``candidates.fetch_existing_leagues`` re-applies the exact
same Python filter afterwards, so this backend returns an identical candidate
pool to the CSV backend given the same underlying data.

Note: in a sandboxed shell the CLI's network call is blocked — run the importer
with the sandbox disabled (this project's reference notes cover that).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from .base import AccountRow, UserRow


# The live Spiideo org exposes "Org Type" as Org_Type_for_Calc__c (the engine's
# Google-Sheet export labelled the column Org_Type__c, which is NOT the API
# name). Make it configurable; whatever it is, we map the queried value back to
# the standard ``Org_Type__c`` key the matcher reads. Verify on a new org with
# `sf sobject describe --sobject Account`.
def _orgtype_field() -> str:
    return os.environ.get("SF_ACCOUNT_ORGTYPE_FIELD", "Org_Type_for_Calc__c")


def _accounts_soql() -> str:
    """League-candidate superset. Mirrors the keyword set used by
    candidates.fetch_existing_leagues; the Python filter re-applies afterwards,
    so this returns an identical pool to the CSV path."""
    ot = _orgtype_field()
    return (
        f"SELECT Id, Name, {ot}, Sport__c, BillingCountry FROM Account WHERE "
        f"{ot} LIKE '%League%' OR {ot} LIKE '%Conference%' OR "
        f"{ot} LIKE '%Federation%' OR {ot} LIKE '%Association%' OR "
        "Name LIKE '%League%' OR Name LIKE '%Conference%' OR "
        "Name LIKE '%Federation%' OR Name LIKE '%Association%' OR "
        "Name LIKE '%Liga%' OR Name LIKE '%Serie%'"
    )


_USERS_SOQL = "SELECT Id, Name, IsActive FROM User WHERE IsActive = true"


class SfCliError(RuntimeError):
    """Raised when the ``sf`` CLI is missing or a query fails."""


class SfCliLookupSource:
    """LookupSource backed by live ``sf data query`` calls."""

    def __init__(self, target_org: str = "spiideo", sf_bin: str = "sf",
                 timeout: int = 180):
        self._org = target_org
        self._sf = sf_bin
        self._timeout = timeout
        self._cache: dict[str, list[dict]] = {}

    def fetch_accounts(self) -> list[AccountRow]:
        return self._query("accounts", _accounts_soql())

    def fetch_users(self) -> list[UserRow]:
        return self._query("users", _USERS_SOQL)

    # ------------------------------------------------------------------ internal

    def _query(self, cache_key: str, soql: str) -> list[dict]:
        if cache_key in self._cache:
            return self._cache[cache_key]
        if shutil.which(self._sf) is None:
            raise SfCliError(
                f"'{self._sf}' CLI not found on PATH. Install the Salesforce CLI "
                f"or use --source csv."
            )
        cmd = [
            self._sf, "data", "query",
            "--query", soql,
            "--target-org", self._org,
            "--json",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as e:  # pragma: no cover - env dependent
            raise SfCliError(f"sf query timed out after {self._timeout}s: {soql[:60]}…") from e

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise SfCliError(
                f"sf query failed (org={self._org!r}, exit {proc.returncode}). "
                f"Is the org authenticated (`sf org list`)?\n{detail[:500]}"
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:  # pragma: no cover
            raise SfCliError(f"Could not parse sf JSON output: {e}") from e

        records = (payload.get("result") or {}).get("records") or []
        rows = [self._clean(r) for r in records]
        self._cache[cache_key] = rows
        return rows

    @staticmethod
    def _clean(record: dict) -> dict:
        """Drop SObject ``attributes``, stringify values (None → ""), and map the
        org-specific org-type field back to the standard ``Org_Type__c`` key the
        matcher reads."""
        orgtype_field = _orgtype_field()
        out: dict[str, str] = {}
        for k, v in record.items():
            if k == "attributes":
                continue
            key = "Org_Type__c" if k == orgtype_field else k
            out[key] = "" if v is None else str(v)
        return out
