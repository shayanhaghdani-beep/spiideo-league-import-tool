"""Paths, defaults, and a tiny zero-dependency .env loader.

The app is intentionally dependency-free (stdlib only), so this replaces
python-dotenv with a minimal KEY=VALUE parser. Call ``load_env()`` once at CLI
start; values already in the real environment win over the .env file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# App root = league_deals_dataload/ (one above this package).
APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "data"
INPUTS_DIR = APP_ROOT / "inputs"
OUTPUTS_DIR = APP_ROOT / "outputs"


# ---------------------------------------------------------------------------
# .env

def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env into os.environ (without overwriting)."""
    env_path = path or (APP_ROOT / ".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# Lookup-data file paths (override any via .env)

def sf_accounts_csv() -> Path:
    return Path(_env("SF_ACCOUNTS_CSV", str(DATA_DIR / "sf_accounts.csv")))


def users_csv() -> Path:
    return Path(_env("USERS_CSV", str(DATA_DIR / "users.csv")))


def hubspot_leagues_csv() -> Path:
    return Path(_env("HUBSPOT_LEAGUES_CSV", str(DATA_DIR / "hubspot_leagues.csv")))


def hubspot_company_ids_csv() -> Path:
    return Path(_env("HUBSPOT_COMPANY_IDS_CSV", str(DATA_DIR / "hubspot_company_ids.csv")))


def league_deal_aliases_csv() -> Path:
    return Path(_env("LEAGUE_DEAL_ALIASES_CSV", str(DATA_DIR / "league_deal_aliases.csv")))


def manual_account_ids_csv() -> Path:
    return Path(_env("MANUAL_ACCOUNT_IDS_CSV", str(DATA_DIR / "manual_account_ids.csv")))


def pricebook_csv() -> Path:
    return Path(_env("PRICEBOOK_CSV", str(DATA_DIR / "pricebook.csv")))


def sf_target_org() -> str:
    return _env("SF_TARGET_ORG", "spiideo")


def league_opp_currency() -> str:
    return _env("LEAGUE_OPP_CURRENCY", "EUR")


# ---------------------------------------------------------------------------
# Emit defaults (faithful to the engine's league flow)

@dataclass(frozen=True)
class AccountDefaults:
    """Account-sheet constants. League accounts are always EUR (set in emit)."""
    invoice_delivery_method: str = "Email"
    payment_terms: str = "30"
    account_currency: str = "EUR"


@dataclass(frozen=True)
class LeagueOppDefaults:
    """Opportunity-sheet constants for league forecasts (pipeline opps)."""
    stage: str = "Discover Challenges"
    forecast_category: str = "Pipeline"
    opp_currency: str = "EUR"
    opp_type: str = "New"          # overridden per-row from Deal Type
    order_type: str = "New order"
    billing_period: str = "Annual"
    notice_period_months: str = "1"
    camera_shipping_schedule: str = ""   # not applicable to league deals

    @classmethod
    def from_env(cls) -> "LeagueOppDefaults":
        return cls(opp_currency=league_opp_currency())


# ---------------------------------------------------------------------------
# Opp Product (OLI) line constants — used by classify_product / emit_opp_product.

DEFAULT_BILLING_PERIOD = "Annual"
DEFAULT_PRICE_PERIOD = "Annual"
SUBSCRIPTION_CHARGE_TYPE = "Recurring"
SUBSCRIPTION_UOM = "account"
CAMERA_CHARGE_TYPE = "One-off"
CAMERA_UOM = "camera system/s"
CAMERA_ORDER_TYPE = "Shipment - No additional cost"
CAMERA_DEFAULT_HEIGHT = 10
CAMERA_DEFAULT_DISTANCE = 10
