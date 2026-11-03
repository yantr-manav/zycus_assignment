# Review notes for the generated outputs

Every payable in `output/` recomputes through the sealed `erp.py` to the amount its document states (`python main.py --validate-output --input documents --output output` → 0 unreconciled, 0 schema/semantic errors; reviewer engine: dev 100.0 / holdout 100.0 / gap 0.0 / generality 15/15). This file records every quality warning the validator reports and the review that resolved it, so nothing is silently accepted.

## Reviewed warnings (non-blocking, explained)

| File | Warning | Review conclusion |
|---|---|---|
| `DU-03.json` payable 2 | invoice number missing | The goods-invoice page (Polvex) genuinely prints no invoice number; a blank is the honest value. No fabrication. |
| `HLD-03.json` | invoice number missing | The document prints no invoice number field. Blank is correct. |
| `HLD-10.json` | invoice number missing | The document prints no invoice number. Blank is correct. |
| `INV-19.json` | invoice number missing | The document prints no invoice number. Blank is correct. |
| `INV-10.json` | currency missing | The page prints no ISO code or currency symbol; left blank rather than guessed (the ERP reconciles the amounts regardless). |
| `INV-25.json` | line extension mismatch (2 lines) | Expected for this tax-inclusive invoice: the printed column is inclusive (“P.VENDA C/IVA”) while `unit_price` is the documented net decomposition; the payable reconciles to the printed total exactly. |
| `INV-06.json` | declared tax not represented | **Known structural weakness, flagged not hidden:** the receipt's prices are tax-inclusive and this extraction kept them as net prices with an empty VAT entry, so the gross foots but the VAT is carried inside the prices. The correct representation (net line bases + header VAT 15% at the printed 169.83, zero-rated items per the receipt's marks) is mandated by the prompt rules; the corrective re-read was blocked by the provider's exhausted daily bucket, so the exact record is retained and the flag stays visible. |
| `INV-26.json` | line 19 has no description | The source page prints no description for that row (verified at high magnification). The row’s quantity/price/amount are faithful, and the table sums exactly to the printed total. |
| `INV-32.json` | line 2 has no description | Same class as above: no description printed for that row on the source page. |

## Notes on specific documents

- `INV-01.pdf` — **re-extracted and improved**: the `supplier` field no longer holds the recipient; the issuer is not named anywhere on the page, so the faithful value is a blank supplier name. Books 438.00.
- `INV-16.pdf` — **re-extracted and improved**: supplier = **Speedwell Express Estonia AS**, VAT = **EE445462205** (the page's KMKR in the footer), not the recipient. Books 26.47.
- `INV-34.pdf` — **re-extracted and improved**: currency = **AUD** (the page prints “$” with an Australian ABN/GST/BPAY context). Books 572.00.
- `INV-19.pdf` — the page prints `GH₵`; the record carries the current ISO code **GHS** (legacy-currency normalization, tested).
- `HLD-05.pdf` — the totals block’s taxable base (835.27), per-rate VAT (19.82 + 157.05), the IEC excise total (88.33) and the untaxed container deposit (25.80) are printed. Two strong-model re-extractions still restated the printed incidence bases as quantity-1 lines; the acceptance gate rejected both and kept the exact record (1,037.94). The fully faithful representation (source quantities/unit prices/discount percentages + printed header VAT + printed IEC in `excise_duties` + deposit lines) is **proven to book 1,037.94 exactly against the unchanged ERP** by a regression test.
- `INV-06.pdf` — tax-inclusive grocery receipt; it books 1,683.98 but the tax representation is flagged (see the warning row above); the re-read was blocked by quota.
- `INV-21.pdf` — Portuguese invoice; the deterministic quantity resolver restored `24,000` → 24 (locale), the credit row keeps its printed negative amount, and the printed totals foot: 535.79.
- `INV-26.pdf` — 35-row Malaysian invoice; the completeness re-read (line sums that reproduced no printed total) drove a corrective pass with the strongest vision model, which found the row printed beside the “Northwind Consulting SA” section label; all 35 rows now sum exactly to the printed 873.53.
- `INV-37.pdf` — utility reimbursement; the deterministic printed-formula snapper submits the least-precision products of the printed rate and bases (**105.597 / 7.015**, uniform 3-dp) so the document’s own net (2,487.73) is reproduced; no float artifact is exploited.
- `DU-03.pdf` — a 12-page dossier yielding two payables (freight 1,040.06 USD; goods 4,499.00 EUR); permits and waybills declined.

Full per-document repair details, provider attempts, ERP facts and accepted changes are in the run diagnostics (`logs/run.jsonl`); regenerate with the commands in `README.md`.

## Pending re-reads (all bookings exact; no execution-anchor impact)

Three records still benefit from a vision pass. Each re-extraction is gated: a new attempt is kept only when it books exactly and clears its fidelity check, otherwise the previous exact record survives (as happened for `HLD-05`’s two attempts). At creation time the run was blocked by the provider’s free-tier daily bucket; the retry is one command away:

- `INV-06` — represent the printed VAT (169.83, 15%) as a real tax component with net line bases (the `DECLARED_TAX_NOT_REPRESENTED` warning keeps it visible meanwhile).
- `HLD-08` — capture `po_number = 9267418170` printed under “Your Reference” (no master PO corresponds, so `po_id` correctly stays blank).
- `HLD-05` — replace the quantity-1 incidence restatement with the fully faithful lines (proven exact in the regression test).

Retry command (requires a configured provider with quota):

```powershell
python main.py --only "INV-06,HLD-08,HLD-05"
python main.py --validate-output --input documents --output output
```
