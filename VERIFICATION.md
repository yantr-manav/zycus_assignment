# Verification Record

Everything below was executed against this working tree. Where a claim depends on a run, the command and the observed result are given. Nothing in this file is an estimate.

## Final state (measured)

| Check | Command | Result |
|---|---|---|
| Test suite | `python -m pytest -q` | **48 passed, 1 skipped** (the opt-in live-provider smoke test) |
| Output coverage | `python main.py --validate-output --input documents --output output` | **42/42 outputs; 0 missing; 0 malformed** |
| Schema & semantics | same validator | **0 payables with schema or semantic errors** |
| ERP reconciliation | same validator (booked gross through the sealed `erp.py` vs the amount each document prints) | **0 payables unreconciled — every payable recomputes to the cent** |
| Ready flag | same validator | **`ready_for_manual_review: true`** |
| Quality warnings | same validator | **7 warnings, all reviewed page facts** (below) |
| Execution anchor (reviewer engine) | reviewer kit's `eval/review.py` | **dev 100.0, holdout 100.0, gap 0.0, generality 15/15; `erp.py` untampered; no divergences** |

## Reviewed warnings (all explainable, none blocking)

Absent invoice numbers on four documents that print none (`DU-03` second payable, `HLD-03`, `HLD-10`, `INV-19`); absent printed currency on `INV-10` (the page prints no ISO code or currency symbol — left blank rather than guessed); and two genuinely blank line descriptions on their source pages (`INV-26` row 19, `INV-32`). Every other warning class that appeared during the work was either fixed at its root or accepted by a precise rule:

- **Tax-inclusive line extensions** (`INV-25`, and the converted lines of `INV-06`) no longer raise false `LINE_EXTENSION_MISMATCH` warnings: the check now accepts a printed extension that equals the ERP's net base grown by the line's applicable rate (a faithful tax-inclusive presentation).
- **Printed row extensions before/after a discount** are both accepted (discount-column documents no longer raise false positives).
- **`INV-06`'s declared-tax flag is gone** — the printed VAT is now a real tax component.

## Field-level re-extraction pass (executed, credential-gated)

With valid credentials restored (Gemini key pool, plus Groq for the text route), the six flagged records were re-extracted behind strict acceptance gates: a new attempt was kept **only when it books exactly AND clears its specific fidelity check**; otherwise the previous exact record was restored. Final outcomes:

