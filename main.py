from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

from payable_ai.config import load_settings
from payable_ai.llm import LLMRouter
from payable_ai.pipeline import run_inventory, run_pipeline, validate_outputs

ROOT = Path(__file__).resolve().parent


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Evidence-driven PDF-to-payable autodraft pipeline")
    p.add_argument("--input", type=Path, default=ROOT / "documents", help="Folder containing input PDFs")
    p.add_argument("--output", type=Path, default=ROOT / "output", help="Folder to write one JSON per PDF")
    p.add_argument("--master-data", type=Path, default=ROOT / "master_data", help="Folder containing Zycus master-data JSON files")
    p.add_argument("--log-dir", type=Path, default=ROOT / "logs", help="Diagnostics folder; does not go in submitted payable JSON")
    p.add_argument("--provider", choices=["auto", "gemini", "groq"], default=None, help="Override PAYABLE_AI_PROVIDER")
    p.add_argument("--limit", type=int, default=None, help="Process only first N PDFs (smoke testing)")
    p.add_argument("--only", type=str, default=None, help="Comma-separated stems/globs to process (e.g. INV-01,DU-0*)")
    p.add_argument("--diagnose", action="store_true", help="PDF ingestion inventory only; does not call an LLM")
    p.add_argument("--diagnose-ocr", choices=["off", "sample", "all"], default="off", help="OCR amount for --diagnose; off is fast, sample OCRs a few pages, all OCRs every scanned page")
    p.add_argument("--check-config", action="store_true", help="Check SDKs/keys and print effective models without processing PDFs")
    p.add_argument("--probe-keys", action="store_true", help="With --check-config: verify each key with one metadata request (no tokens)")
    p.add_argument("--validate-output", action="store_true", help="Validate existing output JSONs against schema, semantic checks and ERP; no LLM calls")
    return p


def main() -> int:
    args = parser().parse_args()
    settings = load_settings(args.provider)
    if args.validate_output:
        summary = validate_outputs(args.input, args.output, args.log_dir)
        print(json.dumps({k: v for k, v in summary.items() if k != "details"}, indent=2))
        return 0 if summary["ready_for_manual_review"] else 1
    if args.check_config:
        router = LLMRouter(settings)
        summary = {
            "provider_mode": settings.provider,
            "available_providers": router.available(),
            "gemini_model_default": settings.gemini_model,
            "gemini_fallback_models": list(settings.gemini_fallback_models),
            "groq_model_default": settings.groq_model,
            "gemini_keys_found": len(settings.gemini_api_keys),
            "groq_keys_found": len(settings.groq_api_keys),
            "min_call_interval_seconds": settings.min_call_interval_seconds,
            "repair_rounds": settings.repair_rounds,
            "note": "Key values are never printed. Run `--check-config --probe-keys` to verify each key with a metadata request.",
        }
        if getattr(args, "probe_keys", False):
            summary["key_probe"] = router.probe_keys()
        print(json.dumps(summary, indent=2))
        return 0 if router.available() else 2
    if args.diagnose:
        report = run_inventory(args.input, args.log_dir, settings, args.limit, ocr_mode=args.diagnose_ocr)
        print(json.dumps({k: v for k, v in report.items() if k != "inventory"}, indent=2))
        for row in report["inventory"]:
            print(f"{row['file']}: pages={row.get('pages','?')} chars={row.get('characters','?')} confidence={row.get('mean_ocr_confidence')} needs_vision={row.get('needs_vision')} sources={row.get('page_sources', [])}")
        return 0 if not any("error" in r for r in report["inventory"]) else 1
    try:
        only = [p for p in (args.only or "").split(",") if p.strip()] or None
        summary = run_pipeline(args.input, args.output, args.master_data, args.log_dir, settings, args.limit, only=only)
        print("\nRun summary:\n" + json.dumps(summary, indent=2))
        return 1 if summary["errors"] or summary["files_with_schema_warnings"] else 0
    except Exception as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
