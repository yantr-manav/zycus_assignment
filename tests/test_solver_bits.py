from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from payable_ai.keys import KeyPool, parse_key_pool, redact, key_label
from payable_ai.signals import build_signals, erp_trace


def test_key_pool_parsing_supports_mixed_formats_and_separators():
    pool = parse_key_pool("AIza_FAKE_1,AQ.FAKE_2;gsk_fake3  AIza_FAKE_1", "AQ.FAKE_4")
    assert pool[0] == "AIza_FAKE_1"
    assert pool == ["AIza_FAKE_1", "AQ.FAKE_2", "gsk_fake3", "AQ.FAKE_4"]


def test_redaction_never_leaks_key_material():
    secret = "AIzaFAKEFORREDACTOINTESTONLY0000000000"
    redacted = redact(f"request failed with key {secret} at URL")
    assert secret not in redacted
    assert "[REDACTED]" in redacted
    assert key_label(secret).startswith("AIza")


def test_key_pool_marks_invalid_dead_and_cools_quota():
    pool = KeyPool("gemini", ["key-a", "key-b", "key-c"])
    pool.note_failure("key-a", KeyPool.INVALID)
    assert pool.next_key() == "key-b"
    pool.note_failure("key-b", KeyPool.QUOTA, retry_after=0.1)
    assert pool.next_key() == "key-c"
    status = {row["key"]: row["state"] for row in pool.status()}
    assert status[key_label("key-a")] == "invalid"
    assert status[key_label("key-b")] == "cooling-down"
    # never returns a dead key
    assert all(row["state"] != "ready" or row["key"] != key_label("key-a") for row in pool.status())


def test_key_pool_returns_none_when_all_invalid():
    pool = KeyPool("groq", ["only-key"])
    pool.note_failure("only-key", KeyPool.INVALID)
    assert pool.next_key() is None


def test_long_documents_split_by_page_and_merge_payables():
    from payable_ai.pdf_io import split_document_text
    from payable_ai.llm import merge_extraction_parts

    text = "\n\n".join(f"--- PAGE {i} (native_text) ---\n" + (f"line {i} amount {i}.00 " * 60) for i in range(1, 8))
    parts = split_document_text(text, 2000)
    assert len(parts) > 1
    for part in parts:
        assert len(part) <= 2200
    # every page label appears in exactly one part
    joined = "\n".join(parts)
    for i in range(1, 8):
        assert joined.count(f"--- PAGE {i} (native_text) ---") == 1

    merged = merge_extraction_parts([
        {"document_type": "OTHER", "is_payable": False, "payables": [],
         "classification_reason": "annex", "declined_reason": "not payable"},
        {"document_type": "INVOICE", "is_payable": True, "payables": [
            {"invoice_number": "A-1", "line_items": [{"description": "x"}]}],
         "classification_reason": "invoice found", "declined_reason": ""},
        {"document_type": "INVOICE", "is_payable": True, "payables": [
            {"invoice_number": "A-1", "line_items": [{"description": "x"}, {"description": "y"}]},
            {"invoice_number": "", "line_items": [{"description": "z"}]}],
         "classification_reason": "", "declined_reason": ""},
    ])
    assert merged["is_payable"] is True
    assert merged["document_type"] == "INVOICE"
    assert len(merged["payables"]) == 2
    a1 = next(p for p in merged["payables"] if p["invoice_number"] == "A-1")
    assert len(a1["line_items"]) == 2  # the more detailed duplicate won
    assert merged["declined_reason"] == ""


def test_missing_rows_delta_detects_incomplete_line_sums():
    from payable_ai.signals import missing_rows_delta

    incomplete = {
        "gross_total": "873.53", "subtotal": "873.53", "total_tax_amount": "0.00", "taxes": [],
        "line_items": [{"total": "28.40"}, {"total": "21.90"}],
    }
    gap = missing_rows_delta(incomplete)
    assert gap is not None and gap > 100

    complete = {**incomplete, "line_items": [{"total": "873.53"}]}
    assert missing_rows_delta(complete) is None

    tax_separate = {
        "gross_total": "123.00", "subtotal": "", "total_tax_amount": "23.00", "taxes": [],
        "line_items": [{"total": "100.00"}],
    }
    assert missing_rows_delta(tax_separate) is None

    header_tax_separate = {
        "gross_total": "123.00", "subtotal": "", "total_tax_amount": "",
        "taxes": [{"tax_rate": "23", "tax_amount": "23.00"}],
        "line_items": [{"total": "100.00"}],
    }
    assert missing_rows_delta(header_tax_separate) is None


