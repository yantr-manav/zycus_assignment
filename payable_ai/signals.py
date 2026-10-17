"""Deterministic signals for the reconcile-repair loop.

The Constitution pattern this module implements: *facts, not instructions*.
Every signal states something provably true about how the sealed ERP computes
the drafted payable and what the document prints — never how to fix it. The
model decides the remedy; the application only verifies grounding and results.

``erp_trace`` re-implements ``erp.py``'s arithmetic **for introspection only**
(the brief explicitly permits this) and refuses to emit a trace unless its
total agrees with the sealed calculator to the cent, so the two can never drift.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from erp import erp_book, num, round2

from .normalization import normalize_decimal


def _decimal(value: Any) -> Decimal | None:
    """Parse a payable number string into a Decimal (or None)."""
    normalized = normalize_decimal(value)
    if not normalized:
        return None
    try:
        return Decimal(normalized)
    except (ArithmeticError, ValueError):
        return None


def _line_base(li: dict[str, Any]) -> float:
    qty = num(li.get("quantity"))
    price = num(li.get("unit_price"))
    disc_pct = abs(num(li.get("discount_percentage")))
    disc_amt = abs(num(li.get("discount")))
    sub_total = qty * price
    if disc_pct > 0:
        return round2(sub_total - (sub_total * disc_pct / 100.0))
    if disc_amt > 0 and qty != 0:
        item_price = price - (disc_amt / abs(qty))
        return round2(item_price * qty)
    return round2(sub_total)


def _line_tax_total(li: dict[str, Any], base: float) -> float:
    taxes = li.get("taxes") or []
    if not taxes and (str(li.get("tax_rate") or "").strip() or str(li.get("tax_amount") or "").strip()):
        taxes = [{"tax_rate": li.get("tax_rate", ""), "tax_amount": li.get("tax_amount", "")}]
    total = 0.0
    for t in taxes:
        if not isinstance(t, dict):
            continue
        rate_str = str(t.get("tax_rate") or "").replace("%", "").strip()
        rate = num(rate_str)
        amt = num(t.get("tax_amount"))
        if amt == 0 and rate > 0:
            amt = round2(base * rate / 100.0)
        if rate == 0 and amt == 0 and rate_str == "":
            continue
        total += amt
    return total


def _header_tax_total(taxes: Any, net_base: float) -> float:
    total = 0.0
    for t in taxes or []:
        if not isinstance(t, dict):
            continue
        rate = num(str(t.get("tax_rate") or "").replace("%", "").strip())
        amt = num(t.get("tax_amount"))
        if amt == 0 and rate > 0:
            amt = round2(net_base * rate / 100.0)
        total += amt
    return total


def _money(value: float) -> str:
    return format(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def erp_trace(payable: dict[str, Any]) -> dict[str, Any] | None:
    """Component-by-component view of what the sealed ERP derives from a draft.

    Returns None when this mirror disagrees with ``erp_book`` on the total —
    the trace is then withheld rather than risking a wrong signal.
    """
    try:
        sealed = float(erp_book(payable)["will_book_gross"])
        lines = []
        line_base_sum = 0.0
        line_tax_sum = 0.0
        for index, li in enumerate(payable.get("line_items") or []):
            if not isinstance(li, dict):
                continue
            base = _line_base(li)
            tax = _line_tax_total(li, base)
            line_base_sum += base
            line_tax_sum += tax
            lines.append({
                "index": index,
                "description": str(li.get("description", "")),
                "printed_total": str(li.get("total", "")),
                "quantity": str(li.get("quantity", "")),
                "unit_price": str(li.get("unit_price", "")),
                "discount": str(li.get("discount", "")),
                "discount_percentage": str(li.get("discount_percentage", "")),
                "erp_line_base": _money(base),
                "erp_line_tax": _money(tax),
            })
        header_discount = abs(num(payable.get("discount_amount")))
        net_base = line_base_sum - header_discount
        header_taxes = []
        for index, t in enumerate(payable.get("taxes") or []):
            if not isinstance(t, dict):
                continue
            rate = num(str(t.get("tax_rate") or "").replace("%", "").strip())
            amt = num(t.get("tax_amount"))
            derived = round2(net_base * rate / 100.0) if (amt == 0 and rate > 0) else amt
            header_taxes.append({
                "index": index,
                "tax_name": str(t.get("tax_name", "")),
                "tax_rate": str(t.get("tax_rate", "")),
                "amount_from_component": str(t.get("tax_amount", "")),
                "erp_contribution": _money(derived),
                "applied_to_base": _money(net_base),
            })
        header_tax_sum = _header_tax_total(payable.get("taxes"), net_base)
        charges = (
            num(payable.get("freight_charges"))
            + num(payable.get("insurance_charges"))
            + num(payable.get("extra_charges"))
            + num(payable.get("excise_duties"))
        )
        mirrored = round2(line_base_sum - header_discount + line_tax_sum + header_tax_sum + charges)
        if abs(mirrored - sealed) > 0.005:
            return None
        return {
            "line_bases_sum": _money(line_base_sum),
            "line_taxes_sum": _money(line_tax_sum),
            "header_discount": _money(header_discount),
            "header_net_base": _money(net_base),
            "header_taxes": header_taxes,
            "header_taxes_sum": _money(header_tax_sum),
            "other_charges_sum": _money(charges),
            "lines": lines,
            "erp_gross": _money(sealed),
        }
    except Exception:
        return None


def missing_rows_delta(payable: dict[str, Any]) -> float | None:
    """Gap between the extracted line sums and every printed total candidate.

    Returns the gap (a positive float) when the extracted ``total`` column
    matches NONE of the document's printed totals — the printed subtotal, the
    printed gross, the gross minus the declared tax, or the gross minus the
    supplied header tax amounts. A draft that matches none of them has lost or
    mis-transcribed rows; the caller drives one corrective re-read. Returns
    None when any printed total is reproduced (or when nothing printed exists
    to compare against), so complete drafts are never re-extracted.
    """
    from decimal import Decimal

    lines = payable.get("line_items") if isinstance(payable.get("line_items"), list) else []
    if not lines:
        return None
    line_sum = Decimal("0")
    for line in lines:
        if not isinstance(line, dict):
            continue
        value = _decimal(line.get("total"))
        if value is not None:
            line_sum += value
    if line_sum == 0:
        return None

    candidates: list[Decimal] = []
    declared = _decimal(payable.get("subtotal"))
    if declared is not None:
        candidates.append(declared)
    gross = _decimal(payable.get("gross_total"))
    if gross is not None:
        candidates.append(gross)
        declared_tax = _decimal(payable.get("total_tax_amount"))
        if declared_tax is not None:
            candidates.append(gross - declared_tax)
        header_amounts = Decimal("0")
        header_known = True
        for tax in payable.get("taxes") or []:
            if not isinstance(tax, dict):
                continue
            amount = _decimal(tax.get("tax_amount"))
            if amount is None:
                header_known = False
                break
            header_amounts += amount
        if header_known and header_amounts != 0:
            candidates.append(gross - header_amounts)
    if not candidates:
        return None
    if any(abs(line_sum - candidate) <= Decimal("0.05") for candidate in candidates):
        return None
    gap = min((abs(line_sum - candidate) for candidate in candidates))
    return float(gap)


def build_signals(
    payable: dict[str, Any],
    validation: dict[str, Any],
    quality_issues: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compose the signal bundle handed to the repair turn.

    Each signal is ``{kind, field_path?, message, ...facts}``. Messages state
    what the ERP computed and what the document states. None of them name a
    remedy; the solver model owns that judgment.
    """
    signals: list[dict[str, Any]] = []

    printed = validation.get("printed_gross") or str(payable.get("gross_total", ""))
    booked = validation.get("booked_gross")
    if booked is not None:
        signals.append({
            "kind": "GROSS_DELTA",
            "field_path": "gross_total",
            "message": (
                f"The sealed ERP recomputes {booked} {payable.get('currency', '')} from the components in the "
                f"draft, while the document prints {printed} {payable.get('currency', '')} as the amount owed. "
                "The delta is the symptom; its cause is one or more components whose shipped value does not "
                "reflect what the page shows."
            ),
            "booked_gross": booked,
            "printed_gross": printed,
        })

    trace = erp_trace(payable)
    if trace:
        signals.append({
            "kind": "ERP_STRUCTURE",
            "message": (
                "ERP model facts (identical to the sealed calculator): each line's net base is "
                "quantity x unit_price, less a line discount if present; a line tax with an amount is summed as "
                "given, otherwise it is derived from the line's own base and rate. Header taxes are derived on "
                "the total net base (all line bases minus any header discount); a header tax with a rate but a "
                "zero or empty amount is still derived on that whole base. Freight, insurance, extra "
                "charges and excise duties are added as separate components. Line tax and header tax are summed "
                "independently — a tax placed at both levels contributes twice by construction."
            ),
            "trace": trace,
        })
        for line in trace["lines"]:
            if not line["printed_total"]:
                continue
            try:
                printed_line = float(str(line["printed_total"]).replace(",", "."))
            except ValueError:
                continue
            erp_base = float(line["erp_line_base"])
            delta = round2(printed_line - erp_base)
            if abs(delta) >= 0.01:
                signals.append({
                    "kind": "LINE_BASE_VS_PRINTED_TOTAL",
                    "field_path": f"line_items[{line['index']}]",
                    "message": (
                        f"Line {line['index']} ({line['description'][:60]!r}): the ERP derives {line['erp_line_base']} "
                        f"from quantity {line['quantity']!r} x unit_price {line['unit_price']!r} "
                        f"(discount {line['discount']!r}, {line['discount_percentage']!r}%); the page prints a line "
                        f"total of {line['printed_total']!r} (delta {_money(delta)})."
                    ),
                    "erp_line_base": line["erp_line_base"],
                    "printed_line_total": line["printed_total"],
                })

    declared = str(payable.get("total_tax_amount", "")).strip()
    if trace and declared:
        try:
            declared_value = float(declared.replace(",", "."))
            component_sum = float(trace["header_taxes_sum"]) + float(trace["line_taxes_sum"])
            if abs(declared_value - component_sum) >= 0.01:
                signals.append({
                    "kind": "DECLARED_TAX_VS_COMPONENTS",
                    "field_path": "total_tax_amount",
                    "message": (
                        f"The document declares total tax {declared}; the taxes in the draft contribute "
                        f"{_money(component_sum)} to the ERP gross ({trace['header_taxes_sum']} header + "
                        f"{trace['line_taxes_sum']} line). If a printed tax is missing from the draft, the ERP "
                        "cannot see it regardless of the declared total."
                    ),
                })
        except ValueError:
            pass

    for issue in quality_issues or []:
        code = str(issue.get("code", ""))
        if code in {
            "TAX_AT_BOTH_LEVELS", "LINE_EXTENSION_MISMATCH", "DECLARED_TAX_DIFFERENCE",
            "INCOMPLETE_LINE_COMPONENTS", "INVALID_INVOICE_TYPE", "MISSING_GROSS_TOTAL",
        }:
            signals.append({
                "kind": f"CHECK_{code}",
                "message": str(issue.get("message", "")),
                "severity": str(issue.get("severity", "warning")),
            })

    return signals
