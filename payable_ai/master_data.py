from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from rapidfuzz import fuzz


def norm_text(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).casefold().replace("&", " and ")
    s = s.replace("ß", "ss").replace("æ", "ae").replace("ø", "o").replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "", s)


def norm_id(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _load(path: Path, key: str) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get(key, [])
    if not isinstance(rows, list):
        raise ValueError(f"Expected {key} array in {path}")
    return rows


def _prefix_keys(value: Any) -> set[str]:
    """Compact blocking keys from the normalized name and each original name token.

    Token-prefix indexes use far less memory than storing every character trigram for a
    million-row supplier/address catalog. The exact-identifier indexes still take priority;
    the prefix block is only a candidate generator before fuzzy scoring.
    """
    raw = str(value or "")
    whole = norm_text(raw)
    keys: set[str] = set()
    for width in (2, 3, 4):
        if len(whole) >= width:
            keys.add(whole[:width])
    raw_tokens = re.findall(r"[A-Za-z0-9]+", raw.casefold())
    for token in raw_tokens:
        normalized = norm_text(token)
        if len(normalized) >= 3:
            keys.add(normalized[:3])
        elif len(normalized) >= 2:
            keys.add(normalized)
    return keys


def _index_add(index: dict[str, set[int]], value: str, row_id: int) -> None:
    if value:
        index.setdefault(value, set()).add(row_id)


class MasterData:
    """Load supplied reference JSON and build compact indexes once, not per invoice.

    Exact identifiers/names are O(1) dictionary lookups. Approximate supplier and buyer
    resolution uses token-prefix candidate blocking before similarity scoring. This is more
    memory-efficient than storing every character n-gram. Ambiguity resolves to blank.
    For very large masters, replace flat JSON with a database/search index while preserving
    the resolver contract and uniqueness checks.
    """

    _FUZZY_CANDIDATE_CAP = 500
    _SMALL_MASTER_SCAN_CAP = 5000

    def __init__(self, root: Path):
        self.root = root
        self.suppliers = _load(root / "suppliers.json", "suppliers")
        self.taxes = _load(root / "tax_master.json", "taxes")
        self.payment_terms = _load(root / "payment_terms.json", "payment_terms")
        self.purchase_orders = _load(root / "po_master.json", "purchase_orders")
        self.companies = _load(root / "chart_of_books.json", "companies")

        self._supplier_by_vat: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._supplier_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._supplier_name_prefix: dict[str, set[int]] = {}
        self._supplier_country_prefix: dict[str, dict[str, set[int]]] = defaultdict(dict)
        self._supplier_country_rows: dict[str, list[int]] = defaultdict(list)
        self._supplier_missing_country_rows: list[int] = []
        for idx, row in enumerate(self.suppliers):
            vat = norm_id(row.get("vat_id"))
            name = norm_text(row.get("name"))
            country = norm_id(row.get("country"))
            if vat:
                self._supplier_by_vat[vat].append(row)
            if name:
                self._supplier_by_name[name].append(row)
                for prefix in _prefix_keys(row.get("name")):
                    _index_add(self._supplier_name_prefix, prefix, idx)
                    _index_add(self._supplier_country_prefix[country], prefix, idx)
            if country:
                self._supplier_country_rows[country].append(idx)
            else:
                self._supplier_missing_country_rows.append(idx)

        self._po_by_number: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.purchase_orders:
            # Deduplicate per row if normalized PO number and ID happen to be identical.
            row_keys = {norm_id(row.get("po_number")), norm_id(row.get("po_id"))} - {""}
            for number in row_keys:
                self._po_by_number[number].append(row)

        self._taxes_by_country: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._taxes_by_country_type_rate: dict[tuple[str, str, float], list[dict[str, Any]]] = defaultdict(list)
        for row in self.taxes:
            country = norm_id(row.get("country"))
            tax_type = norm_text(row.get("tax_type"))
            try:
                rate = round(float(str(row.get("rate", "")).replace(",", ".")), 4)
            except (TypeError, ValueError):
                rate = None
            self._taxes_by_country[country].append(row)
            if rate is not None:
                self._taxes_by_country_type_rate[(country, tax_type, rate)].append(row)

        # Term catalogs are usually small but are normalized once for easy exact alias search.
        self._term_aliases: list[tuple[str, dict[str, Any]]] = []
        self._terms_by_days: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in self.payment_terms:
            for alias in [row.get("payment_term_id", "")] + list(row.get("text_aliases", [])):
                normalized = norm_text(alias)
                if normalized:
                    self._term_aliases.append((normalized, row))
            try:
                days = int(row.get("days", -999))
                self._terms_by_days[days].append(row)
            except (TypeError, ValueError):
                pass

        # Flatten hierarchical chart-of-accounts rows once; retain parent rows to return codes.
        self._buyer_records: list[dict[str, Any]] = []
        self._buyer_exact_index: dict[str, set[int]] = defaultdict(set)
        self._buyer_prefix_index: dict[str, set[int]] = {}
        self._buyer_all_ids: set[int] = set()
        for company in self.companies:
            for bu in company.get("business_units", []):
                for loc in bu.get("locations", []):
                    record = {
                        "company": company,
                        "business_unit": bu,
                        "location": loc,
                        "company_name": norm_text(company.get("company_name")),
                        "company_code": norm_text(company.get("company_code")),
                        "bu_name": norm_text(bu.get("business_unit_name")),
                        "bu_code": norm_text(bu.get("business_unit_code")),
                        "location_name": norm_text(loc.get("location_name")),
                        "address": norm_text(loc.get("invoice_to_address")),
                    }
                    row_id = len(self._buyer_records)
                    self._buyer_records.append(record)
                    self._buyer_all_ids.add(row_id)
                    raw_keys = (
                        company.get("company_name"), company.get("company_code"),
                        bu.get("business_unit_name"), bu.get("business_unit_code"),
                        loc.get("location_name"), loc.get("invoice_to_address"),
                    )
                    for key in (record["company_name"], record["company_code"], record["bu_name"], record["bu_code"], record["location_name"], record["address"]):
                        if key:
                            self._buyer_exact_index[key].add(row_id)
                    for raw_key in raw_keys:
                        for prefix in _prefix_keys(raw_key):
                            _index_add(self._buyer_prefix_index, prefix, row_id)

    def _supplier_candidate_ids(self, name: str, country: str, raw_name: Any = "") -> list[int]:
        country_ids = set(self._supplier_country_rows.get(country, [])) if country else set(range(len(self.suppliers)))
        country_ids.update(self._supplier_missing_country_rows)
        if not country_ids:
            return []
        prefixes = _prefix_keys(raw_name or name)
        counts: Counter[int] = Counter()
        if country:
            idx = self._supplier_country_prefix.get(country, {})
            for prefix in prefixes:
                for row_id in idx.get(prefix, ()):
                    if row_id in country_ids:
                        counts[row_id] += 1
            # Supplier rows with no country are still valid matching candidates.
            for prefix in prefixes:
                for row_id in self._supplier_name_prefix.get(prefix, ()):
                    if row_id in self._supplier_missing_country_rows:
                        counts[row_id] += 1
        else:
            for prefix in prefixes:
                for row_id in self._supplier_name_prefix.get(prefix, ()):
                    counts[row_id] += 1
        if counts:
            return [row_id for row_id, _ in counts.most_common(self._FUZZY_CANDIDATE_CAP)]
        # On a small catalog a full scan is safe/useful; on a large catalog fail closed rather
        # than running an expensive unconstrained fuzzy match against millions of rows.
        if len(country_ids) <= self._SMALL_MASTER_SCAN_CAP:
            return sorted(country_ids)
        return []

    def resolve_supplier(self, supplier: dict[str, Any]) -> tuple[str, str]:
        vat = norm_id(supplier.get("vat_id"))
        country = norm_id(supplier.get("country"))
        name = norm_text(supplier.get("name"))
        if vat:
            matches = self._supplier_by_vat.get(vat, [])
            if len(matches) == 1:
                return str(matches[0].get("supplier_id", "")), "exact VAT ID"
            if len(matches) > 1:
                return "", "duplicate VAT ID in master data; ambiguous match"
        if not name:
            return "", "supplier name/VAT ID absent"
        exact = list(self._supplier_by_name.get(name, []))
        if country:
            exact = [s for s in exact if not s.get("country") or norm_id(s.get("country")) == country]
        if len(exact) == 1:
            return str(exact[0].get("supplier_id", "")), "exact normalized supplier name"
        if len(exact) > 1:
            return "", "duplicate normalized supplier name; ambiguous match"

        candidates_ids = self._supplier_candidate_ids(name, country, supplier.get("name", ""))
        scored = sorted(
            ((fuzz.WRatio(name, norm_text(self.suppliers[i].get("name"))), self.suppliers[i]) for i in candidates_ids),
            key=lambda pair: pair[0], reverse=True,
        )
        if scored and scored[0][0] >= 94 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 5):
            return str(scored[0][1].get("supplier_id", "")), f"high-confidence name similarity ({scored[0][0]:.0f})"
        return "", "no unambiguous supplier match"

    def resolve_buyer(self, buyer: dict[str, Any]) -> tuple[dict[str, str], str]:
        result = {"company_code": "", "business_unit_code": "", "location_code": ""}
        company_name = norm_text(buyer.get("company_name"))
        bu_name = norm_text(buyer.get("business_unit_name"))
        loc_name = norm_text(buyer.get("location_name"))
        address = norm_text(buyer.get("invoice_to_address"))

        query_ids: set[int] = set()
        exact_ids: set[int] = set()
        overlap_counts: Counter[int] = Counter()
        query_values = (
            buyer.get("company_name"), buyer.get("company_code"),
            buyer.get("business_unit_name"), buyer.get("business_unit_code"),
            buyer.get("location_name"), buyer.get("invoice_to_address"),
        )
        for raw_value in query_values:
            key = norm_text(raw_value)
            if key:
                hits = self._buyer_exact_index.get(key, set())
                exact_ids.update(hits)
                overlap_counts.update({row_id: 100 for row_id in hits})
                for prefix in _prefix_keys(raw_value):
                    overlap_counts.update(self._buyer_prefix_index.get(prefix, set()))
        query_ids = set(overlap_counts) | exact_ids
        if not query_ids and len(self._buyer_records) <= self._SMALL_MASTER_SCAN_CAP:
            query_ids = set(self._buyer_all_ids)
        # Exact hits are always retained. For fuzzy candidates, rank by shared character
        # n-grams rather than row order, then cap expensive similarity/containment scoring.
        if len(query_ids - exact_ids) > self._FUZZY_CANDIDATE_CAP:
            ranked_fuzzy = sorted(
                query_ids - exact_ids,
                key=lambda row_id: (-overlap_counts[row_id], row_id),
            )[:self._FUZZY_CANDIDATE_CAP]
            query_ids = exact_ids | set(ranked_fuzzy)

        candidates = []
        for idx in query_ids:
            row = self._buyer_records[idx]
            co_name = row["company_name"]
            co_code = row["company_code"]
            bu_norm = row["bu_name"]
            bu_code = row["bu_code"]
            addr_norm = row["address"]
            loc_norm = row["location_name"]
            company_hit = bool(company_name and (company_name == co_name or company_name == co_code or company_name in co_name or co_name in company_name))
            company_code_query = norm_text(buyer.get("company_code"))
            company_hit = company_hit or bool(company_code_query and (company_code_query == co_code))
            bu_code_query = norm_text(buyer.get("business_unit_code"))
            bu_hit = bool(bu_name and (bu_name == bu_norm or bu_name == bu_code or bu_name in bu_norm or bu_norm in bu_name))
            bu_hit = bu_hit or bool(bu_code_query and bu_code_query == bu_code)
            address_hit = bool(address and addr_norm and (address == addr_norm or (len(address) >= 12 and (address in addr_norm or addr_norm in address))))
            loc_name_hit = bool(loc_name and (loc_name == loc_norm or loc_norm in loc_name or loc_name in loc_norm))
            score = (4 if bu_hit else 0) + (3 if company_hit else 0) + (2 if address_hit else 0) + (1 if loc_name_hit else 0)
            if score:
                candidates.append((score, row, bu_hit, company_hit, address_hit))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            best = candidates[0]
            row = best[1]
            if best[3] or best[2]:
                result["company_code"] = str(row["company"].get("company_code", ""))
            if best[2]:
                result["business_unit_code"] = str(row["business_unit"].get("business_unit_code", ""))
            if best[4] or (best[2] and loc_name and row["location_name"] == loc_name):
                result["location_code"] = str(row["location"].get("location_code", ""))
            if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
                a, b = candidates[0][1], candidates[1][1]
                if a["business_unit"].get("business_unit_code") != b["business_unit"].get("business_unit_code"):
                    result["business_unit_code"] = ""
            return result, "matched printed buyer identity/address"
        return result, "buyer identity not sufficiently matched"

    def resolve_payment_term(self, term_text: str, invoice_date: str = "", due_date: str = "") -> tuple[str, str]:
        text = norm_text(term_text)
        if text:
            matched_by_row: dict[str, tuple[int, dict[str, Any]]] = {}
            for alias, row in self._term_aliases:
                if alias in text:
                    key = str(row.get("payment_term_id", ""))
                    matched_by_row[key] = (max(len(alias), matched_by_row.get(key, (0, {}))[0]), row)
            ranked = sorted(matched_by_row.values(), key=lambda pair: pair[0], reverse=True)
            if len(ranked) == 1:
                return str(ranked[0][1].get("payment_term_id", "")), "printed payment-term alias"
            if len(ranked) > 1:
                if ranked[0][0] > ranked[1][0]:
                    return str(ranked[0][1].get("payment_term_id", "")), "longest printed payment-term alias"
        try:
            inv = date.fromisoformat(invoice_date)
            due = date.fromisoformat(due_date)
            delta = (due - inv).days
            matches = self._terms_by_days.get(delta, []) if delta >= 0 else []
            if len(matches) == 1:
                return str(matches[0].get("payment_term_id", "")), "invoice/due-date delta exactly matches a master term"
        except (ValueError, TypeError):
            pass
        return "", "no sufficiently supported payment-term match"

    def resolve_po(self, po_number: str, supplier_id: str = "", currency: str = "") -> tuple[str, str]:
        raw = norm_id(po_number)
        if not raw:
            return "", "no PO number on document"
        exact = self._po_by_number.get(raw, [])
        if len(exact) == 1:
            po = exact[0]
            if supplier_id and po.get("supplier_id") and supplier_id != str(po.get("supplier_id")):
                return "", "PO number matched but supplier conflicts with master data"
            if currency and po.get("currency") and currency.upper() != str(po.get("currency")).upper():
                return "", "PO number matched but currency conflicts with master data"
            return str(po.get("po_id", "")), "exact PO number with no supplier/currency conflict"
        return "", "printed PO not found uniquely in PO master"

    def resolve_tax_code(self, tax: dict[str, Any], supplier_country: str = "") -> tuple[str, str]:
        raw_rate = str(tax.get("tax_rate", "")).replace("%", "").strip()
        try:
            rate = round(float(raw_rate.replace(",", ".")), 4) if raw_rate else None
        except ValueError:
            rate = None
        tax_type = norm_text(tax.get("tax_type"))
        name = norm_text(tax.get("tax_name"))
        country = norm_id(supplier_country)
        if not country:
            country_hints = {"german": "DE", "estonian": "EE", "portugal": "PT", "portuguese": "PT", "ghana": "GH", "malaysia": "MY", "southafrica": "ZA", "uk": "GB", "british": "GB", "kenya": "KE", "switzerland": "CH", "canada": "CA", "denmark": "DK", "poland": "PL", "thailand": "TH", "singapore": "SG", "sweden": "SE", "romania": "RO", "vietnam": "VN"}
            for word, code in country_hints.items():
                if word in name:
                    country = code
                    break
        country_candidates = list(self._taxes_by_country.get(country, [])) if country else list(self.taxes)
        if tax_type:
            typed = [row for row in country_candidates if norm_text(row.get("tax_type")) == tax_type]
            if typed:
                country_candidates = typed
        if rate is not None:
            if country:
                rated = self._taxes_by_country_type_rate.get((country, tax_type, rate), []) if tax_type else [row for row in country_candidates if abs(float(row.get("rate", 999999)) - rate) < 0.0001]
            else:
                rated = [row for row in country_candidates if abs(float(row.get("rate", 999999)) - rate) < 0.0001]
            if rated:
                country_candidates = list(rated)
            else:
                return "", "no tax-master rate match"
        if name:
            scored = sorted(((fuzz.WRatio(name, norm_text(t.get("name"))), t) for t in country_candidates), key=lambda x: x[0], reverse=True)
            if scored and scored[0][0] >= 75 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 8):
                return str(scored[0][1].get("code", "")), "tax name/rate/type match"
        if len(country_candidates) == 1 and rate is not None and tax_type and country:
            return str(country_candidates[0].get("code", "")), "unique country/type/rate match"
        return "", "tax master match ambiguous or unsupported"
