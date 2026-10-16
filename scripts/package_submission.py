#!/usr/bin/env python3
"""Validate a completed local run and create a clean candidate submission ZIP.

This intentionally refuses placeholder/missing outputs and payables that do not reconcile.
It cannot establish that tax placement/source interpretation is correct; that still needs review.
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from payable_ai.master_data import MasterData
from payable_ai.quality import semantic_issues
from payable_ai.validation import reconcile, shape_errors


CORE_FILES = [
    ".env.example", "README.md", "DESIGN.md", "ARCHITECTURE.md", "AUTODRAFT_SCHEMA.md",
    "requirements.txt", "main.py", "erp.py", "example_check.py", "sample_autodraft.json", "VERIFICATION.md",
]
CORE_DIRS = ["documents", "master_data", "payable_ai", "tests", "scripts"]


def _reference_codes(root: Path) -> dict[str, set[str]]:
    masters = MasterData(root / "master_data")
    codes: dict[str, set[str]] = {
        "supplier": {str(x.get("supplier_id", "")) for x in masters.suppliers},
        "payment_term": {str(x.get("payment_term_id", "")) for x in masters.payment_terms},
        "po": {str(x.get("po_id", "")) for x in masters.purchase_orders},
        "tax": {str(x.get("code", "")) for x in masters.taxes},
        "company": set(), "business_unit": set(), "location": set(),
    }
    for company in masters.companies:
        codes["company"].add(str(company.get("company_code", "")))
        for bu in company.get("business_units", []):
            codes["business_unit"].add(str(bu.get("business_unit_code", "")))
            for loc in bu.get("locations", []):
                codes["location"].add(str(loc.get("location_code", "")))
    return {key: values - {""} for key, values in codes.items()}


def _check_payable(payable: Any, filename: str, index: int, codes: dict[str, set[str]]) -> list[str]:
    problems: list[str] = []
    if not isinstance(payable, dict):
        return [f"{filename} payable[{index}] is not an object"]
    errors = shape_errors(payable)
    problems.extend(f"{filename} payable[{index}] schema: {err}" for err in errors)
    for issue in semantic_issues(payable):
        if issue.get("severity") == "error":
            problems.append(f"{filename} payable[{index}] semantic: {issue.get('code')}: {issue.get('message')}")
    rec = reconcile(payable)
    if rec.get("status") != "MATCH":
        problems.append(f"{filename} payable[{index}] ERP reconciliation is {rec.get('status')} (difference={rec.get('difference')})")

    supplier_id = str(payable.get("supplier", {}).get("supplier_id", "")) if isinstance(payable.get("supplier"), dict) else ""
    if supplier_id and supplier_id not in codes["supplier"]:
        problems.append(f"{filename} payable[{index}] supplier_id {supplier_id!r} does not exist in supplied master data")
    payment_term_id = str(payable.get("payment_term_id", ""))
    if payment_term_id and payment_term_id not in codes["payment_term"]:
        problems.append(f"{filename} payable[{index}] payment_term_id {payment_term_id!r} does not exist in supplied master data")
    po_id = str(payable.get("po_id", ""))
    if po_id and po_id not in codes["po"]:
        problems.append(f"{filename} payable[{index}] po_id {po_id!r} does not exist in supplied master data")
    buyer = payable.get("buyer", {}) if isinstance(payable.get("buyer"), dict) else {}
    for field, kind in (("company_code", "company"), ("business_unit_code", "business_unit"), ("location_code", "location")):
        value = str(buyer.get(field, ""))
        if value and value not in codes[kind]:
            problems.append(f"{filename} payable[{index}] buyer.{field} {value!r} does not exist in supplied master data")
    for tax_idx, tax in enumerate(payable.get("taxes", []) if isinstance(payable.get("taxes"), list) else []):
        if isinstance(tax, dict):
            code = str(tax.get("tax_type_code", ""))
            if code and code not in codes["tax"]:
                problems.append(f"{filename} payable[{index}] taxes[{tax_idx}].tax_type_code {code!r} does not exist in supplied master data")
    for line_idx, line in enumerate(payable.get("line_items", []) if isinstance(payable.get("line_items"), list) else []):
        if isinstance(line, dict):
            for tax_idx, tax in enumerate(line.get("taxes", []) if isinstance(line.get("taxes"), list) else []):
                if isinstance(tax, dict):
                    code = str(tax.get("tax_type_code", ""))
                    if code and code not in codes["tax"]:
                        problems.append(f"{filename} payable[{index}] line_items[{line_idx}].taxes[{tax_idx}].tax_type_code {code!r} does not exist in supplied master data")
    return problems


def validate(root: Path, input_dir: Path, output_dir: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    pdfs = sorted(input_dir.glob("*.pdf"))
    if not pdfs:
        return [f"No PDF inputs found in {input_dir}"], warnings
    expected = {f"{pdf.stem}.json" for pdf in pdfs}
    actual = {p.name for p in output_dir.glob("*.json")} if output_dir.is_dir() else set()
    for missing in sorted(expected - actual):
        errors.append(f"Missing output: {missing}")
    for extra in sorted(actual - expected):
        errors.append(f"Unexpected output not associated with an input PDF: {extra}")
    if errors:
        return errors, warnings
    codes = _reference_codes(root)
    for pdf in pdfs:
        out_path = output_dir / f"{pdf.stem}.json"
        try:
            result = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{out_path.name}: invalid JSON ({type(exc).__name__}: {exc})")
            continue
        if not isinstance(result, dict) or set(result) != {"file", "payables", "declined"}:
            errors.append(f"{out_path.name}: outer object must have exactly file, payables, declined")
            continue
        if result.get("file") != pdf.name:
            errors.append(f"{out_path.name}: file field {result.get('file')!r} does not match {pdf.name!r}")
        if not isinstance(result.get("payables"), list) or not isinstance(result.get("declined"), list):
            errors.append(f"{out_path.name}: payables and declined must be arrays")
            continue
        if not result["payables"] and not result["declined"]:
            errors.append(f"{out_path.name}: empty placeholder detected; classify the document or produce a payable")
        for index, declined in enumerate(result["declined"]):
            if not isinstance(declined, dict) or set(declined) != {"doc_type", "reason"}:
                errors.append(f"{out_path.name}: declined[{index}] must have exactly doc_type and reason")
        for index, payable in enumerate(result["payables"]):
            errors.extend(_check_payable(payable, out_path.name, index, codes))
            if isinstance(payable, dict):
                quality = [q for q in semantic_issues(payable) if q.get("severity") == "warning"]
                if quality:
                    warnings.extend(f"{out_path.name} payable[{index}]: {q.get('code')}: {q.get('message')}" for q in quality)
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate generated outputs and package a clean Zycus submission ZIP")
    parser.add_argument("--input", type=Path, default=ROOT / "documents")
    parser.add_argument("--output", type=Path, default=ROOT / "output")
    parser.add_argument("--zip", type=Path, default=ROOT.parent / "Zycus_Bookable_Payable_Submission.zip")
    parser.add_argument("--allow-quality-warnings", action="store_true", help="Allow packaging after manually reviewing non-error semantic warnings")
    args = parser.parse_args()
    root = ROOT.resolve()
    errors, warnings = validate(root, args.input.resolve(), args.output.resolve())
    if warnings:
        print(f"Quality warnings: {len(warnings)}")
        for warning in warnings[:30]:
            print(f"  REVIEW: {warning}")
        if len(warnings) > 30:
            print(f"  ... and {len(warnings) - 30} more warnings")
    if errors:
        print(f"BLOCKED: {len(errors)} validation error(s); no ZIP created.")
        for error in errors[:80]:
            print(f"  ERROR: {error}")
        if len(errors) > 80:
            print(f"  ... and {len(errors) - 80} more errors")
        return 2
    if warnings and not args.allow_quality_warnings:
        print("BLOCKED: inspect warnings and rerun with --allow-quality-warnings only after manual review.")
        return 3

    target = args.zip.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for rel in CORE_FILES:
        path = root / rel
        if not path.exists():
            print(f"BLOCKED: required project file missing: {rel}")
            return 2
        files.append(path)
    for rel in CORE_DIRS:
        path = root / rel
        if not path.exists():
            print(f"BLOCKED: required project directory missing: {rel}")
            return 2
        if rel == "output":
            continue
        files.extend(item for item in path.rglob("*") if item.is_file())
    files.extend(p for p in args.output.resolve().glob("*.json") if p.is_file())
    # Deduplicate, exclude credentials, generated logs and cache artifacts.
    clean: list[Path] = []
    for path in dict.fromkeys(files):
        try:
            rel = path.resolve().relative_to(root)
        except ValueError:
            # Outputs are expected inside the workspace; do not package arbitrary external paths.
            print(f"BLOCKED: output file is outside project root: {path}")
            return 2
        if rel.name == ".env" or ".venv" in rel.parts or "__pycache__" in rel.parts or ".pytest_cache" in rel.parts:
            continue
        if rel.parts[0] in {"logs", "tmp_smoke"}:
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        clean.append(path)
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as archive:
        for path in sorted(clean):
            archive.write(path, arcname=str(path.resolve().relative_to(root)))
    print(f"Created {target} with {len(clean)} files; output coverage verified for {len(list(args.input.glob('*.pdf')))} PDF(s).")
    print("Warning: this checker can verify schema, reference-code existence and ERP arithmetic; it cannot prove that visual extraction/tax placement is faithful. Manually review the PDFs and diagnostics.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
