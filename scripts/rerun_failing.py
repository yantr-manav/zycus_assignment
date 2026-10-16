#!/usr/bin/env python3
"""Re-run only the documents that still fail reconciliation.

Offline detection (no API calls): every output in ``output/`` is replayed
through the sealed ERP and the printed gross. Documents whose payables do not
reconcile, or whose output is missing/placeholder, are collected and re-run
through the normal pipeline (spending quota only where it matters).

    python scripts/rerun_failing.py                       # detect + re-run failures
    python scripts/rerun_failing.py --dry-run             # list failures only
    python scripts/rerun_failing.py --provider gemini     # force a provider
    python scripts/rerun_failing.py --rounds 3            # repeat until clean (bounded)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from payable_ai.config import load_settings
from payable_ai.pipeline import run_pipeline, select_pdfs
from payable_ai.quality import semantic_issues
from payable_ai.validation import reconcile


def failing_stems(input_dir: Path, output_dir: Path) -> list[str]:
    failures: list[str] = []
    for pdf in select_pdfs(input_dir):
        out_path = output_dir / f"{pdf.stem}.json"
        if not out_path.exists():
            failures.append(pdf.stem)
            continue
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            failures.append(pdf.stem)
            continue
        if not isinstance(data, dict) or not data.get("payables"):
            continue  # honest declines are not failures
        for payable in data.get("payables", []):
            if not isinstance(payable, dict):
                failures.append(pdf.stem)
                break
            if reconcile(payable).get("status") != "MATCH" or any(
                issue.get("severity") == "error" for issue in semantic_issues(payable)
            ):
                failures.append(pdf.stem)
                break
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-run only the documents that fail reconciliation")
    parser.add_argument("--input", type=Path, default=ROOT / "documents")
    parser.add_argument("--output", type=Path, default=ROOT / "output")
    parser.add_argument("--master-data", type=Path, default=ROOT / "master_data")
    parser.add_argument("--log-dir", type=Path, default=ROOT / "logs")
    parser.add_argument("--provider", choices=["auto", "gemini", "groq"], default=None)
    parser.add_argument("--rounds", type=int, default=2, help="attempts per document across passes (default 2)")
    parser.add_argument("--dry-run", action="store_true", help="list failures without spending quota")
    args = parser.parse_args()

    settings = load_settings(args.provider)
    for attempt in range(1, max(1, args.rounds) + 1):
        failures = failing_stems(args.input, args.output)
        if not failures:
            print("All payables reconcile. Nothing to re-run.")
            return 0
        print(f"[pass {attempt}] failing: {', '.join(failures)}")
        if args.dry_run:
            return 0
        run_pipeline(
            args.input, args.output, args.master_data, args.log_dir, settings,
            only=failures,
        )
    remaining = failing_stems(args.input, args.output)
    if remaining:
        print(f"Still failing after {args.rounds} pass(es): {', '.join(remaining)}")
        print("These remain flagged in output/ and logs/; do not fake a fix.")
        return 1
    print("All payables reconcile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
