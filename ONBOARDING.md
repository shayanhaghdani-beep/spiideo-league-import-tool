# League Import Tool — START HERE

**Local path:** `Claude-Code-Projects/league_deals_dataload/` (own top-level project)
**Shared-brain ticket:** https://app.notion.com/p/3817ee4294a8816fb6e1d492fdd14a23

Turns a league's **Main Camera Sheet** (one row per club/team — subscription + up to 7 cameras +
shipping) into Salesforce records for **big league deals**. **It pushes to Salesforce directly via
the `sf` CLI, after a dry-run preview + a typed `yes`.**

## Where to look
| Need | File |
|---|---|
| How to run it (step by step) | **`RUNBOOK.md`** |
| Project context + guardrails (auto-loads in Claude Code) | **`CLAUDE.md`** |
| Every sheet column → SF field (source of truth) | **`MAPPING.md`** + `league_dataload/v2/mapping.py` |
| The v2 code | `league_dataload/v2/` (CLI: `python3 -m league_dataload.v2`) |
| Club matcher | `league_dataload/clubmatch/` |
| Full history / decisions / first-batch steps | the Notion ticket above |

## TL;DR run
```bash
pip install openpyxl
python3 -m league_dataload.v2 gen --out outputs/WHL.xlsx --league "WHL"   # Phase A: make the sheet
#   fill the Config tab (structure 1/2/3, master opp Id, currency, gender, pricing) + green cols
python3 -m league_dataload.v2 import --sheet outputs/WHL.xlsx            # Phase B: dry-run preview
python3 -m league_dataload.v2 import --sheet outputs/WHL.xlsx --live     # writes after typed 'yes'
```

## Non-negotiables
- Never create the master league opp — it's always an input.
- Live writes are gated (dry-run default + `--live` + typed `yes`). No sandbox (prod `spiideo` only) →
  canary one club first. Don't re-run the same sheet `--live` twice (opps/OLIs always create).
- Closed Won child opps fire the Younium sync (real orders).

## History
Started (v1) as a CSV emitter for manual dataload; **reshaped to v2** (direct, confirm-gated SF
import) on 2026-06-16/17. The old `README.md` describes the legacy v1 CSV flow and is superseded —
use `RUNBOOK.md` / `CLAUDE.md`.
