from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from erp import erp_book
from payable_ai.master_data import MasterData
from payable_ai.normalization import normalize_decimal, normalize_payable
from payable_ai.validation import reconcile, shape_errors, apply_grounded_repairs


def test_locale_number_normalization():
    assert normalize_decimal("1.234,56") == "1234.56"
    assert normalize_decimal("1,234.56") == "1234.56"
    assert normalize_decimal("€ 438,00") == "438.00"
    assert normalize_decimal("(19,50)") == "-19.50"
    assert normalize_decimal("4", money=False) == "4"
    assert normalize_decimal("8,1", money=False) == "8.1"


def test_sample_autodraft_is_shape_valid_and_reconciles():
    import json
    masters = MasterData(ROOT / "master_data")
    raw = json.loads((ROOT / "sample_autodraft.json").read_text(encoding="utf-8"))
    normalized, diagnostics = normalize_payable(raw, masters)
    assert shape_errors(normalized) == []
    assert erp_book(normalized)["will_book_gross"] == 438.0
    assert reconcile(normalized)["status"] == "MATCH"
    assert normalized["supplier"]["supplier_id"] == "2845695"


def test_master_match_does_not_guess_supplier():
    masters = MasterData(ROOT / "master_data")
    supplier_id, why = masters.resolve_supplier({"name": "Completely Unknown Supplier", "vat_id": "", "country": "US"})
    assert supplier_id == ""
    assert "no unambiguous" in why


def test_repair_requires_source_grounding():
    raw = {"line_items": [{"unit_price": "10.00"}]}
    bad = {"changes": [{"path": "line_items[0].unit_price", "value": "12.00", "evidence_quote": "New unit price 12.00 EUR", "reason": "printed"}]}
    fixed, logs = apply_grounded_repairs(raw, bad, "Invoice line: unit price 10.00 EUR. Total 10.00 EUR.")
    assert fixed["line_items"][0]["unit_price"] == "10.00"
    assert logs and logs[0]["accepted"] is False


def test_repair_accepts_verified_evidence():
    raw = {"line_items": [{"unit_price": "10.00"}]}
    repair = {"changes": [{"path": "line_items[0].unit_price", "value": "12.00", "evidence_quote": "Service unit price: 12.00 EUR", "reason": "the source line clearly says 12.00"}]}
    source = "Invoice line item; Service unit price: 12.00 EUR; line amount 12.00 EUR."
    fixed, logs = apply_grounded_repairs(raw, repair, source)
    assert fixed["line_items"][0]["unit_price"] == "12.00"
    assert logs[0]["accepted"] is True


def test_date_normalization_is_conservative():
    from payable_ai.normalization import normalize_iso_date
    assert normalize_iso_date("2026-02-02") == "2026-02-02"
    assert normalize_iso_date("02.02.2026") == "2026-02-02"
    assert normalize_iso_date("13/02/2026") == "2026-02-13"
    assert normalize_iso_date("02/03/2026") == ""  # ambiguous: don't guess locale


def test_repair_rejects_amount_from_wrong_accounting_context():
    raw = {"line_items": [{"unit_price": "10.00"}]}
    repair = {"changes": [{"path": "line_items[0].unit_price", "value": "12.00", "evidence_quote": "Grand total due 12.00 EUR", "reason": "found total"}]}
    source = "Invoice line item unit price 10.00 EUR. Grand total due 12.00 EUR."
    fixed, logs = apply_grounded_repairs(raw, repair, source)
    assert fixed["line_items"][0]["unit_price"] == "10.00"
    assert logs[0]["accepted"] is False


def test_repair_cannot_change_printed_gross_target():
    raw = {"gross_total": "100.00"}
    repair = {"changes": [{"path": "gross_total", "value": "90.00", "evidence_quote": "Grand total due 90.00 EUR", "reason": "balance it"}]}
    fixed, logs = apply_grounded_repairs(raw, repair, "Grand total due 90.00 EUR")
    assert fixed["gross_total"] == "100.00"
    assert logs[0]["accepted"] is False


