from __future__ import annotations
import re
from datetime import date, datetime
from typing import Any
from .master_data import MasterData

PAYABLE_KEYS = [
    "invoice_number", "invoice_date", "due_date", "invoice_type", "currency", "supplier", "buyer",
    "payment_term_id", "po_number", "po_id", "gross_total", "subtotal", "total_tax_amount",
    "discount_amount", "freight_charges", "insurance_charges", "extra_charges", "excise_duties", "taxes", "line_items"
]
TAX_KEYS = ["tax_type", "tax_name", "tax_rate", "tax_amount", "tax_type_code"]
LINE_KEYS = ["description", "item_type", "uom", "quantity", "unit_price", "total", "discount", "discount_percentage", "tax_rate", "tax_amount", "taxes"]


def as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def normalize_decimal(value: Any, absolute: bool = False, money: bool = True) -> str:
    s = as_str(value)
    if not s:
        return ""
    s = s.replace("\u00a0", " ").replace("\u202f", " ").strip()
    parenthesized = s.startswith("(") and s.endswith(")")
    negative = s.startswith("-") or parenthesized
    s = s.replace("(", "").replace(")", "")
    s = re.sub(r"[A-Za-z€$£¥₹\s]", "", s).replace("%", "")
    s = s.lstrip("+-")
    # Locale-aware separator normalization based on the rightmost separator.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")  # 1.234,56
        else:
            s = s.replace(",", "")  # 1,234.56
    elif "," in s:
        parts = s.split(",")
        if len(parts) > 2:
            s = "".join(parts[:-1]) + "." + parts[-1]
        elif len(parts[-1]) in (1, 2):
            s = parts[0] + "." + parts[1]
        elif len(parts[-1]) == 3:
            s = parts[0] + parts[1]
        else:
            s = parts[0] + "." + parts[1]
    if not re.fullmatch(r"\d+(?:\.\d+)?", s):
        return ""
    try:
        from decimal import Decimal
        number = Decimal(s)
        if absolute:
            number = abs(number)
        elif negative:
            number = -number
        if money:
            out = format(number, "f")
            if "." not in out:
                out += ".00"
            else:
                decimals = len(out.split(".", 1)[1])
                if decimals == 1:
                    out += "0"
            return out
        out = format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else format(number, "f")
        return out or "0"
    except (ValueError, ArithmeticError):
        return ""



def normalize_iso_date(value: Any) -> str:
    s = as_str(value)
    if not s:
        return ""
    # ISO forms are always unambiguous.
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        pass
    formats = ["%d.%m.%Y", "%d.%m.%y", "%d %B %Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d"]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # Slash dates are parsed only when one component makes the order unambiguous.
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        first, second, year = map(int, m.groups())
        try:
            if first > 12 and second <= 12:
                return date(year, second, first).isoformat()  # DD/MM/YYYY
            if second > 12 and first <= 12:
                return date(year, first, second).isoformat()  # MM/DD/YYYY
        except ValueError:
            return ""
    return ""  # ambiguous/unparseable date: do not guess

def _money_fields(obj: dict[str, Any], fields: list[str], absolute: bool = False) -> None:
    for key in fields:
        obj[key] = normalize_decimal(obj.get(key), absolute=absolute)


def _normalize_tax(tax: Any, masters: MasterData, country: str, credit: bool) -> dict[str, str]:
    if not isinstance(tax, dict):
        tax = {}
    tax_type_value = as_str(tax.get("tax_type"))
    tax_name_value = as_str(tax.get("tax_name"))
    tax_semantics = (tax_type_value + " " + tax_name_value).casefold()
    is_withholding = any(token in tax_semantics for token in ("withholding", "retention", "deduction", "withheld", "retained"))
    out = {
        "tax_type": tax_type_value,
        "tax_name": tax_name_value,
        "tax_rate": normalize_decimal(tax.get("tax_rate"), money=False),
        # Credit-note monetary magnitudes are positive, except a real withholding/deduction
        # tax that the ERP contract explicitly represents as a negative amount.
        "tax_amount": normalize_decimal(tax.get("tax_amount"), absolute=(credit and not is_withholding)),
        "tax_type_code": "",
    }
    code, _ = masters.resolve_tax_code(out, country)
    out["tax_type_code"] = code
    return out


