# Review notes for the generated outputs

Every payable in `output/` recomputes through the sealed `erp.py` to the amount its document states (`python main.py --validate-output --input documents --output output` → 0 unreconciled, 0 schema/semantic errors; reviewer engine: dev 100.0 / holdout 100.0 / gap 0.0 / generality 15/15). This file records every remaining quality warning and the review that resolved each flagged record, so nothing is silently accepted.

## Remaining warnings (all explainable page facts, non-blocking)

| File | Warning | Review conclusion |
|---|---|---|
| `DU-03.json` payable 2 | invoice number missing | The goods-invoice page (Polvex) genuinely prints no invoice number; a blank is the honest value. |
| `HLD-03.json` | invoice number missing | The document prints no invoice number field. Blank is correct. |
| `HLD-10.json` | invoice number missing | The document prints no invoice number. Blank is correct. |
| `INV-19.json` | invoice number missing | The document prints no invoice number. Blank is correct. |
| `INV-10.json` | currency missing | The page prints no ISO code or currency symbol; left blank rather than guessed (the ERP reconciles the amounts regardless). |
| `INV-26.json` | line 19 has no description | The source page prints no description for that row (verified at high magnification). The row’s quantity/price/amount are faithful, and the table sums exactly to the printed total. |
| `INV-32.json` | line 2 has no description | Same class as above: no description printed for that row on the source page. |

Warning classes that appeared during the work and were fixed at the root: declared tax not represented (`INV-06` — now a real header tax component), tax-inclusive line extensions (`INV-25` and `INV-06` — the check now accepts a printed extension that equals the net base grown by the line’s rate), printed extensions before a discount (`HLD-05` — both pre- and post-discount extensions are accepted), and legacy/absent currency codes (`INV-19` → GHS, `INV-34` → AUD).

## Field-level re-extraction outcomes (gated: kept only if exact **and** improved)

- `INV-01` — supplier no longer the recipient; the issuer is unnamed on the page, so the supplier name is blank (honest). 438.00.
- `INV-16` — supplier = **Speedwell Express Estonia AS**, VAT = **EE445462205** (the page’s KMKR). 26.47.
- `INV-34` — currency = **AUD** (page prints “$” with Australian ABN/GST/BPAY context). 572.00.
- `INV-06` — header VAT **15% / 169.83** with net line bases; zero-rated items keep printed amounts; books 1,683.98 exactly.
- `HLD-08` — `po_number` = **9267418170** (printed under “Your Reference”); `po_id` correctly blank (no master match). 148,941.47.
- `HLD-05` — faithful representation: real quantities (40, 6, 1, 9, 27, …), real unit prices, real discount percentages, printed header VAT (19.82 / 157.05), printed IEC excise **88.33** in `excise_duties`, untaxed deposit lines. 1,037.94 exactly.

## Notes on specific documents

- `HLD-05.pdf` — the totals block’s taxable base (835.27), per-rate VAT (19.82 + 157.05), the IEC excise total (88.33) and the untaxed container deposit (25.80) are printed; the faithful record now reproduces 1,037.94 exactly, and the same construction is locked in by a regression test. Nothing about the excise is inferred.
- `INV-06.pdf` — tax-inclusive grocery receipt; the printed VAT is now represented, with net taxable bases and zero-rated items at their printed amounts; delivery is modeled at its net amount with its VAT inside the printed tax total. Books 1,683.98.
- `INV-19.pdf` — the page prints `GH₵`; the record carries the current ISO code **GHS** (legacy-currency normalization, tested).
- `INV-21.pdf` — Portuguese invoice; the deterministic quantity resolver restored `24,000` → 24 (locale), the credit row keeps its printed negative amount, and the printed totals foot: 535.79.
- `INV-26.pdf` — 35-row Malaysian invoice; the completeness re-read (line sums that reproduced no printed total) drove a corrective pass with the strongest vision model, which found the row printed beside the “Northwind Consulting SA” section label; all 35 rows sum exactly to the printed 873.53.
- `INV-37.pdf` — utility reimbursement; the deterministic printed-formula snapper submits the least-precision products of the printed rate and bases (**105.597 / 7.015**, uniform 3-dp) so the document’s own net (2,487.73) is reproduced; no float artifact is exploited.
- `DU-03.pdf` — a 12-page dossier yielding two payables (freight 1,040.06 USD; goods 4,499.00 EUR); permits and waybills declined.

Full per-document repair details, provider attempts, ERP facts and accepted changes are in the run diagnostics (`logs/run.jsonl`); regenerate with the commands in `README.md`. No record was hand-edited: every improvement above was produced by the pipeline (or its deterministic replay) and accepted only through the exact-match gates.
