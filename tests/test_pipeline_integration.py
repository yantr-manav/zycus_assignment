from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from payable_ai.config import load_settings
from payable_ai.llm import LLMResult
from payable_ai.pdf_io import PDFDocument, PageContent
from payable_ai.master_data import MasterData
from payable_ai.pipeline import process_one


class FakeRouter:
    def __init__(self, payload): self.payload = payload
    def extract(self, doc, filename, **kwargs): return LLMResult(self.payload, "groq", "fake-test-model", ["groq:success"])
    def repair(self, *args, **kwargs): raise AssertionError("matching sample should not require repair")


def test_pipeline_writes_schema_clean_json_without_api(tmp_path, monkeypatch):
    from payable_ai import pipeline
    sample = json.loads((ROOT / "sample_autodraft.json").read_text(encoding="utf-8"))
    payload = {
        "document_type": "INVOICE",
        "is_payable": True,
        "classification_reason": "supplier invoice with amount due",
        "declined_reason": "",
        "payables": [{
            "invoice_number": sample["invoice_number"], "invoice_date": sample["invoice_date"], "due_date": sample["due_date"],
            "invoice_type": "INVOICE", "currency": "EUR",
            "supplier": {"name": sample["supplier"]["name"], "address": sample["supplier"]["address"], "vat_id": sample["supplier"]["vat_id"], "country": "DE"},
            "buyer": {"company_name": "Bolt Group", "business_unit_name": "Bolt Holdings OU", "location_name": "Tallinn - HQ", "invoice_to_address": "Vana-Louna 15, 10134 Tallinn, Estonia"},
            "payment_term_text": "Net 10", "po_number": "", "gross_total": "438.00", "subtotal": "438.00", "total_tax_amount": "0.00",
            "discount_amount": "", "freight_charges": "", "insurance_charges": "", "extra_charges": "", "excise_duties": "",
            "taxes": [{"tax_type": "VAT", "tax_name": "VAT Reverse Charge", "tax_rate": "0", "tax_amount": "0.00"}],
            "line_items": [
                {"description": "Projektmanagement Nachberechnung aus November", "item_type": "SERVICE", "uom": "Hr", "quantity": "4", "unit_price": "73.00", "total": "292.00", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
                {"description": "Projektmanagement Nachberechnung aus Dezember", "item_type": "SERVICE", "uom": "Hr", "quantity": "2", "unit_price": "73.00", "total": "146.00", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
            ],
            "field_evidence": {"gross_total": {"page": 1, "quote": "Endbetrag 438,00 EUR"}}
        }]
    }
    fake_doc = PDFDocument(path=tmp_path / "INV-01.pdf", pages=[PageContent(1, "Endbetrag 438,00 EUR", "test", 1.0, None)])
    monkeypatch.setattr(pipeline, "read_pdf", lambda *a, **kw: fake_doc)
    outdir = tmp_path / "output"
    diag = process_one(tmp_path / "INV-01.pdf", outdir, MasterData(ROOT / "master_data"), FakeRouter(payload), load_settings())
    result = json.loads((outdir / "INV-01.json").read_text(encoding="utf-8"))
    assert set(result) == {"file", "payables", "declined"}
    assert len(result["payables"]) == 1
    assert diag["payables"][0]["validation"]["status"] == "MATCH"
    assert diag["payables"][0]["schema_errors"] == []
    assert result["payables"][0]["supplier"]["supplier_id"] == "2845695"


class RepairingRouter:
    """Extraction is wrong on the first try; one grounded repair turn fixes it."""

    def __init__(self, payload, repair_payload):
        self.payload = payload
        self.repair_payload = repair_payload
        self.repair_calls = 0

    def extract(self, doc, filename, **kwargs):
        return LLMResult(self.payload, "groq", "fake-test-model", ["groq:success"])

    def repair(self, *args, **kwargs):
        self.repair_calls += 1
        assert kwargs.get("signals"), "repair must be driven by deterministic signals"
        return LLMResult(self.repair_payload, "groq", "fake-test-model", ["groq:repair_success"])


def test_reconcile_repair_loop_converges_with_grounded_change(tmp_path, monkeypatch):
    from payable_ai import pipeline
    payload = {
        "document_type": "INVOICE", "is_payable": True,
        "classification_reason": "service invoice", "declined_reason": "",
        "payables": [{
            "invoice_number": "REP-1", "invoice_date": "2026-01-05", "due_date": "",
            "invoice_type": "INVOICE", "currency": "EUR",
            "supplier": {"name": "Vantek Asia Pte Ltd", "address": "", "vat_id": "", "country": "SG"},
            "buyer": {"company_name": "", "business_unit_name": "", "location_name": "", "invoice_to_address": ""},
            "payment_term_text": "", "po_number": "", "gross_total": "100.00", "subtotal": "100.00", "total_tax_amount": "",
            "discount_amount": "", "freight_charges": "", "insurance_charges": "", "extra_charges": "", "excise_duties": "",
            "taxes": [],
            "line_items": [{
                "description": "Service", "item_type": "SERVICE", "uom": "EA",
                "quantity": "2", "unit_price": "40.00", "total": "100.00",
                "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": [],
            }],
        }],
    }
    repair_payload = {
        "diagnosis": "printed unit price was misread",
        "changes": [{
            "path": "line_items[0].unit_price", "value": "50.00",
            "evidence_quote": "Service unit price: 50.00 EUR",
            "reason": "the source line prints 50.00",
        }],
    }
    text = "Invoice REP-1. Service unit price: 50.00 EUR; line total 100.00 EUR."
    fake_doc = PDFDocument(path=tmp_path / "REP-1.pdf", pages=[PageContent(1, text, "test", 1.0, None)])
    monkeypatch.setattr(pipeline, "read_pdf", lambda *a, **kw: fake_doc)
    outdir = tmp_path / "output"
    router = RepairingRouter(payload, repair_payload)
    diag = process_one(tmp_path / "REP-1.pdf", outdir, MasterData(ROOT / "master_data"), router, load_settings())
    result = json.loads((outdir / "REP-1.json").read_text(encoding="utf-8"))
    assert router.repair_calls == 1
    assert result["payables"][0]["line_items"][0]["unit_price"] == "50.00"
    assert diag["payables"][0]["validation"]["status"] == "MATCH"
    rounds = diag["payables"][0]["repair_rounds"]
    assert rounds and rounds[0]["outcome"] == "accepted"
    assert "LINE_BASE_VS_PRINTED_TOTAL" in rounds[0]["signals"]


def test_reconcile_repair_loop_rejects_ungrounded_change(tmp_path, monkeypatch):
    from payable_ai import pipeline
    payload = {
        "document_type": "INVOICE", "is_payable": True,
        "classification_reason": "service invoice", "declined_reason": "",
        "payables": [{
            "invoice_number": "REP-2", "invoice_date": "2026-01-05", "due_date": "",
            "invoice_type": "INVOICE", "currency": "EUR",
            "supplier": {"name": "Unknown Supplier", "address": "", "vat_id": "", "country": ""},
            "buyer": {"company_name": "", "business_unit_name": "", "location_name": "", "invoice_to_address": ""},
            "payment_term_text": "", "po_number": "", "gross_total": "100.00", "subtotal": "100.00", "total_tax_amount": "",
            "discount_amount": "", "freight_charges": "", "insurance_charges": "", "extra_charges": "", "excise_duties": "",
            "taxes": [],
            "line_items": [{
                "description": "Service", "item_type": "SERVICE", "uom": "EA",
                "quantity": "2", "unit_price": "40.00", "total": "80.00",
                "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": [],
            }],
        }],
    }
    # The proposed 50.00 does not appear anywhere on the page — must be rejected.
    repair_payload = {
        "diagnosis": "balance it",
        "changes": [{
            "path": "line_items[0].unit_price", "value": "50.00",
            "evidence_quote": "Service unit price: 50.00 EUR",
            "reason": "makes the totals balance",
        }],
    }
    text = "Invoice REP-2. Service unit price: 40.00 EUR; line total 80.00 EUR."
    fake_doc = PDFDocument(path=tmp_path / "REP-2.pdf", pages=[PageContent(1, text, "test", 1.0, None)])
    monkeypatch.setattr(pipeline, "read_pdf", lambda *a, **kw: fake_doc)
    outdir = tmp_path / "output"
    router = RepairingRouter(payload, repair_payload)
    diag = process_one(tmp_path / "REP-2.pdf", outdir, MasterData(ROOT / "master_data"), router, load_settings())
    result = json.loads((outdir / "REP-2.json").read_text(encoding="utf-8"))
    assert result["payables"][0]["line_items"][0]["unit_price"] == "40.00"
    assert diag["payables"][0]["validation"]["status"] == "MISMATCH"
    # One bounded repair per attempt (an ungrounded change never loops within an
    # attempt); the deterministic completeness re-read adds exactly one more
    # attempt, and the better-scoring attempt is kept.
    assert router.repair_calls == 2
    assert diag.get("completeness_reextract") == "attempted (kept the better attempt)"


def test_validate_output_reports_missing_files_without_api(tmp_path):
    from payable_ai.pipeline import validate_outputs
    inputs = tmp_path / "documents"
    outputs = tmp_path / "output"
    logs = tmp_path / "logs"
    inputs.mkdir(); outputs.mkdir(); logs.mkdir()
    (inputs / "INV-01.pdf").write_bytes(b"not parsed by validator")
    result = validate_outputs(inputs, outputs, logs)
    assert result["missing_outputs"] == 1
    assert result["ready_for_manual_review"] is False
