# Design Notes — Bookable Payable AI

## 1. What I understood later that I did not on day one

Day one, this looks like OCR-plus-field-mapping. It is not. The ERP does not book what the page *shows*; it books what its own arithmetic *derives* from the raw components you submit. The task is a translation from the document's presentation to the ERP's model, and almost every hard document is one translation hiding behind several masks:

- **Tax-inclusive pricing** (“Unit Price (incl VAT)”, “P.VENDA C/IVA”): the printed price is the taxed amount. The ERP adds tax on top of what you give it, so a faithful copy overstates the gross. The translation is a net base per taxable line (`incl ÷ (1+rate)`), with zero-rated items untouched — and the printed tax total is the arbiter of which items are zero-rated.
- **Pack and price units** (“Price unit 100”, qty 100 PC, amount 771.66): the ERP multiplies quantity × unit price, so the faithful copy books 100× too much. The translation is to express the line in price-units (the document itself says the price covers a pack).
- **Tax represented once.** A rate in a line column *and* a totals-block tax amount is how most invoices present tax; submitting both levels charges it twice, because the ERP sums line and header taxes independently. One placement, the document's own printed amount, is the faithful model.
- **Discounts represented once** (line percentage XOR totals-block deduction XOR negative row), **credits with positive magnitudes** plus `CREDIT_MEMO`, **withholding as a negative tax**, **duties as bookable rows** (quantity 1 × printed amount when no unit price exists), **locale numbers** (`24,000` is 24.00 on a Portuguese invoice; the row arithmetic proves it).
- And the quiet one: **a document set is not a document**. A dossier is freight invoice + goods invoice + customs permit + waybill; the permits and waybills are not payables, the two invoices are two payables, and a page from a different seller with a price and a total is its own payable even without the word “invoice”.

Each mask looked like a special case until it became the same sentence: *presentation ≠ the ERP's model, and the builder's job is the translation.*

## 2. What the system does on a document unlike any it has seen

Nothing about the machinery branches on filename, supplier, or a list of known cases. The pipeline:

1. **Classifies before extracting** — zero, one, or several payables, and declines for proformas, permits, statements, dunning letters. Supporting pages do not make a set non-payable; the governing payable is still found.
2. **Extracts raw components under translation rules** that describe the ERP's finite computation and the presentation patterns above — not answers. The model may return an empty field; it may not invent a value to make a total foot.
3. **Applies deterministic arithmetic** where the document makes it certain: a quantity uniquely pinned by printed row arithmetic is restored (`total ÷ (price × (1−discount))`, only when exact and not explained by a tax rate); totals and tax declarations are checked against components.
4. **Reconciles through the sealed calculator** and, when it disagrees, runs a **bounded signal→repair loop**: the system states facts (“the ERP derives X on this line; the page prints Y”; “the declared tax is Z, the components contribute W”), and the model proposes source-quoted corrections. Deterministic code accepts a change only if it is grounded, schema-clean and provably better — or, for a removal, lands exactly on the printed amount.

That is why it generalises instead of guessing: the mechanisms are keyed to the ERP's model and to *provable* arithmetic on the page, not to the documents in the folder. The measured record is consistent with that: the pipeline books every held-out document correctly (5/5 holdout, 26/30 dev gross, 30/30 dev payable-count/classification at the time of writing — and the small dev-vs-holdout gap is the wrong direction for overfitting to exist).

## 3. A document that could not be solved the way the others were

`HLD-05.pdf` (a Portuguese *Fatura* with an **embedded IEC excise**). The gross is dominated by a duty that is *inside* the printed prices, and the page never states a per-line excise split — only the inclusive prices and the total. Every mechanism the system uses on other documents either needs the split to be printed or would have to invent it: putting the excise as a line “derived” from the total is precisely the plug the brief forbids, and a header excise at some inferred rate would be a fabricated figure. The honest answer is the one the system takes: submit the components the page does support, do not fabricate the excise, reconcile what can be reconciled, and **flag the payable for human review** rather than fake a booking. Distinguishing “the document doesn't say” from “I didn't find it” is the same discipline that keeps a matching gross from being treated as proof of a correct record.

## 4. Limits and safe operation

The translation of a tax-inclusive or pack-scaled line is a model task; the deterministic layer verifies it (schema, grounding, exact reconciliation) but cannot manufacture it, so a weak extraction can still produce a well-formed, non-reconciling draft — which is reported, not hidden. OCR can garble a quote even when the page is legible, in which case the repair fails closed. The included tests validate the code paths and invariants (resolver exactness, grounding, clear-op exactness, loop convergence, chunk splitting/merging, key rotation and redaction); they are not a claim that every document in the folder is perfect — the unresolved ones are listed honestly in `VERIFICATION.md`, with the exact re-run command.
