from __future__ import annotations
import fnmatch
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .keys import redact
from .llm import LLMRouter
from .master_data import MasterData
from .normalization import normalize_payable, normalize_document_type
from .pdf_io import ensure_full_ocr, read_pdf
from .signals import build_signals, missing_rows_delta
from .validation import shape_errors, reconcile, apply_grounded_repairs, apply_deterministic_repairs
from .quality import semantic_issues
from .evidence import audit_field_evidence


def _write_json(path: Path, data: Any) -> None:
    # ensure_ascii keeps the artifacts readable by tools that open JSON with a
    # non-UTF-8 locale (Windows cp1252 graders). Values are identical.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _outer_result(filename: str, payables: list[dict[str, Any]], declined: list[dict[str, str]]) -> dict[str, Any]:
    return {"file": filename, "payables": payables, "declined": declined}


def _decline(doc_type: str, reason: str) -> list[dict[str, str]]:
    return [{"doc_type": doc_type or "UNKNOWN", "reason": (reason or "No bookable payable identified from the available document evidence.").strip()}]


def _abs_difference(validation: dict[str, Any]) -> float | None:
    try:
        return abs(float(validation["difference"]))
    except (KeyError, TypeError, ValueError):
        return None


def process_one(path: Path, output_dir: Path, masters: MasterData, router: LLMRouter, settings: Settings, _review_signal: str = "") -> dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat()
    doc = read_pdf(path, dpi=settings.render_dpi, max_vision_pages=settings.max_vision_pages, ocr_mode=settings.ocr_mode)
    llm_result = router.extract(doc, path.name, review_signal=_review_signal)
    # Local OCR is free; completing it here gives the evidence audit and the
    # repair-grounding checks real text to verify against, on the vision route too.
    try:
        ensure_full_ocr(doc)
    except Exception as exc:
        doc.warnings.append(f"on-demand OCR completion failed: {type(exc).__name__}: {exc}")
    extracted = llm_result.data
    doc_type = normalize_document_type(extracted.get("document_type"))
    raw_payables = extracted.get("payables", [])
    if not isinstance(raw_payables, list): raw_payables = []
    is_payable = bool(extracted.get("is_payable")) or bool(raw_payables)
    final_payables: list[dict[str, Any]] = []
    payable_diagnostics: list[dict[str, Any]] = []

    for index, original_raw in enumerate(raw_payables):
        if not isinstance(original_raw, dict):
            payable_diagnostics.append({"index": index, "status": "INVALID_MODEL_RECORD", "error": "Payable was not a JSON object"})
            continue
        raw_payable = dict(original_raw)
        if not raw_payable.get("invoice_type"):
            raw_payable["invoice_type"] = "CREDIT_MEMO" if doc_type == "CREDIT_MEMO" else "INVOICE"
        if not raw_payable.get("gross_total") and len(raw_payables) == 1:
            # No derived totals. Leave it empty; this diagnostic helps identify unresolved cases.
            pass
        payable, norm_diag = normalize_payable(raw_payable, masters)
        # Determinism boundary: math-certain repairs (quantities pinned by printed
        # row arithmetic, duplicated tax levels, printed-formula tax amounts) are
        # restored before any judgment work begins. Each is exact-match gated.
        payable, component_repairs = apply_deterministic_repairs(payable)
        initial_errors = shape_errors(payable)
        initial_quality = semantic_issues(payable)
        validation = reconcile(payable)
        repairs: list[dict[str, Any]] = []
        repair_summary = ""
        raw_after_repairs = raw_payable
        repair_rounds_log: list[dict[str, Any]] = []
        conversation = llm_result.conversation

        # ── Bounded reconcile-repair loop ─────────────────────────────────────
        # Each round: the deterministic layer states FACTS about what the sealed
        # ERP derived from the current draft (erp_trace/signals.py), the model
        # decides source-grounded corrections, and the deterministic layer either
        # accepts a strictly improving, grounding-verified candidate or stops.
        # Termination: MATCH, no accepted change, or the round budget.
        if settings.repair_on_mismatch:
            for round_index in range(settings.repair_rounds):
                if validation.get("status") != "MISMATCH":
                    break
                round_number = round_index + 1
                current_quality = semantic_issues(payable)
                signals = build_signals(payable, validation, current_quality)
                try:
                    repair_result = router.repair(
                        doc=doc,
                        filename=path.name,
                        payable=payable,
                        booked=float(validation["booked_gross"]),
                        printed_gross=str(payable.get("gross_total", "")),
                        preferred_provider=llm_result.provider,
                        signals=signals,
                        state=conversation,
                    )
                    conversation = repair_result.conversation or conversation
                    diagnosis = str(repair_result.data.get("diagnosis") or repair_result.data.get("summary") or "")
                    candidate_raw, repair_decisions = apply_grounded_repairs(raw_after_repairs, repair_result.data, doc.text)
                    repairs.extend({**decision, "round": round_number} for decision in repair_decisions)
                    accepted = [r for r in repair_decisions if r.get("accepted")]
                    if not accepted:
                        repair_summary = diagnosis or "No source-grounded correction proposed."
                        repair_rounds_log.append({
                            "round": round_number, "signals": [s.get("kind") for s in signals],
                            "status_before": validation.get("status"), "accepted_changes": 0,
                            "diagnosis": diagnosis, "outcome": "no accepted change",
                        })
                        break
                    candidate_payable, candidate_diag = normalize_payable(candidate_raw, masters)
                    candidate_payable, candidate_components = apply_deterministic_repairs(candidate_payable)
                    candidate_validation = reconcile(candidate_payable)
                    candidate_errors = shape_errors(candidate_payable)
                    candidate_quality = semantic_issues(candidate_payable)
                    current_delta = _abs_difference(validation)
                    candidate_delta = _abs_difference(candidate_validation)
                    current_critical = sum(1 for q in current_quality if q.get("severity") == "error")
                    candidate_critical = sum(1 for q in candidate_quality if q.get("severity") == "error")
                    # A correction must be source-grounded, schema-clean, reduce the reconciliation
                    # discrepancy, and not add semantic errors. Matching the gross is necessary but
                    # does not prove tax/component placement, which remains visible as quality diagnostics.
                    improves = (
                        candidate_validation.get("status") == "MATCH"
                        or (candidate_delta is not None and current_delta is not None and candidate_delta < current_delta)
                    )
                    # A neutralization (clearing a component) carries no new value to
                    # ground, so its bar is exactness: the corrected draft must land on
                    # the printed gross precisely, never merely improve the delta.
                    has_clear = any(r.get("cleared") for r in accepted)
                    if has_clear and candidate_validation.get("status") != "MATCH":
                        improves = False
                    accepted_paths = [r.get("path") for r in accepted]
                    if not candidate_errors and candidate_critical <= current_critical and improves:
                        raw_after_repairs = candidate_raw
                        payable = candidate_payable
                        norm_diag = candidate_diag
                        component_repairs = component_repairs + [
                            {**item, "accepted": True, "round": round_number} for item in candidate_components
                        ]
                        validation = candidate_validation
                        initial_errors = candidate_errors
                        initial_quality = candidate_quality
                        repair_summary = diagnosis
                        repair_rounds_log.append({
                            "round": round_number, "signals": [s.get("kind") for s in signals],
                            "status_before": "MISMATCH", "accepted_changes": len(accepted),
                            "accepted_paths": accepted_paths,
                            "status_after": validation.get("status"),
                            "difference_after": validation.get("difference"),
                            "diagnosis": diagnosis, "outcome": "accepted",
                        })
                    else:
                        reject_reason = "; ".join([
                            *([f"schema errors: {candidate_errors}"] if candidate_errors else []),
                            *([f"semantic errors increased ({current_critical} -> {candidate_critical})"] if candidate_critical > current_critical else []),
                            *([f"reconciliation did not improve ({validation.get('difference')} -> {candidate_validation.get('difference')})"] if not improves else []),
                        ])
                        repairs.append({"accepted": False, "reason": "candidate rejected: " + reject_reason, "round": round_number})
                        repair_rounds_log.append({
                            "round": round_number, "signals": [s.get("kind") for s in signals],
                            "status_before": "MISMATCH", "accepted_changes": 0,
                            "diagnosis": diagnosis, "outcome": f"candidate rejected: {reject_reason}",
                        })
                        break
                except Exception as exc:
                    repair_summary = redact(f"repair pass failed: {type(exc).__name__}: {exc}")
                    repair_rounds_log.append({"round": round_number, "outcome": repair_summary})
                    break

        final_errors = shape_errors(payable)
        final_quality = semantic_issues(payable)
        evidence_audit = audit_field_evidence(norm_diag.get("field_evidence", {}), doc.pages)
        if evidence_audit["record_count"] == 0:
            final_quality.append({"code": "NO_FIELD_EVIDENCE", "severity": "warning", "message": "The extraction model returned no page/quote evidence for its financial fields."})
        else:
            text_pages = {int(page.page_number) for page in doc.pages if page.text.strip()}
            evidence_audit["visual_only_count"] = sum(1 for record in evidence_audit["records"] if not record["verified_against_text"] and record["page"] not in text_pages)
            evidence_audit["unverified_text_count"] = sum(1 for record in evidence_audit["records"] if not record["verified_against_text"] and record["page"] in text_pages)
            if evidence_audit["unverified_text_count"] > 0:
                final_quality.append({"code": "UNVERIFIED_FIELD_EVIDENCE", "severity": "warning", "message": f"{evidence_audit['unverified_text_count']} field evidence quote(s) could not be verified against available native/OCR page text; inspect visually."})
        final_payables.append(payable)
        payable_diagnostics.append({
            "index": index,
            "invoice_number": payable.get("invoice_number", ""),
            "invoice_type": payable.get("invoice_type", ""),
            "currency": payable.get("currency", ""),
            "validation": validation,
            "schema_errors": final_errors,
            "quality_issues": final_quality,
            "master_matches": norm_diag.get("master_matches", {}),
            "field_evidence": norm_diag.get("field_evidence", {}),
            "evidence_audit": evidence_audit,
            "repair_summary": repair_summary,
            "repair_decisions": repairs,
            "component_repairs": component_repairs,
            "repair_rounds": repair_rounds_log,
            "repaired": raw_after_repairs is not raw_payable or bool(component_repairs),
        })

    declined: list[dict[str, str]] = []
    if not final_payables and not is_payable:
        why = str(extracted.get("declined_reason") or extracted.get("classification_reason") or "No bookable payable identified from the document.")
        declined = _decline(doc_type, why)
    elif not final_payables and is_payable:
        # The model believed a payable exists but failed to construct a record; do not mislabel it as non-payable.
        declined = []
    else:
        # If the model also recognized an embedded non-payable section, include it only when meaningful.
        nonpayable_note = str(extracted.get("declined_reason", "")).strip()
        if nonpayable_note and len(raw_payables) > 0 and extracted.get("contains_non_payable_section") is True:
            declined = _decline(str(extracted.get("non_payable_type", "OTHER")), nonpayable_note)

    # ── Deterministic completeness check ──────────────────────────────────────
    # When the extracted line amounts reproduce none of the printed totals, rows
    # were dropped or mis-transcribed. That is a transcription deficiency, not a
    # component-misplacement one, so the remedy is one corrective re-read of the
    # same document carrying the verified gap as a signal. The retry is kept
    # only when it reconciles at least as well as the first attempt.
    def _attempt_score(rows: list[dict[str, Any]]) -> tuple[int, float]:
        matches = sum(1 for row in rows if row.get("validation", {}).get("status") == "MATCH")
        gaps = sum(float(row.get("completeness_gap") or 0.0) for row in rows)
        return (matches, -round(gaps, 2))

    completeness_gaps: list[dict[str, Any]] = []
    completeness_note = ""
    for index, payable in enumerate(final_payables):
        gap = missing_rows_delta(payable)
        if gap is not None and reconcile(payable).get("status") == "MISMATCH":
            completeness_gaps.append({"index": index, "invoice_number": payable.get("invoice_number", ""), "gap": gap})
            if index < len(payable_diagnostics):
                payable_diagnostics[index]["completeness_gap"] = gap

    if completeness_gaps and not _review_signal:
        notes = [
            f"- The draft for payable #{g['index']}"
            + (f" (invoice {g['invoice_number']!r})" if g["invoice_number"] else "")
            + f" has line amounts that match none of the document's printed totals; the closest printed total is off by "
              f"{g['gap']:.2f}. Rows are missing or mis-transcribed. Re-read the item table row by row — including rows "
              "directly under section headings, continuation lines, and rows after subtotal separators — and return the "
              "complete payable list. Verify that the sum of your line amounts reproduces a printed total."
            for g in completeness_gaps
        ]
        retry_signal = ("The line-sum check found incomplete extraction:\n" + "\n".join(notes))
        try:
            retry_diagnostics = process_one(path, output_dir, masters, router, settings, _review_signal=retry_signal)
            retry_rows = retry_diagnostics.get("payables", [])
            if _attempt_score(retry_rows) > _attempt_score(payable_diagnostics):
                retry_diagnostics["completeness_reextract"] = "used (better reconciliation than first attempt)"
                return retry_diagnostics
            # Retry did not improve: restore this attempt's output (the retry wrote its own).
            _write_json(output_dir / f"{path.stem}.json", _outer_result(path.name, final_payables, declined))
            completeness_note = "attempted (kept the better attempt)"
            for row in payable_diagnostics:
                row.pop("completeness_gap", None)
            for g in completeness_gaps:
                if g["index"] < len(payable_diagnostics):
                    payable_diagnostics[g["index"]]["completeness_gap"] = g["gap"]
        except Exception as exc:
            doc.warnings.append(f"completeness re-extraction failed: {type(exc).__name__}: {exc}")
            _write_json(output_dir / f"{path.stem}.json", _outer_result(path.name, final_payables, declined))

    result = _outer_result(path.name, final_payables, declined)
    output_path = output_dir / f"{path.stem}.json"
    _write_json(output_path, result)
    all_schema_errors = any(d.get("schema_errors", []) or any(q.get("severity") == "error" for q in d.get("quality_issues", [])) for d in payable_diagnostics)
    needs_review = (is_payable and not final_payables) or any(
        d.get("validation", {}).get("status") in {"MISMATCH", "NO_PRINTED_GROSS", "UNPARSEABLE_PRINTED_GROSS", "ERROR"}
        or bool(d.get("quality_issues"))
        for d in payable_diagnostics
    )
    diagnostics = {
        "file": path.name,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "SCHEMA_WARNING" if all_schema_errors else ("NEEDS_REVIEW" if needs_review else "OK"),
        "provider": llm_result.provider,
        "model": llm_result.model,
        "provider_attempts": llm_result.attempts,
        "document_type": doc_type,
        "is_payable": is_payable,
        "page_count": doc.page_count,
        "characters_extracted": doc.character_count,
        "mean_ocr_confidence": doc.mean_ocr_confidence,
        "needs_vision": doc.needs_vision,
        "pdf_warnings": doc.warnings,
        "classification_reason": str(extracted.get("classification_reason", "")),
        "payables": payable_diagnostics,
        "output_path": str(output_path),
    }
    if completeness_note:
        diagnostics["completeness_reextract"] = completeness_note
    return diagnostics


