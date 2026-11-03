#!/usr/bin/env python3
"""Apply the pipeline's deterministic (math-certain) repairs to existing outputs.

This replays `apply_deterministic_repairs` — the same code path the pipeline runs
— over every output JSON, rewriting a payable only when the corrected draft
reproduces the printed gross exactly. No API calls, no fabrication: everything
committed is derived from the document's own printed figures.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from payable_ai.validation import apply_deterministic_repairs, reconcile, shape_errors
from payable_ai.normalization import normalize_currency_code


def main() -> int:
    output_dir = ROOT / "output"
    changed_files = []
    for path in sorted(output_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"skip {path.name}: {exc}")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("payables"), list):
            continue
        dirty = False
        for payable in data["payables"]:
            if not isinstance(payable, dict):
                continue
            # Pure normalization replay (idempotent, deterministic): currency code.
            normalized_currency = normalize_currency_code(payable.get("currency"))
            if normalized_currency != payable.get("currency"):
                print(f"{path.stem}: CURRENCY_NORMALIZED {payable.get('currency')!r} -> {normalized_currency!r}")
                payable["currency"] = normalized_currency
                dirty = True
            before = reconcile(payable).get("status")
            resolved, changes = apply_deterministic_repairs(payable)
            after = reconcile(resolved).get("status")
            # Every mechanism inside apply_deterministic_repairs is exact-gated on
            # its own printed evidence, so a replay over an already-matching record
            # may still refine representation (e.g. minimal-precision tax amounts).
            # The write requires the final state to reconcile and stay schema-clean.
            if changes and after == "MATCH" and not shape_errors(resolved):
                payable.clear()
                payable.update(resolved)
                dirty = True
                for change in changes:
                    print(f"{path.stem}: {change['kind']} {change.get('field_path', '')}: {change.get('message', '')[:140]}")
        if dirty:
            path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
            changed_files.append(path.stem)
    print(f"\nrewrote {len(changed_files)} outputs: {', '.join(changed_files) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
