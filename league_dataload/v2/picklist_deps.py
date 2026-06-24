"""Dependent-picklist mapping export (Salesforce field dependencies).

Salesforce encodes a field dependency as a base64 ``validFor`` bitmap on each
dependent picklist value: bit *i* (MSB-first) is set when that value is valid for
the *i*-th **active** controlling value. This module reads the live describe via
the ``sf`` CLI, decodes those bitmaps, and writes the
``controller -> [valid dependents]`` mapping to JSON -- so the sheet generator can
build dependent dropdowns from the org's own config instead of a hand-maintained
list that silently drifts (see CLAUDE.md / gen_sheet POSITIONS).

Default pair -- the one the order sheet needs:

==========  ===================================
object      OpportunityLineItem (Opportunity Product)
controller  ``Sport__c``
dependent   ``Position_of_Field__c``
==========  ===================================

Refresh (writes ``data/sport_position_deps.json``)::

    python3 -m league_dataload.v2 picklist-deps
"""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

DEFAULT_PATH = "data/sport_position_deps.json"
DEFAULT_OBJECT = "OpportunityLineItem"
DEFAULT_CONTROLLER = "Sport__c"
DEFAULT_DEPENDENT = "Position_of_Field__c"


def _describe(sobject: str, org: str) -> dict:
    p = subprocess.run(["sf", "sobject", "describe", "--sobject", sobject,
                        "--target-org", org, "--json"],
                       capture_output=True, text=True, timeout=180)
    out = json.loads(p.stdout or "{}")
    if p.returncode != 0:
        raise SystemExit(f"SF describe failed: {out.get('message') or p.stderr}")
    return out["result"]


def _active_values(field: dict) -> list[str]:
    return [v["value"] for v in field.get("picklistValues", []) if v.get("active", True)]


def _valid_for(b64: str, n_controls: int) -> set[int]:
    """Decode one validFor bitmap to the set of controlling indexes it enables."""
    if not b64:
        return set()
    raw = base64.b64decode(b64)
    idxs = set()
    for i in range(n_controls):
        byte = raw[i >> 3] if (i >> 3) < len(raw) else 0
        if byte & (0x80 >> (i & 7)):
            idxs.add(i)
    return idxs


def decode_dependency(describe: dict, controller: str, dependent: str) -> dict:
    """Build the controller -> [valid dependent values] mapping from a describe."""
    fields = {f["name"]: f for f in describe["fields"]}
    ctrl, dep = fields.get(controller), fields.get(dependent)
    if not ctrl or not dep:
        raise SystemExit(f"{controller!r} or {dependent!r} not found on {describe['name']}")
    if not dep.get("dependentPicklist"):
        raise SystemExit(f"{dependent!r} is not a dependent picklist")
    if dep.get("controllerName") != controller:
        raise SystemExit(f"{dependent!r} is controlled by {dep.get('controllerName')!r}, "
                         f"not {controller!r}")
    controls = _active_values(ctrl)
    mapping: dict[str, list[str]] = {c: [] for c in controls}
    for v in dep.get("picklistValues", []):
        if not v.get("active", True):
            continue
        for i in _valid_for(v.get("validFor"), len(controls)):
            mapping[controls[i]].append(v["value"])
    return {
        "object": describe["name"],
        "controlling_field": controller,
        "dependent_field": dependent,
        "controlling_values": controls,        # order matters: bitmap index
        "dependent_values": _active_values(dep),
        "dependencies": mapping,
    }


def refresh(*, org: str, out_path: str = DEFAULT_PATH, sobject: str = DEFAULT_OBJECT,
            controller: str = DEFAULT_CONTROLLER, dependent: str = DEFAULT_DEPENDENT) -> dict:
    """Live-describe the object, decode the dependency, and write it to ``out_path``."""
    data = decode_dependency(_describe(sobject, org), controller, dependent)
    data["source_org"] = org
    Path(out_path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    return data


def load(path: str = DEFAULT_PATH) -> dict:
    """Read a previously refreshed mapping JSON."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