def run_inventory(input_dir: Path, log_dir: Path, settings: Settings, limit: int | None = None, ocr_mode: str = "off") -> dict[str, Any]:
    files = sorted(input_dir.glob("*.pdf"))
    if limit is not None: files = files[:limit]
    inventory=[]
    for path in files:
        try:
            doc = read_pdf(path, dpi=settings.render_dpi, max_vision_pages=settings.max_vision_pages, ocr_mode=ocr_mode)
            inventory.append({
                "file": path.name, "pages": doc.page_count, "characters": doc.character_count,
                "mean_ocr_confidence": doc.mean_ocr_confidence, "needs_vision": doc.needs_vision,
                "page_sources": [p.source for p in doc.pages], "ocr_mode": ocr_mode, "warnings": doc.warnings,
            })
        except Exception as exc:
            inventory.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_dir": str(input_dir), "file_count": len(files), "inventory": inventory,
    }
    _write_json(log_dir / "pdf_inventory.json", report)
    return report


def select_pdfs(input_dir: Path, only: list[str] | None = None, limit: int | None = None) -> list[Path]:
    """All PDFs, optionally filtered by stem/name globs (e.g. INV-01, 'DU-*')."""
    pdfs = sorted(input_dir.glob("*.pdf"))
    if only:
        patterns = [p.strip().casefold() for p in only if p.strip()]
        selected = []
        for pdf in pdfs:
            stem = pdf.stem.casefold()
            name = pdf.name.casefold()
            if any(fnmatch.fnmatch(stem, pattern) or fnmatch.fnmatch(name, pattern) for pattern in patterns):
                selected.append(pdf)
        pdfs = selected
    if limit is not None:
        pdfs = pdfs[:limit]
    return pdfs