def test_component_resolver_fixes_locale_quantity_misreads():
    from payable_ai.validation import resolve_line_components
    # Portuguese style: quantity misread as 24000; printed total + price + 51% discount pin it to 24.
    payable = {"taxes": [], "line_items": [
        {"description": "SICAL", "quantity": "24000", "unit_price": "34.69", "total": "407.95",
         "discount": "", "discount_percentage": "51", "tax_rate": "", "tax_amount": "", "taxes": []},
    ]}
    resolved, changes = resolve_line_components(payable)
    assert resolved["line_items"][0]["quantity"] == "24"
    assert changes and changes[0]["kind"] == "LINE_QUANTITY_RESOLVED"


def test_component_resolver_fixes_pack_scaled_quantity():
    from payable_ai.validation import resolve_line_components
    payable = {"taxes": [], "line_items": [
        {"description": "PU item", "quantity": "100", "unit_price": "771.66", "total": "771.66",
         "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
    ]}
    resolved, changes = resolve_line_components(payable)
    assert resolved["line_items"][0]["quantity"] == "1"


def test_component_resolver_never_touches_tax_inclusive_totals():
    from payable_ai.validation import resolve_line_components
    # Net price 0.3252 x 400 = 130.08, but printed total 160.00 is inclusive of 23% VAT.
    payable = {"taxes": [{"tax_type": "IVA", "tax_name": "IVA", "tax_rate": "23", "tax_amount": "32.91", "tax_type_code": ""}],
               "line_items": [
                   {"description": "print 400p", "quantity": "400", "unit_price": "0.3252", "total": "160.00",
                    "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
               ]}
    resolved, changes = resolve_line_components(payable)
    assert resolved["line_items"][0]["quantity"] == "400"
    assert changes == []


def test_duplicate_tax_resolver_clears_line_copies_when_exact():
    from payable_ai.validation import apply_deterministic_repairs
    # Line rate 20 duplicates the header VAT; clearing the line copies lands exactly on the printed gross.
    payable = {
        "currency": "GBP", "gross_total": "5076.17", "total_tax_amount": "846.03",
        "discount_amount": "", "freight_charges": "", "insurance_charges": "", "extra_charges": "", "excise_duties": "",
        "taxes": [{"tax_type": "VAT", "tax_name": "VAT", "tax_rate": "20", "tax_amount": "846.03", "tax_type_code": ""}],
        "line_items": [
            {"description": "a", "quantity": "6", "unit_price": "457.24", "total": "2743.44",
             "discount": "", "discount_percentage": "", "tax_rate": "20", "tax_amount": "", "taxes": []},
            {"description": "b", "quantity": "2", "unit_price": "743.35", "total": "1486.70",
             "discount": "", "discount_percentage": "", "tax_rate": "20", "tax_amount": "", "taxes": []},
        ],
    }
    resolved, changes = apply_deterministic_repairs(payable)
    assert all(line["tax_rate"] == "" and line["taxes"] == [] for line in resolved["line_items"])
    assert resolved["taxes"][0]["tax_amount"] == "846.03"
    assert any(c["kind"] == "DUPLICATE_TAX_LEVEL_CLEARED" for c in changes)


def test_duplicate_tax_resolver_never_fires_without_exact_match():
    from payable_ai.validation import resolve_duplicate_taxes
    # Clearing the line rate does NOT reproduce the printed gross (the doc genuinely charges both).
    payable = {
        "currency": "GBP", "gross_total": "5076.17",
        "discount_amount": "", "freight_charges": "", "insurance_charges": "", "extra_charges": "", "excise_duties": "",
        "taxes": [{"tax_type": "VAT", "tax_name": "VAT", "tax_rate": "20", "tax_amount": "846.03", "tax_type_code": ""}],
        "line_items": [
            {"description": "a", "quantity": "6", "unit_price": "457.24", "total": "2743.44",
             "discount": "", "discount_percentage": "", "tax_rate": "20", "tax_amount": "", "taxes": []},
        ],
    }
    resolved, changes = resolve_duplicate_taxes(payable)
    assert resolved["line_items"][0]["tax_rate"] == "20"  # untouched
    assert changes == []


def test_printed_formula_tax_snaps_to_exact_product():
    from payable_ai.validation import recompute_printed_formula_taxes
    # Utility-bill pattern: taxes displayed rounded (105.60 / 7.02) but the printed net
    # is computed from the unrounded products (105.597 / 7.015) of the printed rate 0.0575.
    payable = {
        "currency": "USD", "gross_total": "2487.73", "total_tax_amount": "112.62",
        "discount_amount": "", "freight_charges": "", "insurance_charges": "", "extra_charges": "", "excise_duties": "",
        "taxes": [
            {"tax_type": "TAX", "tax_name": "Sales tax at 0.0575 on utility bills", "tax_rate": "5.75", "tax_amount": "105.60", "tax_type_code": ""},
            {"tax_type": "TAX", "tax_name": "Sales tax at 0.0575 on building services", "tax_rate": "5.75", "tax_amount": "7.02", "tax_type_code": ""},
        ],
        "line_items": [
            {"description": "electric", "quantity": "1", "unit_price": "1570.37", "total": "1570.37", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
            {"description": "demand", "quantity": "1", "unit_price": "266.10", "total": "266.10", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
            {"description": "generator", "quantity": "1", "unit_price": "0.00", "total": "0.00", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
            {"description": "energy", "quantity": "1", "unit_price": "393.47", "total": "393.47", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
            {"description": "pm", "quantity": "1", "unit_price": "23.18", "total": "23.18", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
            {"description": "building", "quantity": "1", "unit_price": "122.00", "total": "122.00", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
        ],
    }
    resolved, changes = recompute_printed_formula_taxes(payable)
    amounts = [t["tax_amount"] for t in resolved["taxes"]]
    # Least uniform precision that reproduces the printed net exactly (3 decimals).
    assert amounts == ["105.597", "7.015"]
    assert any(c["kind"] == "PRINTED_FORMULA_TAX_SNAPPED" for c in changes)
    from erp import erp_book
    assert round(erp_book(resolved)["will_book_gross"], 2) == 2487.73


def test_printed_formula_precision_hygiene_on_already_exact_record():
    from payable_ai.validation import recompute_printed_formula_taxes
    # The record already books exactly, but one amount carries six decimals;
    # precision hygiene trims both formula-derived amounts to a uniform level.
    payable = {
        "currency": "USD", "gross_total": "2487.73", "total_tax_amount": "112.62",
        "discount_amount": "", "freight_charges": "", "insurance_charges": "", "extra_charges": "", "excise_duties": "",
        "taxes": [
            {"tax_type": "TAX", "tax_name": "Sales tax", "tax_rate": "5.75", "tax_amount": "105.597025", "tax_type_code": ""},
            {"tax_type": "TAX", "tax_name": "Sales tax", "tax_rate": "5.75", "tax_amount": "7.015", "tax_type_code": ""},
        ],
        "line_items": [
            {"description": "electric", "quantity": "1", "unit_price": "1570.37", "total": "1570.37", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
            {"description": "demand", "quantity": "1", "unit_price": "266.10", "total": "266.10", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
            {"description": "energy", "quantity": "1", "unit_price": "393.47", "total": "393.47", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
            {"description": "pm", "quantity": "1", "unit_price": "23.18", "total": "23.18", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
            {"description": "building", "quantity": "1", "unit_price": "122.00", "total": "122.00", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
        ],
    }
    resolved, changes = recompute_printed_formula_taxes(payable)
    assert [t["tax_amount"] for t in resolved["taxes"]] == ["105.597", "7.015"]
    from erp import erp_book
    assert round(erp_book(resolved)["will_book_gross"], 2) == 2487.73


def test_legacy_currency_codes_map_to_current_iso():
    from payable_ai.normalization import normalize_currency_code, normalize_payable
    from payable_ai.master_data import MasterData
    assert normalize_currency_code("GHC") == "GHS"
    assert normalize_currency_code("ghc") == "GHS"
    assert normalize_currency_code("GH₵") == "GHS"
    assert normalize_currency_code("EUR") == "EUR"
    assert normalize_currency_code("") == ""
    normalized, _ = normalize_payable({"currency": "GHC"}, MasterData(ROOT / "master_data"))
    assert normalized["currency"] == "GHS"


def test_quality_flags_declared_tax_that_no_component_represents():
    from payable_ai.quality import semantic_issues
    embedded = {
        "invoice_type": "INVOICE", "currency": "ZAR", "invoice_number": "X-1", "gross_total": "1683.98",
        "subtotal": "1633.98", "total_tax_amount": "169.83", "discount_amount": "", "freight_charges": "50.00",
        "insurance_charges": "", "extra_charges": "", "excise_duties": "",
        "taxes": [{"tax_type": "VAT", "tax_name": "VAT", "tax_rate": "", "tax_amount": "", "tax_type_code": ""}],
        "line_items": [{"description": "x", "quantity": "1", "unit_price": "44.99", "total": "44.99",
                        "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []}],
    }
    codes = {issue["code"] for issue in semantic_issues(embedded)}
    assert "DECLARED_TAX_NOT_REPRESENTED" in codes

    represented = dict(embedded)
    represented["taxes"] = [{"tax_type": "VAT", "tax_name": "VAT", "tax_rate": "15", "tax_amount": "169.83", "tax_type_code": ""}]
    codes = {issue["code"] for issue in semantic_issues(represented)}
    assert "DECLARED_TAX_NOT_REPRESENTED" not in codes


def test_repair_rejects_changes_to_irrelevant_fields():
    from payable_ai.validation import apply_grounded_repairs
    raw = {"invoice_number": "A-1", "line_items": [{"unit_price": "10.00"}]}
    repair = {"changes": [
        {"path": "invoice_number", "value": "B-2", "evidence_quote": "Invoice number B-2 printed", "reason": "typo"},
        {"path": "line_items[0].taxes[0].tax_rate", "value": "19", "evidence_quote": "VAT 19%", "reason": "irrelevant"},
    ]}
    source = "Invoice number B-2 printed. VAT 19%."
    fixed, logs = apply_grounded_repairs(raw, repair, source)
    assert fixed["invoice_number"] == "A-1"  # not an ERP input: never editable
    assert all(entry["accepted"] is False for entry in logs)


def test_quality_accepts_printed_extension_before_or_after_discount():
    from payable_ai.quality import semantic_issues

    def codes(payable):
        return {issue["code"] for issue in semantic_issues(payable)}

    base = {
        "invoice_type": "INVOICE", "currency": "EUR", "invoice_number": "A-1", "gross_total": "271.11",
        "subtotal": "", "total_tax_amount": "", "discount_amount": "", "freight_charges": "",
        "insurance_charges": "", "extra_charges": "", "excise_duties": "", "taxes": [],
    }
    # Pre-discount extension printed (59.040 x ...): qty x price equals the printed total.
    pre = dict(base, line_items=[{"description": "x", "quantity": "40", "unit_price": "14.76", "total": "590.40",
                                  "discount": "", "discount_percentage": "54.080", "tax_rate": "", "tax_amount": "", "taxes": []}])
    assert "LINE_EXTENSION_MISMATCH" not in codes(pre)
    # Post-discount extension printed: the ERP's net base equals the printed total.
    post = dict(base, line_items=[{"description": "x", "quantity": "1", "unit_price": "407.95", "total": "407.95",
                                   "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []}])
    assert "LINE_EXTENSION_MISMATCH" not in codes(post)
    # Neither extension matches: flagged.
    wrong = dict(base, line_items=[{"description": "x", "quantity": "2", "unit_price": "40.00", "total": "100.00",
                                    "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []}])
    assert "LINE_EXTENSION_MISMATCH" in codes(wrong)


def test_hld05_faithful_representation_books_exactly():
    """Source facts verified against the two pages of HLD-05.pdf.

    The document prints, per goods line, a real quantity, a net unit price and a
    promotional discount percentage; the totals block prints the VAT amounts per
    rate (19.82 at 13%, 157.05 at 23%), the IEC excise total (88.33) and two
    untaxed container deposits (162 x 0.10; 96 x 0.10). Submitting those source
    values — quantities and unit prices, taxes once at the header with the
    printed amounts, the printed excise as excise_duties, deposits as untaxed
    lines — reproduces the owed amount (sub-total incl. VAT 1,012.14 + deposits
    25.80 = 1,037.94) through the unchanged ERP.
    """
    from erp import erp_book

    goods = [
        ("SAGRES Branca Gfa Tab 6x1L PT", "40", "14.76", "54.080"),
        ("HEINEKEN Barril OW 5L PT", "6", "24.53", "61.240"),
        ("SAGRES Branca Gfa Tab 4(6x25cl) PT 201a", "1", "24.96", "63.500"),
        ("SAGRES Branca Gfa Cxa 20x25cl PT 201a", "9", "20.80", "66.290"),
        ("CASTELLO Original Gfa SW 4(6x25cl) 404a", "7", "20.40", "60.530"),
        ("LUSO PET SW 6x1,5L SDR", "27", "6.72", "65.110"),
        ("LUSO PET SW 4(6x50cl) SDR", "4", "14.64", "44.040"),
        ("DECVAKUGZK Gfa Cxa 4(6x33cl) ES", "1", "48.00", "56.600"),
        ("HEINEKEN Gfa Cxa 18x25cl PT", "4", "18.72", "57.150"),
        ("HEINEKEN Gfa Tab 4(6x25cl) PT", "7", "24.96", "53.930"),
        ("SAGRES Branca Gfa SW 2(10x25cl) PT 201a", "8", "20.80", "63.510"),
    ]
    lines = [
        {"description": desc, "item_type": "GOODS", "uom": "", "quantity": qty,
         "unit_price": price, "total": "", "discount": "", "discount_percentage": pct,
         "tax_rate": "", "tax_amount": "", "taxes": []}
        for desc, qty, price, pct in goods
    ]
    lines.extend([
        {"description": "Depósito SDR", "item_type": "GOODS", "uom": "UND", "quantity": "162",
         "unit_price": "0.10", "total": "", "discount": "", "discount_percentage": "",
         "tax_rate": "0", "tax_amount": "", "taxes": []},
        {"description": "Depósito SDR", "item_type": "GOODS", "uom": "UND", "quantity": "96",
         "unit_price": "0.10", "total": "", "discount": "", "discount_percentage": "",
         "tax_rate": "0", "tax_amount": "", "taxes": []},
    ])
    payable = {
        "currency": "EUR", "gross_total": "1037.94", "subtotal": "835.27", "total_tax_amount": "176.87",
        "discount_amount": "", "freight_charges": "", "insurance_charges": "",
        "extra_charges": "", "excise_duties": "88.33",
        "taxes": [
            {"tax_type": "VAT", "tax_name": "IVA 13%", "tax_rate": "13", "tax_amount": "19.82", "tax_type_code": ""},
            {"tax_type": "VAT", "tax_name": "IVA 23%", "tax_rate": "23", "tax_amount": "157.05", "tax_type_code": ""},
        ],
        "line_items": lines,
    }
    assert round(erp_book(payable)["will_book_gross"], 2) == 1037.94
    assert payable["excise_duties"] == "88.33"
    assert lines[0]["quantity"] == "40" and lines[0]["unit_price"] == "14.76"


def test_component_resolver_fills_empty_quantity_from_exact_quotient():
    from payable_ai.validation import resolve_line_components
    payable = {"taxes": [], "line_items": [
        {"description": "P2H-AT", "quantity": "", "unit_price": "8.18", "total": "4499.00",
         "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
    ]}
    resolved, changes = resolve_line_components(payable)
    assert resolved["line_items"][0]["quantity"] == "550"
    assert changes and changes[0]["after"] == "550"


def test_component_resolver_skips_ambiguous_division():
    from payable_ai.validation import resolve_line_components
    payable = {"taxes": [], "line_items": [
        {"description": "odd", "quantity": "7", "unit_price": "3.33", "total": "10.00",
         "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []},
    ]}
    resolved, changes = resolve_line_components(payable)
    assert resolved["line_items"][0]["quantity"] == "7"
    assert changes == []


def test_repair_supports_grounded_clear_operations():
    raw = {"taxes": [{"tax_rate": "20", "tax_amount": "100.00"}], "line_items": [{"unit_price": "100", "tax_rate": "20"}]}
    repair = {"changes": [
        {"path": "taxes[0].tax_rate", "value": "", "evidence_quote": "Total VAT 20% 100.00", "reason": "duplicated header tax"},
        {"path": "taxes[0].tax_amount", "value": "", "evidence_quote": "Total VAT 20% 100.00", "reason": "duplicated header tax"},
        {"path": "line_items[0].tax_rate", "value": "", "evidence_quote": "VAT rate 20% applied at line", "reason": "moved to header"},
    ]}
    source = "Total VAT 20% 100.00. VAT rate 20% applied at line. line total 100.00"
    fixed, logs = apply_grounded_repairs(raw, repair, source)
    accepted = [l for l in logs if l.get("accepted")]
    assert len(accepted) == 3
    assert all(l.get("cleared") for l in accepted)
    assert fixed["taxes"][0]["tax_rate"] == "" and fixed["taxes"][0]["tax_amount"] == ""
    assert fixed["line_items"][0]["tax_rate"] == ""


def test_repair_clear_requires_quote_on_page():
    raw = {"taxes": [{"tax_rate": "20", "tax_amount": "100.00"}]}
    repair = {"changes": [
        {"path": "taxes[0].tax_rate", "value": "", "evidence_quote": "Total VAT 19% 115.00", "reason": "not on page"},
    ]}
    source = "Total VAT 20% 100.00"
    fixed, logs = apply_grounded_repairs(raw, repair, source)
    assert fixed["taxes"][0]["tax_rate"] == "20"
    assert logs[0]["accepted"] is False


def test_repair_rejects_unit_price_when_value_is_only_another_field():
    raw = {"line_items": [{"unit_price": "10.00"}]}
    repair = {"changes": [{"path": "line_items[0].unit_price", "value": "2", "evidence_quote": "Unit price 10.00 EUR; quantity 2; line total 20.00 EUR", "reason": "model selected quantity by mistake"}]}
    fixed, logs = apply_grounded_repairs(raw, repair, "Unit price 10.00 EUR; quantity 2; line total 20.00 EUR")
    assert fixed["line_items"][0]["unit_price"] == "10.00"
    assert logs[0]["accepted"] is False


def test_quality_checks_flag_line_component_mismatch():
    from payable_ai.quality import semantic_issues
    payable = {
        "invoice_type": "INVOICE", "currency": "EUR", "invoice_number": "A-1", "gross_total": "120.00",
        "subtotal": "100.00", "total_tax_amount": "20.00", "discount_amount": "", "freight_charges": "",
        "insurance_charges": "", "extra_charges": "", "excise_duties": "", "taxes": [],
        "line_items": [{"description": "Service", "quantity": "2", "unit_price": "40.00", "total": "100.00", "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []}],
    }
    issues = semantic_issues(payable)
    assert any(issue["code"] == "LINE_EXTENSION_MISMATCH" for issue in issues)


def test_page_aware_trim_preserves_middle_page_labels():
    from payable_ai.pdf_io import trim_document_text
    raw = "\n\n".join(f"--- PAGE {i} (native_text) ---\n" + (f"body-{i} " * 500) for i in range(1, 7))
    trimmed = trim_document_text(raw, 2400)
    for i in range(1, 7):
        assert f"--- PAGE {i} (native_text) ---" in trimmed


def test_groq_text_route_fails_closed_when_ocr_remains_incomplete(monkeypatch, tmp_path):
    from payable_ai.config import load_settings
    from payable_ai.llm import LLMRouter
    from payable_ai.pdf_io import PDFDocument, PageContent

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    router = LLMRouter(load_settings("groq"))
    # Don't make a network call. Even if OCR is attempted, page source remains image-only.
    monkeypatch.setattr("payable_ai.llm.ensure_full_ocr", lambda _doc: None)
    router._groq = object()
    monkeypatch.setattr(router, "ensure_available", lambda: None)
    monkeypatch.setattr(router, "available", lambda: ["groq"])
    monkeypatch.setattr(router, "_order", lambda _doc: ["groq"])
    monkeypatch.setattr(router, "_call", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not call a text LLM with incomplete OCR")))
    doc = PDFDocument(path=tmp_path / "scanned.pdf", pages=[
        PageContent(1, "", "image_only", None, b"fake-image"),
        PageContent(2, "", "image_only", None, b"fake-image"),
        PageContent(3, "Some text", "native_text", None, None),
    ])
    try:
        router.extract(doc, "scanned.pdf")
    except RuntimeError as exc:
        assert "OCR remains incomplete" in str(exc)
    else:
        raise AssertionError("Expected Groq route to fail closed")
