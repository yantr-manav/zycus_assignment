from __future__ import annotations
import copy
import json
import re
from typing import Any
from rapidfuzz import fuzz
from erp import erp_book
from .master_data import norm_text
from .normalization import normalize_decimal

REQUIRED_PAYABLE_KEYS = {
    "invoice_number", "invoice_date", "due_date", "invoice_type", "currency", "supplier", "buyer",
    "payment_term_id", "po_number", "po_id", "gross_total", "subtotal", "total_tax_amount",
    "discount_amount", "freight_charges", "insurance_charges", "extra_charges", "excise_duties", "taxes", "line_items"
}
REQUIRED_SUPPLIER_KEYS = {"name", "supplier_id", "address", "vat_id"}
REQUIRED_BUYER_KEYS = {"company_code", "business_unit_code", "location_code"}
REQUIRED_TAX_KEYS = {"tax_type", "tax_name", "tax_rate", "tax_amount", "tax_type_code"}
REQUIRED_LINE_KEYS = {"description", "item_type", "uom", "quantity", "unit_price", "total", "discount", "discount_percentage", "tax_rate", "tax_amount", "taxes"}


def _parse_number(value: Any) -> float | None:
    """Parse a payable number string using the same locale rules as the ERP."""
    normalized = normalize_decimal(value)
    if not normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def resolve_line_components(payable: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Restore a math-certain component when the printed row arithmetic pins it.

    Determinism boundary: when a line prints unit price, discount and row total,
    the quantity is *provable* — it is the only positive value whose ERP line
    base reproduces the printed total. This catches locale misreads and pack
    scaling (e.g. "24,000" read as 24000 when 24 is the only solution) without
    ever using the gross target. Applied only when the solution is exact: any
    ambiguity leaves the line untouched.
    """
    resolved = copy.deepcopy(payable)
    changes: list[dict[str, Any]] = []

    header_rates: list[float] = []
    for tax in payable.get("taxes") or []:
        if isinstance(tax, dict):
            rate = _parse_number(tax.get("tax_rate"))
            if rate and rate > 0:
                header_rates.append(rate)

    def current_base(line: dict[str, Any]) -> float | None:
        qty = _parse_number(line.get("quantity"))
        price = _parse_number(line.get("unit_price"))
        if qty is None or price is None:
            return None
        disc_pct = abs(_parse_number(line.get("discount_percentage")) or 0.0)
        disc_amt = abs(_parse_number(line.get("discount")) or 0.0)
        sub_total = qty * price
        if disc_pct > 0:
            return round(sub_total - (sub_total * disc_pct / 100.0), 2)
        if disc_amt > 0 and qty != 0:
            return round((price - (disc_amt / abs(qty))) * qty, 2)
        return round(sub_total, 2)

    for index, line in enumerate(resolved.get("line_items") or []):
        if not isinstance(line, dict):
            continue
        total = _parse_number(line.get("total"))
        if total is None or total == 0:
            continue
        qty = _parse_number(line.get("quantity"))
        price = _parse_number(line.get("unit_price"))
        disc_pct = abs(_parse_number(line.get("discount_percentage")) or 0.0)
        disc_amt = abs(_parse_number(line.get("discount")) or 0.0)
        if qty is None and str(line.get("quantity", "")).strip() == "" and price and price > 0 and disc_pct == 0 and disc_amt == 0:
            # Empty quantity with a printed price and total: the quotient is
            # unique. Accept it only when it is clean (integer or 4 decimals).
            solution = total / price
            if 0 < solution < 10_000_000:
                if abs(solution - round(solution)) <= 1e-6:
                    candidate = str(int(round(solution)))
                elif abs(solution - round(solution, 4)) <= 1e-9:
                    candidate = format(round(solution, 4), "f").rstrip("0").rstrip(".")
                else:
                    candidate = None
                if candidate:
                    trial_base = round(float(candidate) * price, 2)
                    if abs(trial_base - total) <= 0.005:
                        changes.append({
                            "kind": "LINE_QUANTITY_RESOLVED",
                            "field_path": f"line_items[{index}].quantity",
                            "before": "",
                            "after": candidate,
                            "printed_line_total": format(total, "f"),
                            "message": (
                                f"line {index}: quantity was empty; the printed total {format(total, 'f')} equals "
                                f"{candidate} x the printed unit price {format(price, 'f')} exactly."
                            ),
                        })
                        line["quantity"] = candidate
            continue
        if price is None or qty is None or price <= 0:
            continue
        if disc_amt > 0:
            # With an amount discount the solution depends on the quantity itself;
            # leave ambiguous cases to extraction/repair rather than guessing.
            base = current_base(line)
            if base is not None and abs(base - total) <= 0.005:
                continue
            continue
        base = current_base(line)
        if base is not None and abs(base - total) <= 0.005:
            continue
        # If a tax rate explains the gap, the printed total is simply
        # tax-inclusive — that is not a quantity error, and a tax-inclusive
        # total must never be used to "solve" for the quantity.
        if base is not None and base > 0:
            line_rates = []
            line_rate = _parse_number(line.get("tax_rate"))
            if line_rate and line_rate > 0:
                line_rates.append(line_rate)
            for tax in line.get("taxes") or []:
                if isinstance(tax, dict):
                    rate = _parse_number(tax.get("tax_rate"))
                    if rate and rate > 0:
                        line_rates.append(rate)
            if any(abs(base * (1 + rate / 100.0) - total) <= 0.01 for rate in line_rates + header_rates):
                continue
        # A header tax amount can also sit inside the printed total.
        header_amount_sum = 0.0
        for tax in payable.get("taxes") or []:
            if isinstance(tax, dict):
                amount = _parse_number(tax.get("tax_amount"))
                if amount and amount > 0:
                    header_amount_sum += amount
        if base is not None and header_amount_sum > 0 and abs(base + header_amount_sum - total) <= 0.02:
            continue
        effective_price = price * (1 - disc_pct / 100.0) if disc_pct > 0 else price
        if effective_price <= 0:
            continue
        solution = total / effective_price
        if not (0 < solution < 10_000_000):
            continue
        # The current quantity must be the implausible one: candidates are the
        # nearest integer, and a 4-decimal value only when the current quantity
        # differs from it by an exact power of ten (a decimal-separator misread).
        def is_power_of_ten_ratio(current: float, solved: float) -> bool:
            if current <= 0 or solved <= 0:
                return False
            ratio = current / solved
            if ratio < 1:
                ratio = 1 / ratio
            for power in range(1, 7):
                if abs(ratio - (10 ** power)) <= (10 ** power) * 1e-6:
                    return True
            return False

        candidates: list[float] = []
        rounded_int = round(solution)
        if rounded_int > 0:
            candidates.append(float(rounded_int))
        rounded_4 = round(solution, 4)
        if rounded_4 > 0 and rounded_4 not in candidates and is_power_of_ten_ratio(qty, rounded_4):
            candidates.append(rounded_4)
        accepted = None
        for candidate in candidates:
            line_copy = dict(line)
            line_copy["quantity"] = str(int(candidate)) if candidate == int(candidate) else format(candidate, "f").rstrip("0").rstrip(".")
            base_trial = current_base({
                **line,
                "quantity": line_copy["quantity"],
            })
            if base_trial is not None and abs(base_trial - total) <= 0.005:
                accepted = line_copy["quantity"]
                break
        if accepted is not None and accepted != str(line.get("quantity", "")).strip():
            changes.append({
                "kind": "LINE_QUANTITY_RESOLVED",
                "field_path": f"line_items[{index}].quantity",
                "before": str(line.get("quantity", "")),
                "after": accepted,
                "printed_line_total": format(total, "f"),
                "message": (
                    f"line {index}: quantity {line.get('quantity', '')!r} does not reproduce the printed row total "
                    f"{format(total, 'f')} from the printed unit price {format(price, 'f')} and discount; "
                    f"{accepted} is the only quantity that does."
                ),
            })
            line["quantity"] = accepted
    return resolved, changes


def shape_errors(payable: dict[str, Any]) -> list[str]:
    errors=[]
    if set(payable) != REQUIRED_PAYABLE_KEYS:
        missing = sorted(REQUIRED_PAYABLE_KEYS - set(payable))
        extra = sorted(set(payable) - REQUIRED_PAYABLE_KEYS)
        if missing: errors.append(f"payable missing keys: {missing}")
        if extra: errors.append(f"payable has unexpected keys: {extra}")
    for key, required in (("supplier", REQUIRED_SUPPLIER_KEYS), ("buyer", REQUIRED_BUYER_KEYS)):
        obj = payable.get(key)
        if not isinstance(obj, dict):
            errors.append(f"{key} must be an object")
        else:
            if set(obj) != required:
                errors.append(f"{key} keys mismatch: missing={sorted(required-set(obj))}, extra={sorted(set(obj)-required)}")
    if not isinstance(payable.get("taxes"), list): errors.append("taxes must be an array")
    if not isinstance(payable.get("line_items"), list): errors.append("line_items must be an array")
    for i, tax in enumerate(payable.get("taxes", []) if isinstance(payable.get("taxes"), list) else []):
        if not isinstance(tax, dict) or set(tax) != REQUIRED_TAX_KEYS:
            errors.append(f"taxes[{i}] keys/type mismatch")
    for i, line in enumerate(payable.get("line_items", []) if isinstance(payable.get("line_items"), list) else []):
        if not isinstance(line, dict) or set(line) != REQUIRED_LINE_KEYS:
            errors.append(f"line_items[{i}] keys/type mismatch")
            continue
        if not isinstance(line.get("taxes"), list): errors.append(f"line_items[{i}].taxes must be array")
        for j, tax in enumerate(line.get("taxes", [])):
            if not isinstance(tax, dict) or set(tax) != REQUIRED_TAX_KEYS:
                errors.append(f"line_items[{i}].taxes[{j}] keys/type mismatch")
    # Schema uses strings for all values, so enforce the record shape explicitly.
    def check_string_tree(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items(): check_string_tree(v, f"{path}.{k}" if path else k)
        elif isinstance(value, list):
            for i, v in enumerate(value): check_string_tree(v, f"{path}[{i}]")
        elif not isinstance(value, str):
            errors.append(f"{path} must be a string, got {type(value).__name__}")
    check_string_tree(payable)
    return errors


def reconcile(payable: dict[str, Any]) -> dict[str, Any]:
    try:
        result = erp_book(payable)
        booked = round(float(result["will_book_gross"]), 2)
    except Exception as exc:
        return {"status": "ERROR", "error": f"ERP calculation failed: {type(exc).__name__}: {exc}"}
    printed_text = str(payable.get("gross_total", "")).strip()
    if not printed_text:
        return {"status": "NO_PRINTED_GROSS", "booked_gross": booked, "currency": result.get("currency", ""), "difference": None, "matches": False}
    try:
        printed = round(float(normalize_decimal(printed_text)), 2)
    except (ValueError, TypeError):
        printed = None
    if printed is None:
        return {"status": "UNPARSEABLE_PRINTED_GROSS", "booked_gross": booked, "printed_gross": printed_text, "currency": result.get("currency", ""), "difference": None, "matches": False}
    difference = round(booked - printed, 2)
    return {
        "status": "MATCH" if difference == 0 else "MISMATCH",
        "booked_gross": booked,
        "printed_gross": f"{printed:.2f}",
        "currency": result.get("currency", ""),
        "difference": f"{difference:.2f}",
        "matches": difference == 0,
    }


def _path_tokens(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    # Split only on dots, and turn the safe array-index form into integer tokens.
    for segment in path.split("."):
        m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\[(\d+)\]", segment)
        if m:
            tokens.extend([m.group(1), int(m.group(2))])
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", segment):
            tokens.append(segment)
        else:
            raise ValueError("invalid change path")
    return tokens

# A gross mismatch can only justify edits to components that actually affect erp_book().
# The printed gross is a target/observation, never a repair knob. Printed line `total`
# is not consumed by erp_book(), so changing it cannot repair reconciliation either.
_ALLOWED_TOP = {"discount_amount", "freight_charges", "insurance_charges", "extra_charges", "excise_duties"}
_ALLOWED_LINE = {"quantity", "unit_price", "discount", "discount_percentage", "tax_rate", "tax_amount"}
_ALLOWED_TAX = {"tax_rate", "tax_amount"}


def _allowed_path(tokens: list[str | int]) -> bool:
    if len(tokens) == 1 and isinstance(tokens[0], str): return tokens[0] in _ALLOWED_TOP
    if len(tokens) == 3 and tokens[0] == "line_items" and isinstance(tokens[1], int) and tokens[2] in _ALLOWED_LINE: return True
    if len(tokens) == 5 and tokens[0] == "line_items" and isinstance(tokens[1], int) and tokens[2] == "taxes" and isinstance(tokens[3], int) and tokens[4] in _ALLOWED_TAX: return True
    if len(tokens) == 3 and tokens[0] == "taxes" and isinstance(tokens[1], int) and tokens[2] in _ALLOWED_TAX: return True
    return False


def _get_at(root: Any, tokens: list[str | int]) -> Any:
    current = root
    for token in tokens:
        current = current[token]
    return current


def _set_at(root: Any, tokens: list[str | int], value: Any) -> None:
    current = root
    for token in tokens[:-1]: current = current[token]
    current[tokens[-1]] = value


def _path_context_ok(path: str, quote: str) -> bool:
    """Check that the quote names the kind of component being edited.

    Context cues intentionally distinguish quantity/unit price/tax rate/tax amount;
    a generic invoice total must not justify a unit-price correction.
    """
    q = norm_text(quote)
    q_raw = quote.casefold()
    tokens = _path_tokens(path)
    leaf = str(tokens[-1]) if tokens else ""
    if not tokens:
        return False

    if leaf == "quantity":
        cues = ("quantity", "qty", "menge", "units", "pieces", "quantidade", "cantidad", "quantite", "qte", "stk")
    elif leaf == "unit_price":
        cues = ("unitprice", "priceperunit", "unitrate", "perunit", "rateper", "preispro", "prixunitaire", "preciounitario", "precoinidade")
    elif leaf == "discount_percentage":
        cues = ("discountpercentage", "discountpercent", "discountpct", "rabattprozent", "remise", "desconto")
        has_percent_marker = any(c in q_raw for c in ("%", "percent", "percentage", "pct", "prozent"))
        has_discount_label = any(c in q for c in ("discount", "rabatt", "remise", "desconto", "skonto"))
        if not (has_percent_marker and has_discount_label):
            return False
    elif leaf == "discount":
        cues = ("discount", "rabatt", "desconto", "remise", "skonto")
    elif leaf == "tax_rate":
        cues = ("taxrate", "vatrate", "gstrate", "rateofvat", "taxpercent", "vatpercent", "gstpercent", "taxpercentage", "vatpercentage", "gstpercentage", "mwst", "vat", "gst", "sales tax")
        has_rate_marker = any(c in q_raw for c in ("%", "percent", "percentage")) or any(c in q for c in ("rate", "mwst"))
        has_tax_label = any(c in q for c in ("tax", "vat", "gst", "mwst", "nhil", "getfl", "iva", "ust"))
        if not (has_rate_marker and has_tax_label):
            return False
    elif leaf == "tax_amount":
        cues = ("taxamount", "vatamount", "gstamount", "taxvalue", "withholding", "taxdeducted", "amountoftax", "tax charged", "vat charged", "gst charged")
        has_amount_marker = any(c in q for c in ("amount", "value", "withholding", "deducted", "charged", "payable tax")) or any(c in q_raw for c in ("€", "$", "£", "¥", "₹"))
        has_tax_label = any(c in q for c in ("tax", "vat", "gst", "mwst", "nhil", "getfl", "iva", "withholding"))
        if not (has_amount_marker and has_tax_label):
            return False
    elif leaf in {"discount_amount", "freight_charges", "insurance_charges", "extra_charges", "excise_duties"}:
        cue_map = {
            "discount_amount": ("discount", "rabatt", "desconto", "remise", "skonto"),
            "freight_charges": ("freight", "shipping", "transport", "deliverycharge", "versand", "fracht"),
            "insurance_charges": ("insurance", "seguro", "assurance", "versicherung"),
            "extra_charges": ("surcharge", "servicecharge", "othercharge", "fee", "charge", "gebuhr", "encargo"),
            "excise_duties": ("excise", "duty", "levy", "accise", "verbrauchsteuer"),
        }
        cues = cue_map[leaf]
    else:
        return False
    return any(norm_text(cue) in q for cue in cues)

def _quote_verified(quote: str, source_text: str) -> bool:
    normalized_quote = norm_text(quote)
    normalized_source = norm_text(source_text)
    if not normalized_quote or not normalized_source:
        return False
    if normalized_quote in normalized_source:
        return True
    return fuzz.partial_ratio(normalized_quote, normalized_source) >= 92


def _clear_context_ok(path: str, quote: str) -> bool:
    """Context check for a neutralization (no new value exists to verify).

    For tax amounts, a totals-block quote like "Total VAT 20% 100.00" is a
    legitimate reference even though it never says "amount": the tax label plus
    a printed number is enough, because acceptance downstream still requires the
    candidate to reproduce the printed gross *exactly*.
    """
    tokens = _path_tokens(path)
    leaf = str(tokens[-1]) if tokens else ""
    if leaf == "tax_amount":
        normalized = norm_text(quote)
        has_label = any(label in normalized for label in ("tax", "vat", "gst", "mwst", "nhil", "getfl", "iva", "km", "moms", "withholding"))
        has_number = bool(re.search(r"\d", quote))
        return has_label and has_number
    return _path_context_ok(path, quote)


def _grounded_clear(path: str, quote: str, source_text: str) -> tuple[bool, str]:
    """Grounding for a neutralization (setting a component to empty).

    There is no new value to verify, so the bar is different and stricter
    downstream: the quote must exist on the page and describe the field being
    neutralized, and the caller additionally requires the candidate to
    reproduce the printed gross exactly before accepting the change.
    """
    quote = str(quote or "").strip()
    if len(norm_text(quote)) < 10:
        return False, "evidence quote is too short"
    if not _quote_verified(quote, source_text):
        return False, "evidence quote could not be verified against extracted page text"
    if not _clear_context_ok(path, quote):
        return False, "evidence quote does not contain context for the field being cleared"
    return True, "field context and source quote verified; acceptance still requires exact reconciliation"


def _grounded_quote(path: str, quote: str, proposed_value: str, source_text: str) -> tuple[bool, str]:
    quote = str(quote or "").strip()
    if len(norm_text(quote)) < 10:
        return False, "evidence quote is too short"
    if not _quote_verified(quote, source_text):
        return False, "evidence quote could not be verified against extracted page text"
    if not _path_context_ok(path, quote):
        return False, "evidence quote does not contain context for the field being changed"
    # Compare numeric values canonically rather than requiring punctuation to match exactly
    # (e.g. proposed 12.00 can be printed as 12, 12,00, or 12.00).
    def canonical_number(raw: str) -> str:
        normalized = normalize_decimal(raw, money=False)
        if not normalized:
            return ""
        try:
            from decimal import Decimal
            return format(Decimal(normalized).normalize(), "f")
        except Exception:
            return ""

    proposed_number = canonical_number(str(proposed_value))
    if not proposed_number:
        return False, "proposed value is not a normalizable number"
    number_pattern = re.compile(r"(?<![A-Za-z])[-+]?\(?\d[\d.,'’ ]*\d?%?\)?(?![A-Za-z])")

    # Rule 1 of the brief: every value the system emits must appear on the document.
    # This also stops a fuzzy quote match from smuggling in a value that differs from
    # the printed digits (e.g. quote says 50.00 while the page visibly says 40.00).
    source_numbers = {
        canonical_number(match.group(0).rstrip("%"))
        for match in number_pattern.finditer(source_text)
    }
    if proposed_number not in source_numbers:
        return False, "proposed numeric value does not appear anywhere in the source text"

    number_matches = []
    for match in number_pattern.finditer(quote):
        lexeme = match.group(0).strip()
        canonical = canonical_number(lexeme.rstrip("%"))
        if canonical:
            number_matches.append((canonical, match.start(), match.end(), lexeme))
    candidates = [m for m in number_matches if m[0] == proposed_number]
    if not candidates:
        return False, "proposed numeric value is not present in its evidence quote"

    # Require the matching number to be near the field label, not merely elsewhere in
    # the quote. Tax amount quotes containing only a rate (e.g. 'VAT 19%') are rejected.
    context_map = {
        "quantity": ("quantity", "qty", "menge", "units", "pieces", "quantidade", "cantidad", "quantite", "qte", "stk"),
        "unit_price": ("unit price", "unitprice", "price per unit", "priceperunit", "unit rate", "unitrate", "per unit", "preis pro", "prix unitaire", "preco por unidade"),
        "discount": ("discount", "rabatt", "desconto", "remise", "skonto"),
        "discount_percentage": ("discount", "rabatt", "desconto", "remise", "skonto"),
        "tax_rate": ("tax rate", "vat rate", "gst rate", "tax percent", "vat percent", "tax percentage", "vat", "gst", "mwst", "nhil", "getfl"),
        "tax_amount": ("tax amount", "vat amount", "gst amount", "tax value", "withholding", "tax deducted", "amount of tax", "tax charged", "vat", "gst", "mwst", "nhil", "getfl"),
        "discount_amount": ("discount", "rabatt", "desconto", "remise", "skonto"),
        "freight_charges": ("freight", "shipping", "transport", "delivery charge", "versand", "fracht"),
        "insurance_charges": ("insurance", "seguro", "assurance", "versicherung"),
        "extra_charges": ("surcharge", "service charge", "other charge", "fee", "charge", "gebuhr", "encargo"),
        "excise_duties": ("excise", "duty", "levy", "accise", "verbrauchsteuer"),
    }
    leaf = str(_path_tokens(path)[-1])
    lower_quote = quote.casefold()
    cue_positions = []
    for cue in context_map.get(leaf, ()):
        cursor = 0
        while True:
            pos = lower_quote.find(cue.casefold(), cursor)
            if pos < 0:
                break
            cue_positions.append((pos, pos + len(cue)))
            cursor = pos + max(1, len(cue))
    supported = False
    for cue_start, cue_end in cue_positions:
        nearby_numbers = []
        for canonical, start, end, lexeme in number_matches:
            distance = (start - cue_end) if start >= cue_end else (cue_start - end)
            if (start >= cue_end and distance <= 48) or (end <= cue_start and distance <= 20):
                if leaf == "tax_amount" and "%" in lexeme:
                    continue  # a printed percentage alone cannot ground an amount field
                nearby_numbers.append((abs(distance), canonical, start, end, lexeme))
        if not nearby_numbers:
            continue
        # The value nearest the edited field label is the only value that label can
        # ground. This stops 'Unit price 10; quantity 2' from accepting 2 as the price.
        nearest = min(nearby_numbers, key=lambda item: item[0])
        canonical, start, end, lexeme = nearest[1], nearest[2], nearest[3], nearest[4]
        if canonical != proposed_number:
            continue
        if leaf == "tax_amount":
            nearby = quote[max(0, start - 12):min(len(quote), end + 16)]
            has_currency = any(symbol in nearby for symbol in ("€", "$", "£", "¥", "₹"))
            has_amount_label = any(word in norm_text(nearby) for word in ("amount", "value", "charged", "withheld", "deducted"))
            if not (has_currency or has_amount_label):
                continue
        supported = True
        break
    if not supported:
        return False, "numeric value is not unambiguously associated with the edited field in its evidence quote"
    return True, "field label, numeric value and source quote verified"


def apply_grounded_repairs(raw_payable: dict[str, Any], repair_payload: dict[str, Any], source_text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = copy.deepcopy(raw_payable)
    applied=[]
    changes = repair_payload.get("changes", [])
    if not isinstance(changes, list): return result, applied
    for change in changes:
        if not isinstance(change, dict): continue
        path = str(change.get("path", ""))
        value = change.get("value", "")
        quote = str(change.get("evidence_quote", ""))
        if not isinstance(value, (str, int, float)):
            applied.append({"path": path, "accepted": False, "reason": "change value must be scalar"}); continue
        try:
            tokens = _path_tokens(path)
            if not _allowed_path(tokens):
                raise ValueError("path is not in the editable allow-list")
            previous = _get_at(result, tokens)
            if not isinstance(previous, (str, int, float)):
                raise ValueError("path does not point to a scalar value")
            if str(previous).strip() == str(value).strip():
                applied.append({"path": path, "accepted": False, "reason": "proposed value is unchanged"})
                continue
            if str(value).strip() == "":
                # Neutralization: the model is asserting the component should not
                # exist. Grounding is context-only; the pipeline additionally
                # requires the candidate to reconcile exactly before accepting.
                ok, why = _grounded_clear(path, quote, source_text)
                if not ok:
                    applied.append({"path": path, "accepted": False, "reason": why}); continue
                _set_at(result, tokens, "")
                applied.append({"path": path, "accepted": True, "cleared": True, "before": str(previous), "after": "",
                                "reason": str(change.get("reason", "")), "evidence_quote": quote})
                continue
            ok, why = _grounded_quote(path, quote, str(value), source_text)
            if not ok:
                applied.append({"path": path, "accepted": False, "reason": why}); continue
            _set_at(result, tokens, str(value).strip())
            applied.append({"path": path, "accepted": True, "before": str(previous), "after": str(value), "reason": str(change.get("reason", "")), "evidence_quote": quote})
        except Exception as exc:
            applied.append({"path": path, "accepted": False, "reason": str(exc)})
    return result, applied