def run_pipeline(input_dir: Path, output_dir: Path, master_dir: Path, log_dir: Path, settings: Settings, limit: int | None = None, only: list[str] | None = None) -> dict[str, Any]:
    if not input_dir.exists() or not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    pdfs = select_pdfs(input_dir, only=only, limit=limit)
    if not pdfs:
        raise RuntimeError(f"No PDFs found in {input_dir}" + (f" matching {only!r}" if only else ""))
    masters = MasterData(master_dir)
    router = LLMRouter(settings)
    router.ensure_available()
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    # Remove previous versions only for files processed in this run, avoiding stale per-file results.
    for p in pdfs:
        (output_dir / f"{p.stem}.json").unlink(missing_ok=True)
    log_path = log_dir / "run.jsonl"
    failures=[]; statuses=[]
    with log_path.open("w", encoding="utf-8") as log:
        for idx, path in enumerate(pdfs, start=1):
            print(f"[{idx}/{len(pdfs)}] {path.name} ...", flush=True)
            try:
                diagnostic = process_one(path, output_dir, masters, router, settings)
                if diagnostic["status"] != "OK": failures.append({"file": path.name, "reason": diagnostic["status"]})
            except Exception as exc:
                # Emit schema-shaped placeholder so batch processing can continue, but mark the run failed.
                # Do NOT submit this placeholder as a valid extraction; inspect run.jsonl and rerun it.
                _write_json(output_dir / f"{path.stem}.json", _outer_result(path.name, [], []))
                diagnostic = {
                    "file": path.name, "status": "ERROR_NO_VALID_EXTRACTION", "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=5), "output_path": str(output_dir / f"{path.stem}.json"),
                }
                failures.append({"file": path.name, "reason": diagnostic["error"]})
                print(f"  ERROR: {diagnostic['error']}", flush=True)
            statuses.append(diagnostic)
            log.write(json.dumps(diagnostic, ensure_ascii=False) + "\n")
            log.flush()
            if diagnostic.get("status") not in {"ERROR_NO_VALID_EXTRACTION"}:
                reconciles = [p.get("validation", {}).get("status", "") for p in diagnostic.get("payables", [])]
                print(f"  provider={diagnostic.get('provider')} type={diagnostic.get('document_type')} payables={len(diagnostic.get('payables', []))} reconcile={reconciles}", flush=True)
    summary = {
        "total_files": len(pdfs),
        "successful_files": sum(1 for s in statuses if s.get("status") == "OK"),
        "files_needing_review": sum(1 for s in statuses if s.get("status") == "NEEDS_REVIEW"),
        "files_with_schema_warnings": sum(1 for s in statuses if s.get("status") == "SCHEMA_WARNING"),
        "errors": failures,
        "output_dir": str(output_dir),
        "diagnostics_jsonl": str(log_path),
    }
    _write_json(log_dir / "run_summary.json", summary)
    return summary