def test_provider_error_classification_is_conservative():
    from payable_ai.llm import classify_provider_error

    class FakeExc(Exception):
        def __init__(self, status, message="boom"):
            super().__init__(message)
            self.status_code = status

    assert classify_provider_error(FakeExc(413, "Request too large for model"))[0] == KeyPool.REQUEST
    assert classify_provider_error(FakeExc(429, "Rate limit reached"))[0] == KeyPool.QUOTA
    assert classify_provider_error(Exception("API key not valid. Please pass a valid API key."))[0] == KeyPool.INVALID
    # A generic 400 must NOT kill a healthy key.
    assert classify_provider_error(FakeExc(400, "something odd"))[0] == KeyPool.TRANSIENT
    assert classify_provider_error(FakeExc(500, "internal"))[0] == KeyPool.TRANSIENT


def test_daily_quota_recognized_from_retry_delay_text():
    from payable_ai.llm import classify_provider_error, _is_daily_exhaustion
    exc = Exception('429 quota exceeded ... retryDelay: \'62959s\'')
    kind, retry_after = classify_provider_error(exc)
    assert kind == KeyPool.QUOTA
    assert retry_after and retry_after > 60000
    assert _is_daily_exhaustion(str(exc), retry_after) is True
    exc2 = Exception("Rate limit reached ... try again in 12.5s")
    kind2, retry2 = classify_provider_error(exc2)
    assert kind2 == KeyPool.QUOTA
    assert retry2 is not None and retry2 < 30


def test_key_pool_wait_cap_does_not_sleep_through_daily_quota():
    import time as _time
    pool = KeyPool("gemini", ["key-aaaa", "key-bbbb"])
    pool.note_failure("key-aaaa", KeyPool.QUOTA, retry_after=3600)
    pool.note_failure("key-bbbb", KeyPool.QUOTA, retry_after=3600)
    start = _time.monotonic()
    assert pool.next_key(wait=True, max_wait=5.0) is None
    assert _time.monotonic() - start < 5.0


def test_erp_trace_matches_sealed_calculator():
    payable = json.loads((ROOT / "sample_autodraft.json").read_text(encoding="utf-8"))
    trace = erp_trace(payable)
    assert trace is not None
    assert float(trace["erp_gross"]) == 438.0
    assert trace["header_taxes_sum"] == "0.00"


def test_signals_flag_line_base_vs_printed_total():
    payable = {
        "currency": "EUR", "gross_total": "100.00", "total_tax_amount": "",
        "discount_amount": "", "freight_charges": "", "insurance_charges": "", "extra_charges": "", "excise_duties": "",
        "taxes": [],
        "line_items": [{"description": "Widget", "quantity": "2", "unit_price": "40.00", "total": "100.00",
                        "discount": "", "discount_percentage": "", "tax_rate": "", "tax_amount": "", "taxes": []}],
    }
    from payable_ai.validation import reconcile
    validation = reconcile(payable)
    signals = build_signals(payable, validation, [])
    kinds = {s["kind"] for s in signals}
    assert "GROSS_DELTA" in kinds
    assert "LINE_BASE_VS_PRINTED_TOTAL" in kinds
    line_signal = next(s for s in signals if s["kind"] == "LINE_BASE_VS_PRINTED_TOTAL")
    assert line_signal["field_path"] == "line_items[0]"
    # The signal states facts; it must not prescribe a remedy.
    assert "should" not in line_signal["message"].lower()


def test_erp_trace_withheld_when_mirror_disagrees(monkeypatch):
    from payable_ai import signals as signals_module
    payable = json.loads((ROOT / "sample_autodraft.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(signals_module, "erp_book", lambda _p: {"will_book_gross": 123.45})
    assert signals_module.erp_trace(payable) is None
