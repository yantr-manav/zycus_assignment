"""Deterministic quality checks that complement the exact ERP oracle.

These checks are diagnostics, not a substitute for source review. They deliberately
avoid auto-correcting financial values based on arithmetic alone.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .normalization import normalize_decimal

MONEY_FIELDS = (
    "gross_total", "subtotal", "total_tax_amount", "discount_amount",
    "freight_charges", "insurance_charges", "extra_charges", "excise_duties",
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    normalized = normalize_decimal(value)
    if not normalized:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def semantic_issues(payable: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def add(code: str, severity: str, message: str) -> None:
        issues.append({"code": code, "severity": severity, "message": message})

    if payable.get("invoice_type") not in {"INVOICE", "CREDIT_MEMO"}:
        add("INVALID_INVOICE_TYPE", "error", "invoice_type must be INVOICE or CREDIT_MEMO")
    currency = str(payable.get("currency", "")).strip()
    if currency and (len(currency) != 3 or not currency.isalpha() or currency != currency.upper()):
        add("INVALID_CURRENCY", "warning", f"Currency {currency!r} is not a normalized 3-letter code")
    if not str(payable.get("invoice_number", "")).strip():
        add("MISSING_INVOICE_NUMBER", "warning", "Invoice number is missing; verify the source page")
    if not currency:
        add("MISSING_CURRENCY", "warning", "Currency is missing; ERP reconciliation is not enough to infer it")
    if not str(payable.get("gross_total", "")).strip():
        add("MISSING_GROSS_TOTAL", "error", "Printed gross total is missing; accounting reconciliation cannot be verified")

    for field in MONEY_FIELDS:
        value = str(payable.get(field, "")).strip()
        if value and _decimal(value) is None:
            add("INVALID_NUMERIC", "error", f"{field} is not a normalized numeric value")

    taxes = payable.get("taxes") if isinstance(payable.get("taxes"), list) else []
    lines = payable.get("line_items") if isinstance(payable.get("line_items"), list) else []
    tax_amount_total = Decimal("0")
    tax_total_known = True
    for index, tax in enumerate(taxes):
        if not isinstance(tax, dict):
            continue
        rate = str(tax.get("tax_rate", "")).strip()
        amount = str(tax.get("tax_amount", "")).strip()
        if rate and _decimal(rate) is None:
            add("INVALID_TAX_RATE", "error", f"taxes[{index}].tax_rate is not numeric")
        if amount:
            parsed = _decimal(amount)
            if parsed is None:
                add("INVALID_TAX_AMOUNT", "error", f"taxes[{index}].tax_amount is not numeric")
                tax_total_known = False
            else:
                tax_amount_total += parsed
        else:
            tax_total_known = False

    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        for field in ("quantity", "unit_price", "total", "discount", "discount_percentage", "tax_rate", "tax_amount"):
            value = str(line.get(field, "")).strip()
            if value and _decimal(value) is None:
                add("INVALID_NUMERIC", "error", f"line_items[{index}].{field} is not numeric")
        quantity = _decimal(line.get("quantity"))
        price = _decimal(line.get("unit_price"))
        printed_line_total = _decimal(line.get("total"))
        discount_pct = _decimal(line.get("discount_percentage"))
        discount_amt = _decimal(line.get("discount"))
        if not str(line.get("description", "")).strip():
            add("MISSING_LINE_DESCRIPTION", "warning", f"line_items[{index}] has no description")
        if quantity is None or price is None:
            add("INCOMPLETE_LINE_COMPONENTS", "warning", f"line_items[{index}] lacks quantity or net unit_price; the ERP base may be undercounted")
        elif printed_line_total is not None:
            gross_extension = quantity * price
            net_extension = gross_extension
            discounted = False
            if discount_pct is not None and discount_pct > 0:
                net_extension = (gross_extension - gross_extension * discount_pct / Decimal("100")).quantize(Decimal("0.01"))
                discounted = True
            elif discount_amt is not None and discount_amt > 0 and quantity != 0:
                net_extension = (price - discount_amt / abs(quantity)) * quantity
                discounted = True
            matches = abs(net_extension - printed_line_total) <= Decimal("0.02")
            if not matches and discounted:
                # Some documents print the row extension before the discount is
                # applied; the discount column then explains the ERP's net base.
                matches = abs(gross_extension - printed_line_total) <= Decimal("0.02")
            if not matches:
                add(
                    "LINE_EXTENSION_MISMATCH", "warning",
                    f"line_items[{index}] quantity × net unit price minus discount differs from printed total; inspect whether line total includes tax or has different semantics",
                )

    # Gross can match while taxes are duplicated or placed on both the line and header.
    header_tax_signatures = {
        (str(t.get("tax_type", "")).casefold().strip(), str(t.get("tax_name", "")).casefold().strip(), str(t.get("tax_rate", "")).strip())
        for t in taxes if isinstance(t, dict)
    }
    for i, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        line_taxes = line.get("taxes") if isinstance(line.get("taxes"), list) else []
        for tax in line_taxes:
            if not isinstance(tax, dict):
                continue
            signature = (str(tax.get("tax_type", "")).casefold().strip(), str(tax.get("tax_name", "")).casefold().strip(), str(tax.get("tax_rate", "")).strip())
            if signature in header_tax_signatures and any(signature):
                add("TAX_AT_BOTH_LEVELS", "warning", f"Tax {signature[1] or signature[0] or signature[2]!r} appears at header and line level; verify it is not duplicated")

    declared_tax = _decimal(payable.get("total_tax_amount"))
    has_line_tax = any(
        isinstance(line, dict) and (
            bool(line.get("taxes")) or bool(str(line.get("tax_rate", "")).strip()) or bool(str(line.get("tax_amount", "")).strip())
        )
        for line in lines
    )
    # A header-only comparison is meaningful only when no line-level taxes exist.
    if declared_tax is not None and tax_total_known and taxes and not has_line_tax and abs(declared_tax - tax_amount_total) > Decimal("0.02"):
        add("DECLARED_TAX_DIFFERENCE", "warning", "Declared total_tax_amount differs from explicit header taxes; verify whether another tax/withholding component is represented elsewhere")

    # The brief's named anti-pattern: prices submitted as if net while the tax is
    # only declared, never represented. The gross may foot (inclusive prices plus
    # no tax), but the record does not show the tax the document charges.
    if declared_tax is not None and declared_tax > 0 and not has_line_tax:
        header_contribution = Decimal("0")
        for tax in taxes:
            if not isinstance(tax, dict):
                continue
            header_contribution += _decimal(tax.get("tax_amount")) or Decimal("0")
            rate_value = _decimal(tax.get("tax_rate"))
            if rate_value and rate_value > 0 and not (tax.get("tax_amount") or "").strip():
                header_contribution += Decimal("1")  # rate-only taxes are derived, so they do represent tax
        if header_contribution == 0:
            add(
                "DECLARED_TAX_NOT_REPRESENTED", "warning",
                "The document declares tax but no submitted tax component represents it (tax may be embedded in line prices); inspect tax placement",
            )

    return issues
