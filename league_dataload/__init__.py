"""league_dataload — focused Salesforce DataLoad importer for big league deals.

Ports the proven crosscheck + emit logic from the Spiideo DataLoad Engine's
league flow into a self-contained, dependency-free package with a pluggable
lookup layer (local CSV exports or live `sf` CLI).
"""

__version__ = "0.1.0"
