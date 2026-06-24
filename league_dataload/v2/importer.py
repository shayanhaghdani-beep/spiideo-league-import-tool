"""Phase B: stage a Plan into Salesforce via the `sf` CLI, confirm, then push.

Order is strict: Account -> Contact -> Opportunity -> OpportunityLineItem, then a
deferred pass for Account contact-role lookups (which point at Contacts created
after their Account). Each parent's freshly-created Id is captured and threaded
into its children (staged create-and-capture, no external-ID fields).

SAFETY:
- ``dry_run=True`` is the default -- it resolves + prints the full change list and
  pushes NOTHING.
- A live push requires ``dry_run=False`` AND an explicit ``confirm`` callback that
  returns True after the operator sees the change list.
- Existing Accounts are upserted with a field-level diff (only changed fields).
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from . import mapping as M
from .build_records import Plan, PlannedRecord


# ---------------------------------------------------------------------------
# sf CLI thin wrapper

class SfError(RuntimeError):
    pass


def _sf(args: list[str], target_org: str) -> dict:
    proc = subprocess.run(
        ["sf", *args, "--target-org", target_org, "--json"],
        capture_output=True, text=True, timeout=120)
    # sf prints non-fatal warnings (e.g. "update available") to stderr -- ignore
    # those; the authoritative result/error is the JSON on stdout.
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        out = {}
    if proc.returncode != 0:
        msg = out.get("message") or proc.stdout.strip() or proc.stderr.strip()
        raise SfError(msg)
    return out


def sf_create(sobject: str, fields: dict, target_org: str) -> str:
    """Create one record, return its new Id."""
    values = " ".join(f'{k}="{_esc(v)}"' for k, v in fields.items())
    res = _sf(["data", "create", "record", "--sobject", sobject,
               "--values", values], target_org)
    return res["result"]["id"]


SF_API_VERSION = "v67.0"


def sf_create_contact(fields: dict, target_org: str) -> str:
    """Create a Contact via REST, BYPASSING Salesforce duplicate rules
    (Sforce-Duplicate-Rule-Header: allowSave). We've already deduped on email
    ourselves, so SF's fuzzy surname rule (e.g. Tim O'Donovan vs an unrelated
    Paul O'Donovan) must not block a genuinely new contact."""
    body = json.dumps(fields)
    proc = subprocess.run(
        ["sf", "api", "request", "rest",
         f"/services/data/{SF_API_VERSION}/sobjects/Contact",
         "--method", "POST", "--body", "-",
         "--header", "Sforce-Duplicate-Rule-Header:allowSave=true",
         "--target-org", target_org],
        input=body, capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        out = {}
    if isinstance(out, list):
        out = out[0] if out else {}
    if proc.returncode != 0 or not out.get("success"):
        detail = out.get("message") or (out.get("errors") and str(out["errors"])) \
            or proc.stderr.strip() or proc.stdout.strip()
        raise SfError(f"contact create failed: {detail}")
    return out["id"]


def sf_update(sobject: str, record_id: str, fields: dict, target_org: str) -> None:
    values = " ".join(f'{k}="{_esc(v)}"' for k, v in fields.items())
    _sf(["data", "update", "record", "--sobject", sobject,
         "--record-id", record_id, "--values", values], target_org)


def sf_get_fields(sobject: str, record_id: str, fields: list[str], target_org: str) -> dict:
    cols = ", ".join(fields)
    res = _sf(["data", "query", "--query",
               f"SELECT {cols} FROM {sobject} WHERE Id = '{record_id}'"], target_org)
    recs = res["result"]["records"]
    return recs[0] if recs else {}


def _esc(v) -> str:
    return str(v).replace('"', '\\"')


# ---------------------------------------------------------------------------
# Change-set summary + confirm gate

@dataclass
class ChangeSet:
    creates: list[tuple[str, str]] = field(default_factory=list)   # (sobject, label)
    updates: list[tuple[str, str, dict]] = field(default_factory=list)  # (sobject, label, diff)
    reuses: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = ["", "=" * 64, "CHANGE LIST (review before pushing)", "=" * 64]
        from collections import Counter
        c = Counter(s for s, _ in self.creates)
        lines.append(f"CREATE: {sum(c.values())} records  " +
                     " ".join(f"{k}={v}" for k, v in c.items()))
        for s, lbl in self.creates[:200]:
            lines.append(f"  + {s:20s} {lbl}")
        if self.updates:
            lines.append(f"\nUPDATE: {len(self.updates)} records (changed fields only)")
            for s, lbl, diff in self.updates[:200]:
                d = ", ".join(f"{k}: {a!r}->{b!r}" for k, (a, b) in diff.items()) or "(no change)"
                lines.append(f"  ~ {s:20s} {lbl}: {d}")
        if self.reuses:
            lines.append(f"\nREUSE (no write): {len(self.reuses)}")
            for s, lbl in self.reuses[:50]:
                lines.append(f"  = {s:20s} {lbl}")
        if self.warnings:
            lines.append(f"\n⚠️  WARNINGS: {len(self.warnings)}")
            for w in self.warnings:
                lines.append(f"  ! {w}")
        lines.append("=" * 64)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Contact whole-CRM email dedup

def resolve_contact(email: str, target_account_id: str, target_org: str) -> tuple[str, str]:
    """Whole-CRM dedup by EMAIL (Shayan's rule). Returns (action, contact_id):
      no email / email not found -> ('create', '')   genuinely new -> create (dup rule bypassed)
      email on the SAME account  -> ('reuse', id)
      email but contact has NO account -> ('adopt', id)   write our account onto it
      email on a DIFFERENT account -> ('flag_other', id)  don't touch; flag for review
    Email is the only reliable dup key; SF's fuzzy surname rule is bypassed at
    create time, so a same-surname different-person isn't treated as a dup here."""
    if not email:
        return "create", ""
    res = _sf(["data", "query", "--query",
               f"SELECT Id, AccountId FROM Contact WHERE Email = '{_esc(email)}'"],
              target_org)
    recs = res["result"]["records"]
    if not recs:
        return "create", ""
    same = [r for r in recs if r.get("AccountId") == target_account_id]
    if same:
        return "reuse", same[0]["Id"]
    noacct = [r for r in recs if not r.get("AccountId")]
    if noacct:
        return "adopt", noacct[0]["Id"]
    return "flag_other", recs[0]["Id"]


# ---------------------------------------------------------------------------
# Cross-check / verification helpers (pure -- the SF I/O lives on Importer)

def _oli_counts_by_opp_ref(plan: Plan) -> dict:
    """opp ref-key -> how many OLI records the plan will attach to it."""
    counts: dict[str, int] = {}
    oid = M.OLI_FIELDS["opportunity_id"]
    for r in plan.by_object(M.SOBJECT["oli"]):
        ref = r.parents.get(oid, "")
        counts[ref] = counts.get(ref, 0) + 1
    return counts


def opp_preflight_warnings(attach_opps: list, existing: dict, run_currency: str) -> list:
    """Pure cross-check of attach-mode opps against their live SF state. Catches the
    things we used to eyeball by hand (Shayan, 2026-06-23):
      - the pinned opp doesn't exist (bad Opportunity SF ID),
      - it ALREADY has line items -> re-running double-loads OLIs,
      - its currency differs from the run currency (the attach update will switch it),
      - it's already closed.
    `attach_opps`: [{id, label, planned_olis}]; `existing`: id -> {stage, isclosed,
    currency, oli_count} (missing key = not found)."""
    out: list[str] = []
    for o in attach_opps:
        info = existing.get(o["id"])
        if info is None:
            out.append(f"{o['label']}: opportunity {o['id']} NOT FOUND in SF — check the Opportunity SF ID")
            continue
        if info.get("oli_count", 0) > 0:
            out.append(f"{o['label']}: target opp ALREADY has {info['oli_count']} line item(s); "
                       f"this run ADDS {o['planned_olis']} more → DOUBLE-LOAD risk — skip this club "
                       f"if it was already imported")
        cur = info.get("currency")
        if run_currency and cur and cur != run_currency:
            out.append(f"{o['label']}: opp currency {cur} ≠ run currency {run_currency} → "
                       f"the attach update will switch the opp to {run_currency}")
        if info.get("isclosed"):
            out.append(f"{o['label']}: opp is already CLOSED ({info.get('stage')})")
    return out


def contact_preflight_warnings(contacts: list, existing_by_email: dict) -> list:
    """Surface, BEFORE the push, the 'Skara contact' situation: a row's contact email
    already lives on a DIFFERENT account than the one we pinned -> the importer will FLAG
    it (not auto-reparent), so the opp/account would be left without that contact unless
    you reparent manually. `contacts`: [{email, label, pinned_acct}]; `existing_by_email`:
    lower(email) -> [{id, account_id}] (Shayan, 2026-06-23)."""
    out: list[str] = []
    for c in contacts:
        recs = existing_by_email.get((c.get("email") or "").lower(), [])
        if not recs or not c.get("pinned_acct"):
            continue
        on_pinned = any(r.get("account_id") == c["pinned_acct"] for r in recs)
        other = [r for r in recs if r.get("account_id") and r["account_id"] != c["pinned_acct"]]
        if not on_pinned and other:
            out.append(f"{c['label']}: contact email {c['email']} already on a DIFFERENT account "
                       f"({other[0]['account_id']}, contact {other[0]['id']}) than the pinned "
                       f"account ({c['pinned_acct']}) → will be FLAGGED, not reparented — reparent "
                       f"manually if it's the same club (cf. the Skara duplicate)")
    return out


def duplicate_account_warnings(pinned: list, name_ids: dict) -> list:
    """Warn when a pinned account's Name is shared by other SF accounts → you may have
    pinned the wrong one of a duplicate pair (cf. the two 'Skara FC' accounts). `pinned`:
    [{id, name, label}]; `name_ids`: account Name -> [ids]."""
    out: list[str] = []
    for a in pinned:
        ids = name_ids.get(a.get("name"), [])
        if len(ids) > 1:
            others = ", ".join(i for i in ids if i != a["id"])
            out.append(f"{a['label']}: {len(ids)} accounts share the name {a.get('name')!r} "
                       f"(pinned {a['id']}; also {others}) → verify you pinned the right one "
                       f"(cf. the Skara duplicate)")
    return out


def verify_rows(targets: list, state: dict, run_currency: str) -> tuple:
    """Pure post-push check: each touched opp's live state vs what the plan intended.
    `targets`: [{id, label, expected_olis}]; `state`: id -> {name, stage, currency,
    amount, oli_count}. Returns (rows_for_display, problems)."""
    rows, problems = [], []
    for t in targets:
        s = state.get(t["id"])
        if s is None:
            problems.append(f"{t['label']}: opp {t['id']} not found post-push")
            continue
        actual, expected = s.get("oli_count", 0), t["expected_olis"]
        flag = "✓"
        if actual < expected:
            flag = "✗"
            problems.append(f"{t['label']}: only {actual}/{expected} line items attached")
        elif run_currency and s.get("currency") and s["currency"] != run_currency:
            flag = "✗"
            problems.append(f"{t['label']}: currency {s['currency']} ≠ {run_currency}")
        rows.append((s.get("name") or t["label"], s.get("stage"), s.get("currency"),
                     s.get("amount"), actual, expected, flag))
    return rows, problems


# ---------------------------------------------------------------------------
# Staged executor

class Importer:
    def __init__(self, target_org: str = "spiideo", dry_run: bool = True):
        self.org = target_org
        self.dry_run = dry_run
        self.ref_to_id: dict[str, str] = {}     # temp ref_key -> real SF Id
        self.skipped: set[str] = set()           # ref_keys not created (dup/error)
        self.failures: list[tuple] = []          # (sobject, label, reason) to report
        self._fake = 0

    def _resolve(self, value: str) -> str:
        """A parent value is either a literal Id or a temp ref_key."""
        return self.ref_to_id.get(value, value)

    def _resolved_fields(self, rec: PlannedRecord) -> dict:
        out = dict(rec.fields)
        for api, ref in rec.parents.items():
            out[api] = self._resolve(ref)
        return out

    def _new_fake_id(self, sobject: str) -> str:
        self._fake += 1
        return f"<{sobject[:3].upper()}_NEW_{self._fake}>"

    def build_changeset(self, plan: Plan, confirm_reparent=None) -> ChangeSet:
        """Resolve the plan into a change list WITHOUT writing (used for the
        confirm gate and dry-run). For existing accounts, diffs against live SF."""
        cs = ChangeSet(warnings=list(plan.warnings))
        for rec in plan.records:
            if rec.operation == "create":
                cs.creates.append((rec.sobject, rec.label))
            elif rec.operation == "upsert" and rec.sf_id:
                # Show parent lookups (resolved to real Ids at apply time) in the preview.
                disp = dict(rec.fields)
                for api, ref in rec.parents.items():
                    disp[api] = ref
                diff = self._diff_existing(rec, disp)
                if diff:
                    cs.updates.append((rec.sobject, rec.label, diff))
                else:
                    cs.reuses.append((rec.sobject, rec.label))
            else:
                cs.creates.append((rec.sobject, rec.label))
        return cs

    def _diff_existing(self, rec: PlannedRecord, fields: dict | None = None) -> dict:
        """Field-level diff of desired vs current SF values (changed only). The
        Name is never diffed -- matched records keep their CRM name (Shayan, 2026-06-23).
        `fields` lets callers pass parent-resolved values (e.g. an opp's contact lookups)."""
        desired = rec.fields if fields is None else fields
        name_key = M.ACCOUNT_FIELDS["name"]
        if self.dry_run:
            # Avoid a live read per record in dry-run; mark as potential update.
            return {k: ("<current?>", v) for k, v in desired.items() if k != name_key}
        cols = [k for k in desired if k != name_key]
        current = sf_get_fields(rec.sobject, rec.sf_id, cols, self.org)
        diff = {}
        for k, v in desired.items():
            if k == name_key:
                continue
            cur = current.get(k)
            if str(cur or "") != str(v or ""):
                diff[k] = (cur, v)
        return diff

    def execute(self, plan: Plan, confirm, confirm_reparent=None) -> dict:
        """Resolve, render the change list, confirm, then push (unless dry_run).
        Returns a summary dict. NEVER creates the master opp (it's an input)."""
        cs = self.build_changeset(plan, confirm_reparent)
        # Live read-only cross-check BEFORE the change list (runs in dry-run too): catches a
        # bad pinned Id, a target opp that already has OLIs (double-load), or a currency mismatch.
        try:
            cs.warnings.extend(self.preflight(plan))
        except SfError as e:
            cs.warnings.append(f"preflight cross-check skipped (SF read failed): {e}")
        print(cs.render())
        if self.dry_run:
            print("\n[DRY RUN] nothing was pushed. Re-run with dry_run=False to apply.")
            return {"dry_run": True, "creates": len(cs.creates),
                    "updates": len(cs.updates), "reuses": len(cs.reuses)}
        if not confirm(cs):
            print("\n[ABORTED] no changes pushed.")
            return {"aborted": True}

        # ---- staged push, strict order ----
        applied = {"created": 0, "updated": 0, "reused": 0}
        for sobject in (M.SOBJECT[k] for k in M.IMPORT_ORDER):
            for rec in plan.by_object(sobject):
                self._apply(rec, applied, confirm_reparent)
        # ---- deferred account role-lookup updates ----
        for rec in plan.by_object(M.SOBJECT["account"]):
            if rec.deferred_parents and rec.ref_key not in self.skipped:
                fields = {api: self._resolve(ref) for api, ref in rec.deferred_parents.items()}
                acc_id = self.ref_to_id.get(rec.ref_key, rec.sf_id)
                if acc_id and all(v and ":" not in str(v) for v in fields.values()):
                    try:
                        sf_update(M.SOBJECT["account"], acc_id, fields, self.org)
                        applied["updated"] += 1
                    except SfError as e:
                        self.failures.append((M.SOBJECT["account"], rec.label,
                                              f"role-lookup update failed: {e}"))
        if self.failures:
            print(f"\n⚠️  {len(self.failures)} record(s) need attention "
                  f"(run continued; nothing else blocked):")
            for s, lbl, reason in self.failures:
                print(f"   ! {s:20s} {lbl}: {reason}")
        applied["skipped"] = len(self.skipped)
        # Post-push read-back: confirm every touched opp ended up as planned (stage,
        # currency, line-item count). This is the verification we used to run by hand.
        applied["verified"] = self.verify(plan).get("ok", True)
        return applied

    # -- cross-check / verification (SF I/O; pure analysis lives at module level) ----

    def _query(self, soql: str) -> list:
        return _sf(["data", "query", "--query", soql], self.org)["result"]["records"]

    @staticmethod
    def _oli_count(rec: dict) -> int:
        sub = rec.get("OpportunityLineItems")
        return (sub or {}).get("totalSize", 0) if sub else 0

    def preflight(self, plan: Plan) -> list:
        """Read-only cross-check against live SF before the change list (see
        opp_preflight_warnings). Two batched queries: attach opps + pinned accounts."""
        warnings: list[str] = []
        oli_counts = _oli_counts_by_opp_ref(plan)
        attach = [r for r in plan.by_object(M.SOBJECT["opportunity"])
                  if r.operation == "upsert" and r.sf_id]
        if attach:
            ids = "', '".join(sorted({r.sf_id for r in attach}))
            recs = self._query(
                "SELECT Id, StageName, IsClosed, CurrencyIsoCode, "
                f"(SELECT Id FROM OpportunityLineItems) FROM Opportunity WHERE Id IN ('{ids}')")
            existing = {r["Id"]: {"stage": r.get("StageName"), "isclosed": r.get("IsClosed"),
                                  "currency": r.get("CurrencyIsoCode"),
                                  "oli_count": self._oli_count(r)} for r in recs}
            info = [{"id": r.sf_id, "label": r.label,
                     "planned_olis": oli_counts.get(r.ref_key, 0)} for r in attach]
            warnings += opp_preflight_warnings(info, existing, plan.currency)
        pinned_accts = [r for r in plan.by_object(M.SOBJECT["account"])
                        if r.operation == "upsert" and r.sf_id]
        if pinned_accts:
            ids = "', '".join(sorted({r.sf_id for r in pinned_accts}))
            found = {r["Id"] for r in self._query(f"SELECT Id FROM Account WHERE Id IN ('{ids}')")}
            for r in pinned_accts:
                if r.sf_id not in found:
                    warnings.append(f"{r.label}: account {r.sf_id} NOT FOUND in SF — check Customer SF ID")
            # Duplicate-account-by-name (cf. the two "Skara FC" accounts).
            names = sorted({r.fields.get(M.ACCOUNT_FIELDS["name"], "")
                            for r in pinned_accts if r.fields.get(M.ACCOUNT_FIELDS["name"])})
            if names:
                in_list = "', '".join(n.replace("'", r"\'") for n in names)
                name_ids: dict = {}
                for r in self._query(f"SELECT Id, Name FROM Account WHERE Name IN ('{in_list}')"):
                    name_ids.setdefault(r.get("Name"), []).append(r["Id"])
                pin = [{"id": r.sf_id, "name": r.fields.get(M.ACCOUNT_FIELDS["name"]), "label": r.label}
                       for r in pinned_accts]
                warnings += duplicate_account_warnings(pin, name_ids)
        # Contact email already on a DIFFERENT account than the pinned one (cf. Skara/Ida).
        contacts = [r for r in plan.by_object(M.SOBJECT["contact"])
                    if r.fields.get(M.CONTACT_FIELDS["email"])]
        if contacts:
            emails = sorted({r.fields[M.CONTACT_FIELDS["email"]] for r in contacts})
            in_list = "', '".join(e.replace("'", r"\'") for e in emails)
            by_email: dict = {}
            for r in self._query(f"SELECT Id, Email, AccountId FROM Contact WHERE Email IN ('{in_list}')"):
                by_email.setdefault((r.get("Email") or "").lower(), []).append(
                    {"id": r["Id"], "account_id": r.get("AccountId")})
            pinned_by_ref = {a.ref_key: a.sf_id for a in plan.by_object(M.SOBJECT["account"])}
            cinfo = [{"email": r.fields[M.CONTACT_FIELDS["email"]], "label": r.label,
                      "pinned_acct": pinned_by_ref.get(r.parents.get(M.CONTACT_FIELDS["account_id"]), "")}
                     for r in contacts]
            warnings += contact_preflight_warnings(cinfo, by_email)
        return warnings

    def verify(self, plan: Plan) -> dict:
        """Post-push read-back of every opp this run touched (see verify_rows). Prints a
        table + flags any opp whose live line-item count is short of plan or whose currency
        is wrong. Returns {'ok': bool, 'problems': [...]}."""
        oid = M.OLI_FIELDS["opportunity_id"]
        targets = []
        for r in plan.by_object(M.SOBJECT["opportunity"]):
            rid = r.sf_id or self.ref_to_id.get(r.ref_key, "")
            if not rid or r.ref_key in self.skipped:
                continue
            expected = sum(1 for o in plan.by_object(M.SOBJECT["oli"])
                           if o.parents.get(oid) == r.ref_key and o.ref_key not in self.skipped)
            targets.append({"id": rid, "label": r.label, "expected_olis": expected})
        if not targets:
            return {"ok": True, "problems": []}
        ids = "', '".join(sorted({t["id"] for t in targets}))
        try:
            recs = self._query(
                "SELECT Id, Name, StageName, CurrencyIsoCode, Amount, "
                f"(SELECT Id FROM OpportunityLineItems) FROM Opportunity WHERE Id IN ('{ids}')")
        except SfError as e:
            print(f"\n⚠️  post-push verification query failed: {e}")
            return {"ok": False, "problems": [str(e)]}
        state = {r["Id"]: {"name": r.get("Name"), "stage": r.get("StageName"),
                           "currency": r.get("CurrencyIsoCode"), "amount": r.get("Amount"),
                           "oli_count": self._oli_count(r)} for r in recs}
        rows, problems = verify_rows(targets, state, plan.currency)
        print("\n" + "=" * 64 + "\nPOST-PUSH VERIFICATION\n" + "=" * 64)
        for name, stage, cur, amt, actual, expected, flag in rows:
            print(f"  {flag} {(name or '')[:36]:36s} {str(stage)[:18]:18s} "
                  f"{str(cur or ''):4s} amt={amt} OLIs={actual}/{expected}")
        if problems:
            print(f"\n⚠️  {len(problems)} verification problem(s):")
            for p in problems:
                print(f"   ! {p}")
        else:
            print("  ✓ all opps verified — stage / currency / line items as planned")
        print("=" * 64)
        return {"ok": not problems, "problems": problems}

    def _existing_id(self, sobject: str, fields: dict) -> str:
        """After a duplicate-rule hit, find an existing record to reuse.
        Account -> exact Name; Contact -> exact Email. Returns '' unless there's
        exactly one unambiguous match (never guess across a fuzzy block)."""
        try:
            if sobject == M.SOBJECT["account"]:
                name = fields.get(M.ACCOUNT_FIELDS["name"], "")
                if not name:
                    return ""
                res = _sf(["data", "query", "--query",
                           f"SELECT Id FROM Account WHERE Name = '{_esc(name)}'"], self.org)
            elif sobject == M.SOBJECT["contact"]:
                email = fields.get(M.CONTACT_FIELDS["email"], "")
                if not email:
                    return ""
                res = _sf(["data", "query", "--query",
                           f"SELECT Id FROM Contact WHERE Email = '{_esc(email)}'"], self.org)
            else:
                return ""
            recs = res["result"]["records"]
            return recs[0]["Id"] if len(recs) == 1 else ""
        except SfError:
            return ""

    def _sync_contact_phone(self, cid: str, desired_fields: dict) -> bool:
        """Write the sheet's Phone onto an already-existing (email-matched) contact:
        overwrite if it differs, fill if blank, no-op if already equal. Returns True if
        it issued an update. Email is the exact dedup key, so it's the same person."""
        pf = M.CONTACT_FIELDS["phone"]
        want = desired_fields.get(pf)
        if not want:
            return False
        cur = sf_get_fields(M.SOBJECT["contact"], cid, [pf], self.org).get(pf) or ""
        if str(cur) == str(want):
            return False
        sf_update(M.SOBJECT["contact"], cid, {pf: want}, self.org)
        return True

    def _apply(self, rec: PlannedRecord, applied: dict, confirm_reparent) -> None:
        # Resolve parents; if a REQUIRED parent was skipped, skip this record too
        # (optional lookups like Primary Contact / System Admin are simply dropped).
        optional = {M.OPPORTUNITY_FIELDS.get("primary_contact"),
                    M.OPPORTUNITY_FIELDS.get("system_admin")}
        fields = dict(rec.fields)
        for api, ref in rec.parents.items():
            val = self._resolve(ref)
            unresolved = ref in self.skipped or (val == ref and ":" in str(ref))
            if unresolved:
                if api in optional:
                    continue   # drop optional lookup; still create the child
                self.skipped.add(rec.ref_key)
                self.failures.append((rec.sobject, rec.label, f"parent not created ({ref})"))
                return
            fields[api] = val

        if rec.sobject == M.SOBJECT["contact"]:
            acc_id = fields.get(M.CONTACT_FIELDS["account_id"], "")
            action, cid = resolve_contact(
                fields.get(M.CONTACT_FIELDS["email"], ""), acc_id, self.org)
            if action == "reuse":
                # Email is an EXACT match (the dedup key) -> sync the sheet's Phone onto
                # the existing contact: overwrite if different, fill if blank (Shayan, 2026-06-22).
                if self._sync_contact_phone(cid, fields):
                    applied["updated"] += 1
                else:
                    applied["reused"] += 1
                self.ref_to_id[rec.ref_key] = cid
                return
            if action == "adopt":       # existing contact had no account -> link ours onto it
                upd = {M.CONTACT_FIELDS["account_id"]: acc_id}
                want_phone = fields.get(M.CONTACT_FIELDS["phone"])
                if want_phone:
                    upd[M.CONTACT_FIELDS["phone"]] = want_phone   # also sync the sheet phone
                sf_update(M.SOBJECT["contact"], cid, upd, self.org)
                self.ref_to_id[rec.ref_key] = cid
                applied["updated"] += 1
                self.failures.append((rec.sobject, rec.label,
                                      "existing contact had no account -> linked to this account"))
                return
            if action == "flag_other":  # email on a DIFFERENT account -> don't touch, flag
                self.skipped.add(rec.ref_key)
                self.failures.append((rec.sobject, rec.label,
                                      f"email already on a different account ({cid}) -> flagged, not touched"))
                return
            # action == "create": genuinely new -> create, bypassing SF's fuzzy dup rule
            try:
                new_id = sf_create_contact(fields, self.org)
                self.ref_to_id[rec.ref_key] = new_id
                applied["created"] += 1
            except SfError as e:
                self.skipped.add(rec.ref_key)
                self.failures.append((rec.sobject, rec.label, f"create failed: {e}"))
            return

        if rec.operation == "upsert" and rec.sf_id:
            try:
                # `fields` carries parents already resolved to real Ids (e.g. the opp's
                # Primary/System-Admin contact lookups) -- diff those too, not just rec.fields.
                diff = self._diff_existing(rec, fields)
                if diff:
                    sf_update(rec.sobject, rec.sf_id, {k: v for k, (_, v) in diff.items()}, self.org)
                    applied["updated"] += 1
                else:
                    applied["reused"] += 1
                self.ref_to_id[rec.ref_key] = rec.sf_id
                # Attach mode: closing the opp can trip a SF automation that resets the
                # effective start to CloseDate -- re-apply the sheet value (Shayan, 2026-06-23).
                if rec.sobject == M.SOBJECT["opportunity"]:
                    eff = rec.fields.get(M.OPPORTUNITY_FIELDS["effective_start_date"])
                    if eff:
                        try:
                            sf_update(rec.sobject, rec.sf_id,
                                      {M.OPPORTUNITY_FIELDS["effective_start_date"]: eff}, self.org)
                        except SfError as e:
                            self.failures.append((rec.sobject, rec.label,
                                                  f"effective start not re-applied to {eff!r}: {e}"))
            except SfError as e:
                self.skipped.add(rec.ref_key)
                self.failures.append((rec.sobject, rec.label, f"update failed: {e}"))
            return

        # Create -- resilient to Salesforce duplicate rules (never crash the run).
        try:
            new_id = sf_create(rec.sobject, fields, self.org)
            self.ref_to_id[rec.ref_key] = new_id
            applied["created"] += 1
            # Dependent-picklist quirk: Position_of_Field__c doesn't "take" on
            # insert (Sport__c set in the same DML) -- SF defaults it to "Center".
            # A follow-up update, with Sport__c already committed, applies it.
            if rec.sobject == M.SOBJECT["oli"]:
                pos = rec.fields.get(M.OLI_FIELDS["position_of_field"])
                if pos:
                    try:
                        sf_update(rec.sobject, new_id,
                                  {M.OLI_FIELDS["position_of_field"]: pos}, self.org)
                    except SfError as e:
                        self.failures.append((rec.sobject, rec.label,
                                              f"position not set to {pos!r}: {e}"))
            # Effective start date: a SF automation resets it to CloseDate on insert,
            # so re-apply the sheet value in a follow-up update (Shayan, 2026-06-22).
            if rec.sobject == M.SOBJECT["opportunity"]:
                eff = rec.fields.get(M.OPPORTUNITY_FIELDS["effective_start_date"])
                if eff:
                    try:
                        sf_update(rec.sobject, new_id,
                                  {M.OPPORTUNITY_FIELDS["effective_start_date"]: eff}, self.org)
                    except SfError as e:
                        self.failures.append((rec.sobject, rec.label,
                                              f"effective start not re-applied to {eff!r}: {e}"))
        except SfError as e:
            if "duplicate" in str(e).lower():
                existing = self._existing_id(rec.sobject, fields)
                if existing:                       # safe exact match -> reuse it
                    self.ref_to_id[rec.ref_key] = existing
                    applied["reused"] += 1
                    self.failures.append((rec.sobject, rec.label,
                                          "duplicate rule fired -> reused existing record"))
                else:                              # fuzzy block, no exact match -> skip + flag
                    self.skipped.add(rec.ref_key)
                    self.failures.append((rec.sobject, rec.label,
                                          "duplicate rule blocked; no exact match -> SKIPPED, resolve manually"))
            else:
                self.skipped.add(rec.ref_key)
                self.failures.append((rec.sobject, rec.label, f"create failed: {e}"))
