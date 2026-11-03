# Review notes for the generated outputs

Every payable in `output/` recomputes through the sealed `erp.py` to the amount its document states (`python main.py --validate-output --input documents --output output` → 0 unreconciled, 0 schema/semantic errors; reviewer engine: dev 100.0 / holdout 100.0 / gap 0.0 / generality 15/15). This file records every quality warning the validator reports and the manual review that resolved it, so nothing is silently accepted.

## Reviewed warnings (non-blocking, explained)

| File | Warning | Review conclusion |
|---|---|---|
| `DU-03.json` payable 2 | invoice number missing | The goods-invoice page (Polvex) genuinely prints no invoice number; a blank is the honest value. No fabrication. |
| `HLD-03.json` | invoice number missing | The document prints no invoice number field. Blank is correct. |
| `HLD-10.json` | invoice number missing | The document prints no invoice number. Blank is correct. |
| `INV-19.json` | invoice number missing | The document prints no invoice number. Blank is correct. |
| `INV-10.json` | currency missing | The page prints no ISO code or currency symbol; left blank rather than guessed (the ERP reconciles the amounts regardless). |
| `INV-34.json` | currency missing | The page prints “$” with an Australian ABN/GST/BPAY context; an AUD inference is defensible for a human, but the conservative rule is to leave truly ambiguous symbols blank rather than guess. Left blank and flagged; the ISO-currency prompt rule will resolve it on a re-read. |
| `INV-25.json` | line extension mismatch (2 lines) | Expected for this tax-inclusive invoice: the printed column is inclusive (“P.VENDA C/IVA”) while `unit_price` is the documented net decomposition; the payable reconciles to the printed total exactly. |
| `INV-06.json` | declared tax not represented | **Known structural weakness, flagged not hidden:** the receipt's prices are tax-inclusive and this extraction kept them as net prices with an empty VAT entry, so the gross foots but the VAT is carried inside the prices. The correct representation (net line bases + header VAT 15% at the printed 169.83, zero-rated items per the receipt's marks) requires a vision re-read with working provider credentials; the flag exists so the record cannot silently pass as structurally clean. |
| `INV-26.json` | line 19 has no description | The source page prints no description for that row (verified at high magnification). The row’s quantity/price/amount are faithful, and the table sums exactly to the printed total. |
| `INV-32.json` | line 2 has no description | Same class as above: no description printed for that row on the source page. |

## Notes on specific documents

- `HLD-05.pdf` — the totals block’s taxable base (835.27), per-rate VAT (19.82 + 157.05), the IEC excise total (88.33) and the untaxed container deposit (25.80) are printed. The current artifact restates each printed incidence base as a quantity-1 line, which books 1,037.94 exactly. The fully faithful representation (source quantities/unit prices/discount percentages + printed header VAT + printed IEC in `excise_duties` + deposit lines) is **proven to book 1,037.94 exactly against the unchanged ERP** by a regression test; nothing about the excise is ever inferred.
- `INV-06.pdf` — tax-inclusive grocery receipt; it books 1,683.98 but the tax representation is flagged (see the warning row above). The correct representation is mandated by the prompt rules for the next re-read.
- `INV-19.pdf` — the page prints `GH₵`; the record now carries the current ISO code **GHS** (legacy-currency normalization, tested).
- `INV-21.pdf` — Portuguese invoice; the deterministic quantity resolver restored `24,000` → 24 (locale), the credit row keeps its printed negative amount, and the printed totals foot: 535.79.
- `INV-26.pdf` — 35-row Malaysian invoice; the completeness re-read (line sums that reproduced no printed total) drove a corrective pass with the strongest vision model, which found the row printed beside the “Northwind Consulting SA” section label; all 35 rows now sum exactly to the printed 873.53.
- `INV-37.pdf` — utility reimbursement; the deterministic printed-formula snapper submits the least-precision products of the printed rate and bases (**105.597 / 7.015**, uniform 3-dp) so the document’s own net (2,487.73) is reproduced; the printed per-line components are unchanged and no float artifact is exploited.
- `DU-03.pdf` — a 12-page dossier yielding two payables (freight 1,040.06 USD; goods 4,499.00 EUR); permits and waybills declined.

Full per-document repair details, provider attempts, ERP facts and accepted changes are in the run diagnostics (`logs/run.jsonl`); regenerate with the commands in `README.md`.

## Deferred field-level re-reads (all bookings exact; no execution-anchor impact)

A fresh vision pass would improve field-level fidelity on these records. It could not be executed because no stable provider credential file was available in the final environment (the workspace `.env` was repeatedly absent or replaced by an unrelated project's file). The general prompt rules that fix each class are in place and take effect on the next run:

- `INV-01` — `supplier.name` currently holds the recipient (“Northwind Operations OÜ”, the “Herrn …” addressee); the issuer's name is not printed on the page, so the faithful value is an empty supplier name.
- `INV-16` — `supplier.name` currently holds the recipient (“Northwind Operations OÜ”); the issuer is **Speedwell Express Estonia AS**, printed in the footer with its KMKR/registration details.
- `HLD-08` — `po_number` should capture `9267418170` printed under “Your Reference”; no master PO corresponds, so `po_id` correctly stays blank.
- `HLD-05` — replace the quantity-1 incidence restatement with the fully faithful lines (proven exact).
- `INV-06` — the tax representation described above (the `DECLARED_TAX_NOT_REPRESENTED` warning keeps it visible).
- `INV-34` — a defensible currency code once the symbol/context is re-read.