def normalize_payable(raw: Any, masters: MasterData) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(raw, dict):
        raw = {}
    supplier_raw = raw.get("supplier") if isinstance(raw.get("supplier"), dict) else {}
    buyer_raw = raw.get("buyer") if isinstance(raw.get("buyer"), dict) else {}
    invoice_type = as_str(raw.get("invoice_type")).upper()
    if invoice_type not in {"INVOICE", "CREDIT_MEMO"}:
        invoice_type = "INVOICE"
    credit = invoice_type == "CREDIT_MEMO"

    supplier_country = as_str(supplier_raw.get("country")).upper()
    supplier = {
        "name": as_str(supplier_raw.get("name")),
        "supplier_id": "",
        "address": as_str(supplier_raw.get("address")),
        "vat_id": as_str(supplier_raw.get("vat_id")),
    }
    supplier_match_input = {**supplier, "country": supplier_country}
    supplier_id, supplier_match = masters.resolve_supplier(supplier_match_input)
    supplier["supplier_id"] = supplier_id
    if not supplier_country and supplier_id:
        supplier_country = next((str(s.get("country", "")) for s in masters.suppliers if str(s.get("supplier_id")) == supplier_id), "")

    buyer_codes, buyer_match = masters.resolve_buyer(buyer_raw)
    buyer = buyer_codes
    payment_term_id, payment_term_match = masters.resolve_payment_term(
        as_str(raw.get("payment_term_text")), as_str(raw.get("invoice_date")), as_str(raw.get("due_date"))
    )
    currency = as_str(raw.get("currency")).upper()
    po_number = as_str(raw.get("po_number"))
    po_id, po_match = masters.resolve_po(po_number, supplier_id, currency)

    out: dict[str, Any] = {
        "invoice_number": as_str(raw.get("invoice_number")),
        "invoice_date": normalize_iso_date(raw.get("invoice_date")),
        "due_date": normalize_iso_date(raw.get("due_date")),
        "invoice_type": invoice_type,
        "currency": currency,
        "supplier": supplier,
        "buyer": buyer,
        "payment_term_id": payment_term_id,
        "po_number": po_number,
        "po_id": po_id,
        "gross_total": as_str(raw.get("gross_total")),
        "subtotal": as_str(raw.get("subtotal")),
        "total_tax_amount": as_str(raw.get("total_tax_amount")),
        "discount_amount": as_str(raw.get("discount_amount")),
        "freight_charges": as_str(raw.get("freight_charges")),
        "insurance_charges": as_str(raw.get("insurance_charges")),
        "extra_charges": as_str(raw.get("extra_charges")),
        "excise_duties": as_str(raw.get("excise_duties")),
        "taxes": [],
        "line_items": [],
    }
    _money_fields(out, ["gross_total", "subtotal", "total_tax_amount", "discount_amount", "freight_charges", "insurance_charges", "extra_charges", "excise_duties"], absolute=credit)

    taxes_raw = raw.get("taxes") if isinstance(raw.get("taxes"), list) else []
    out["taxes"] = [_normalize_tax(tax, masters, supplier_country, credit) for tax in taxes_raw if isinstance(tax, dict)]
    lines_raw = raw.get("line_items") if isinstance(raw.get("line_items"), list) else []
    for line_raw in lines_raw:
        if not isinstance(line_raw, dict):
            continue
        line = {
            "description": as_str(line_raw.get("description")),
            "item_type": as_str(line_raw.get("item_type")).upper(),
            "uom": as_str(line_raw.get("uom")),
            "quantity": normalize_decimal(line_raw.get("quantity"), absolute=credit, money=False),
            "unit_price": normalize_decimal(line_raw.get("unit_price"), absolute=credit),
            "total": normalize_decimal(line_raw.get("total"), absolute=credit),
            "discount": normalize_decimal(line_raw.get("discount"), absolute=True),
            "discount_percentage": normalize_decimal(line_raw.get("discount_percentage"), absolute=True, money=False),
            "tax_rate": normalize_decimal(line_raw.get("tax_rate"), money=False),
            "tax_amount": normalize_decimal(
                line_raw.get("tax_amount"),
                absolute=(credit and not any(token in as_str(line_raw.get("description")).casefold() for token in ("withholding", "retention", "deduction", "withheld", "retained"))),
            ),
            "taxes": [],
        }
        if line["item_type"] not in {"GOODS", "SERVICE", "FREIGHT", "TAX"}:
            desc = line["description"].casefold()
            if any(word in desc for word in ("freight", "shipping", "delivery charge", "transport")):
                line["item_type"] = "FREIGHT"
            elif any(word in desc for word in ("vat", "sales tax", "excise duty", "withholding tax")):
                line["item_type"] = "TAX"
            elif any(word in desc for word in ("service", "support", "subscription", "consult", "rental", "maintenance", "hours", "labor", "installation", "fee")):
                line["item_type"] = "SERVICE"
            else:
                line["item_type"] = "GOODS"
        line_taxes = line_raw.get("taxes") if isinstance(line_raw.get("taxes"), list) else []
        line["taxes"] = [_normalize_tax(tax, masters, supplier_country, credit) for tax in line_taxes if isinstance(tax, dict)]
        # If explicit line taxes exist, don't let the ERP double-count shorthand fields.
        if line["taxes"]:
            line["tax_rate"] = ""
            line["tax_amount"] = ""
        out["line_items"].append(line)

    diagnostics = {
        "master_matches": {
            "supplier": {"id": supplier_id, "reason": supplier_match},
            "buyer": {"codes": buyer_codes, "reason": buyer_match},
            "payment_term": {"id": payment_term_id, "reason": payment_term_match},
            "po": {"id": po_id, "reason": po_match},
        },
        "field_evidence": raw.get("field_evidence", {}) if isinstance(raw.get("field_evidence"), dict) else {},
    }
    return out, diagnostics


def normalize_document_type(raw: Any) -> str:
    value = as_str(raw).upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "CREDIT_NOTE":"CREDIT_MEMO", "CREDIT":"CREDIT_MEMO", "CREDIT_MEMO":"CREDIT_MEMO",
        "INVOICE":"INVOICE", "BILL":"INVOICE", "PURCHASE_ORDER":"PURCHASE_ORDER",
        "PO":"PURCHASE_ORDER", "STATEMENT":"STATEMENT", "REMITTANCE":"REMITTANCE_ADVICE",
        "REMITTANCE_ADVICE":"REMITTANCE_ADVICE", "DELIVERY_NOTE":"DELIVERY_NOTE", "QUOTE":"QUOTE",
    }
    return aliases.get(value, value if value in {"OTHER", "UNKNOWN", "INVOICE", "CREDIT_MEMO", "PURCHASE_ORDER", "STATEMENT", "REMITTANCE_ADVICE", "DELIVERY_NOTE", "QUOTE"} else "UNKNOWN")