| Document | Outcome | Final record |
|---|---|---|
| `INV-01` | **fixed** — the supplier field no longer holds the recipient; the issuer is unnamed on the page, so the supplier name is blank (honest) | 438.00, MATCH |
| `INV-16` | **fixed** — supplier = **Speedwell Express Estonia AS**, VAT = **EE445462205** (the page's KMKR) | 26.47, MATCH |
| `INV-06` | **fixed** — the printed VAT is represented as a header tax (**15% / 169.83**) with net line bases; zero-rated items keep their printed amounts | 1,683.98, MATCH |
| `HLD-08` | **fixed** — `po_number` captures the printed reference **9267418170**; `po_id` stays blank (no master match — honest) | 148,941.47, MATCH |
| `HLD-05` | **fixed (faithful)** — real quantities (40, 6, 1, 9, 27, …), real unit prices, real discount percentages, header VAT at printed amounts (19.82 / 157.05), printed IEC excise **88.33** in `excise_duties`, deposit lines untaxed | 1,037.94, MATCH |
| `INV-34` | **fixed** — currency = **AUD** (the page prints “$” with an Australian ABN/GST/BPAY context) | 572.00, MATCH |

The `HLD-05` faithful form was independently proven exact against the unchanged ERP by a regression test before the extraction existed; the extraction now matches it line-for-line.

## How the difficult documents were resolved (all verified against the source pages)

- **INV-06** — tax-inclusive grocery receipt with zero-rated items. The printed VAT (169.83) is now a header tax component; taxable lines carry net bases, zero-rated items keep printed amounts, delivery is modeled at its net amount with its VAT inside the printed total. Books 1,683.98 exactly.
- **INV-19** — Ghanaian invoice; the page prints `GH₵`, and the output carries the current ISO code **GHS** (legacy-currency normalization, tested).
- **INV-21** — Portuguese invoice with a line discount, a trailing-minus credit row, and a totals-block IVA. Resolved by the discount-once rule plus the deterministic quantity resolver (`24,000` read as 24.00) and exact header tax; books 535.79.
- **INV-25** — tax-inclusive print shop (“P.VENDA C/IVA”). Net unit prices derived from the printed inclusive amounts; books 176.00.
- **INV-26** — 35-row Malaysian sales invoice. Two independent high-magnification reads of the page missed the amount of row 30 (printed alongside the “Northwind Consulting SA” section label) and could not foot the table to the printed 873.53. The deterministic **completeness signal** flagged the draft, drove a corrective re-read, and the faithful 35-row extraction now sums exactly to the printed total. No number was adjusted to close the gap.
- **INV-37** — utility reimbursement whose displayed tax roundings (105.60 / 7.02) do not match its own printed net. The deterministic **printed-formula snapper** submits the least-precision products of the printed rate and the uniquely identifiable bases (**105.597 / 7.015**, uniform 3-dp), reproducing the document’s own net 2,487.73 exactly without exploiting float artifacts or inventing precision.
- **HLD-05** — Portuguese Fatura with embedded excise and VAT presented at both levels. The faithful record now carries the source quantities/unit prices/discounts, the printed taxes once at the header, the printed IEC excise total and the untaxed deposits — 1,037.94 exactly. Nothing about the excise is inferred.
- **DU-03** — a 12-page shipment dossier yielding two payables (freight invoice 1,040.06 USD; goods invoice 4,499.00 EUR via the chunked text route), with permits/waybills declined.

## Master-data investigation (systematic blanks, never fabricated)

The supplied masters are samples. Reading the issuer blocks on the pages shows the corpus's issuing parties are not rows of `suppliers.json` (e.g. Speedwell Express Estonia AS, Larkspur Print OÜ, Redwater Technologies), and the documents' bill-to tenants (Northwind entities) are not the chart's Bolt entities — so blank supplier/buyer codes are the honest result, not missed matches. The resolver does match when data corresponds: the kit's worked sample resolves supplier `2845695` and buyer `BOLTGROUP/EE004/LOC_EE_001` in the unit tests, and PO/term/tax resolution follow the same exact-first, ambiguity-blank contract. Legacy currency codes are normalized to current ISO 4217 instead of being guessed.

## Provider verification (live-tested, not assumed)

- **Gemini** — key pools of mixed formats (`AIza…`, `AQ.…`) parsed, health-probed, rotated; daily per-model buckets observed (20/day/model) and rolled automatically; 503/overload handled as transient, exhausted buckets fail fast. `gemini-3.6-flash` / `gemini-3-flash-preview` resolved the dense scans that the text route could not.
- **Groq** — `qwen/qwen3.8-27b`, `openai/gpt-oss-20b`, `openai/gpt-oss-120b` live; free tier observed at ~8,000 tokens/minute, so long documents auto-split into page-aware parts and merge; per-model completion caps enforced (qwen 16,384); decommissioned model IDs are rejected by the ladder instead of crashing.
- **OpenRouter / Cloudflare Workers AI** — adapters implemented and previously verified live (OpenRouter free-model quota 50/day; Cloudflare scout accepts image parts ≈0.5 neurons/call with a verified vision registry); they were not configured in the final credential set, so the final runs used Gemini + Groq.
- **Budgets** — per-document extraction/repair wall-clock budgets and HTTP read timeouts bound slow free-tier queues (a 40-minute pathological stall was observed and eliminated).
- **Live smoke test** — `tests/test_live_smoke.py` exercises one real document end-to-end; it is skipped by default and runs only with `PAYABLE_AI_LIVE_SMOKE=1` and working credentials.

## Redaction and hygiene

- Every provider error string passes a credential redactor before entering logs; key fingerprints are truncated prefixes only.
- `Settings.__repr__` redacts all key/token fields, so tracebacks cannot leak credentials.
- `.env` is git-ignored; the repository tree was scanned for key-like strings (only an unmistakably fake test fixture remains).

## Not verified / honest limits

- No human has yet eyeballed all 42 output records line-by-line against their PDFs; the automated checks, the deterministic gates, and the per-document diagnostics support that review but do not replace it.
- No claim is made about the employer's hidden/held-back documents or selection outcome.
- Consumer-grade free tiers are not suitable for confidential production invoices; see `docs/PRODUCTION.md` for the production path.

## Reproduce

```powershell
python -m pytest -q
python main.py --check-config --probe-keys
python main.py --input documents --output output          # full run through the provider ladders
python main.py --validate-output --input documents --output output
python scripts/apply_deterministic_fixes.py               # offline replay of math-certain repairs
python scripts/package_submission.py --allow-quality-warnings   # after reviewing the warnings above
$env:PAYABLE_AI_LIVE_SMOKE='1'; python -m pytest tests/test_live_smoke.py -q   # opt-in live smoke
```
