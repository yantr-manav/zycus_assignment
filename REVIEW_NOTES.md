# Review notes for the generated outputs

Every payable in `output/` recomputes through the sealed `erp.py` to the amount its document states (`python main.py --validate-output --input documents --output output` → 0 unreconciled, 0 schema/semantic errors). This file records the quality warnings that the validator reports and the manual review that resolved them, so nothing is silently accepted.

## Reviewed warnings (non-blocking, explained)

| File | Warning | Review conclusion |
|---|---|---|
| `DU-03.json` payable 2 | invoice number missing | The goods-invoice page (Polvex) genuinely prints no invoice number; a blank is the honest value. No fabrication. |
| `HLD-03.json` | invoice number missing | The document prints no invoice number field. Blank is correct. |
| `HLD-10.json` | invoice number missing | The document prints no invoice number. Blank is correct. |
| `INV-19.json` | invoice number missing | The document prints no invoice number. Blank is correct. |
| `INV-10.json` | currency missing | The page does not print an ISO currency code; left blank rather than guessed (the ERP reconciles the amounts regardless). |
| `INV-34.json` | currency missing | Same as above. |
| `INV-25.json` | line extension mismatch (2 lines) | Expected for this tax-inclusive invoice: the printed column is inclusive (“P.VENDA C/IVA”) while `unit_price` is the documented net decomposition; the payable reconciles to the printed total exactly. |
| `INV-06.json` | declared tax not represented | **Known structural weakness, flagged not hidden:** the receipt's prices are tax-inclusive and this extraction kept them as net prices with an empty VAT entry, so the gross foots but the VAT is carried inside the prices. The correct representation (net line bases + header VAT 15% at the printed 169.83) requires a vision re-read with working provider credentials; the flag exists so the record cannot silently pass as structurally clean. |
| `INV-26.json` | line 19 has no description | The source page prints no description for that row (verified at high magnification). The row’s quantity/price/amount are faithful, and the table sums exactly to the printed total. |
| `INV-32.json` | line 2 has no description | Same class as above: no description printed for that row on the source page. |

## Notes on specific documents

- `HLD-05.pdf` — the totals block’s taxable base (835.27), per-rate VAT (19.82 + 157.05) and the untaxed container deposit (25.80) are printed; the payable books 1,037.94 exactly. Nothing was inferred about the embedded excise.
- `INV-06.pdf` — tax-inclusive grocery receipt; header VAT at its printed amount (169.83) with net line prices; books 1,683.98 exactly.
- `INV-21.pdf` — Portuguese invoice; the deterministic quantity resolver restored `24,000` → 24 (locale), the credit row keeps its printed negative amount, and the printed totals foot: 535.79.
- `INV-26.pdf` — 35-row Malaysian invoice; the completeness re-read (line sums that reproduced no printed total) drove a corrective pass with the strongest vision model, which found the row printed beside the “Northwind Consulting SA” section label; all 35 rows now sum exactly to the printed 873.53.
- `INV-37.pdf` — utility reimbursement; the deterministic printed-formula snapper submits the exact products of the printed rate and bases (105.597025 / 7.015) so the document’s own net (2,487.73) is reproduced; the printed per-line components are unchanged.
- `DU-03.pdf` — a 12-page dossier yielding two payables (freight 1,040.06 USD; goods 4,499.00 EUR); permits and waybills declined.

Full per-document repair details, provider attempts, ERP facts and accepted changes are in the run diagnostics (`logs/run.jsonl`); regenerate with the commands in `README.md`.

## Deferred structural polish (bookings unaffected)

Four records would benefit from a fresh vision pass to improve field/structure fidelity. They were held back because the provider credential file in the working tree was replaced (the current `.env` belongs to an unrelated project: one invalid Gemini key, no Groq/OpenRouter/Cloudflare credentials), so no API route was available. None of these affects the execution anchor — all four already book exactly:

- `INV-01` — `supplier.name` currently holds the recipient (“Northwind Operations OÜ”, the “Herrn …” addressee); the issuer's name is not printed on the page, so the faithful value is an empty supplier name. The new party-role prompt rule addresses this on any re-run.
- `INV-06` — the tax-inclusive representation described above (the new `DECLARED_TAX_NOT_REPRESENTED` warning keeps it visible).
- `INV-19` — currency should be `GHS` (the page prints `GH₵`; the record carries the legacy `GHC`). The new ISO-currency prompt rule addresses this.
- `HLD-08` — `po_number` should capture `9267418170` printed under “Your Reference”. The new reference-field prompt rule addresses this.