def validate_outputs(input_dir: Path, output_dir: Path, log_dir: Path) -> dict[str, Any]:
    """Validate an existing output folder without making any LLM/API calls."""
    from .validation import reconcile
    from .quality import semantic_issues

    pdfs = sorted(input_dir.glob("*.pdf")) if input_dir.is_dir() else []
    report: list[dict[str, Any]] = []
    missing = 0
    malformed = 0
    schema_problem = 0
    reconciliation_problem = 0
    review_needed = 0
    for pdf in pdfs:
        path = output_dir / f"{pdf.stem}.json"
        row: dict[str, Any] = {"file": pdf.name, "output": str(path), "issues": [], "payables": []}
        if not path.exists():
            missing += 1
            row["issues"].append({"code": "MISSING_OUTPUT", "severity": "error", "message": "No JSON output exists for this input PDF."})
            report.append(row)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            malformed += 1
            row["issues"].append({"code": "INVALID_JSON", "severity": "error", "message": f"{type(exc).__name__}: {exc}"})
            report.append(row)
            continue
        if not isinstance(data, dict) or set(data) != {"file", "payables", "declined"}:
            malformed += 1
            row["issues"].append({"code": "INVALID_OUTER_SCHEMA", "severity": "error", "message": "Outer object must have exactly file, payables, declined."})
            report.append(row)
            continue
        if data.get("file") != pdf.name:
            row["issues"].append({"code": "FILENAME_MISMATCH", "severity": "error", "message": f"Output file field is {data.get('file')!r}, expected {pdf.name!r}."})
        if not isinstance(data.get("payables"), list) or not isinstance(data.get("declined"), list):
            malformed += 1
            row["issues"].append({"code": "INVALID_OUTER_TYPES", "severity": "error", "message": "payables and declined must be arrays."})
            report.append(row)
            continue
        for index, payable in enumerate(data["payables"]):
            pdiag: dict[str, Any] = {"index": index}
            if not isinstance(payable, dict):
                pdiag["schema_errors"] = ["payable must be an object"]
                pdiag["validation"] = {"status": "ERROR"}
                schema_problem += 1
                reconciliation_problem += 1
            else:
                errors = shape_errors(payable)
                quality = semantic_issues(payable)
                rec = reconcile(payable)
                pdiag.update({"schema_errors": errors, "quality_issues": quality, "validation": rec})
                if errors or any(i.get("severity") == "error" for i in quality):
                    schema_problem += 1
                if rec.get("status") != "MATCH":
                    reconciliation_problem += 1
                if quality or rec.get("status") != "MATCH":
                    review_needed += 1
            row["payables"].append(pdiag)
        if row["issues"]:
            malformed += 1
        report.append(row)

    summary = {
        "input_pdf_count": len(pdfs),
        "outputs_found": len(pdfs) - missing,
        "missing_outputs": missing,
        "malformed_outputs": malformed,
        "payables_with_schema_or_semantic_errors": schema_problem,
        "payables_not_reconciled": reconciliation_problem,
        "payables_needing_review": review_needed,
        "ready_for_manual_review": missing == 0 and malformed == 0 and schema_problem == 0 and reconciliation_problem == 0,
        "note": "A MATCH only proves arithmetic reconciliation, not source fidelity or correct tax placement. Inspect warnings and PDFs before submission.",
        "details": report,
    }
    _write_json(log_dir / "output_validation.json", summary)
    return summary
