# Generated outputs

One `<input-stem>.json` per input PDF, produced by running the pipeline over `documents/`:

```powershell
python main.py --input documents --output output
```

Each file follows the candidate brief's outer contract (`file`, `payables[]`, `declined[]`), and every payable matches `AUTODRAFT_SCHEMA.md` exactly. Reconciliation status, provider attempts, ERP facts, repair rounds and evidence audits for these outputs are in `logs/run.jsonl` and `logs/run_summary.json` (diagnostics only — not part of the payable records).

Re-validate without spending quota:

```powershell
python main.py --validate-output --input documents --output output
python scripts/rerun_failing.py --dry-run
```

Any document still not reconciling is flagged in its diagnostics — it is not silently "fixed", and no value is ever invented to make a total foot.
