"""Audit model-provided field evidence against page text for diagnostics."""
from __future__ import annotations

from typing import Any
from rapidfuzz import fuzz
from .master_data import norm_text


def audit_field_evidence(field_evidence: Any, pages: list[Any]) -> dict[str, Any]:
    page_text = {int(p.page_number): str(p.text or "") for p in pages}
    records: list[dict[str, Any]] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            if "quote" in value and "page" in value:
                records.append({"path": path or "field_evidence", "page": value.get("page"), "quote": value.get("quote")})
                return
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(field_evidence if isinstance(field_evidence, (dict, list)) else {})
    checked: list[dict[str, Any]] = []
    for item in records:
        try:
            page_no = int(item["page"])
        except (TypeError, ValueError):
            page_no = -1
        quote = str(item.get("quote") or "").strip()
        source = page_text.get(page_no, "")
        nq, ns = norm_text(quote), norm_text(source)
        if not quote or not source:
            ok = False
            reason = "empty quote or page number not present"
            score = 0
        elif nq and nq in ns:
            ok = True
            reason = "normalized exact match"
            score = 100
        else:
            score = fuzz.partial_ratio(nq, ns) if nq and ns else 0
            ok = score >= 92
            reason = "high similarity" if ok else "quote not found in OCR/native text; vision-only evidence may be valid but needs inspection"
        checked.append({"path": item["path"], "page": page_no, "verified_against_text": ok, "similarity": round(float(score), 1), "reason": reason})
    verified = sum(1 for item in checked if item["verified_against_text"])
    return {
        "record_count": len(checked),
        "verified_count": verified,
        "unverified_count": len(checked) - verified,
        "coverage": round(verified / len(checked), 3) if checked else 0.0,
        "records": checked,
    }
