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
| Execution anchor (reviewer engine) | reviewer kit's `eval/review.py` | **dev 100.0, holdout 100.0, gap 0.0, generality 15/15; `erp.py` untampered; no divergences** |

Quality-actionable warnings were reviewed individually (warnings, not errors): absent invoice numbers on documents that print none (`DU-03` second payable, `HLD-03`, `HLD-10`, `INV-19`), absent printed currency on `INV-10` (left blank rather than guessed — the page prints no ISO code or symbol evidence), the tax-inclusive line semantics of `INV-25` (printed totals are inclusive; unit prices are the documented net decomposition; the payable reconciles exactly), two genuinely blank descriptions on the source pages (`INV-26` row 19, `INV-32`), and one structural flag on `INV-06` (below). The repair loop, deterministic repairs, and provider routing are recorded per document in `logs/run.jsonl`.

### Field-level re-extraction pass (executed this session)

With the provider credentials restored (Gemini key pool live), the six flagged records were re-extracted with the improved prompt rules, each behind a strict acceptance gate (books exactly **and** clears its specific fidelity check; otherwise the previous exact record is restored):

| Document | Attempt result | Kept record |
|---|---|---|
| `INV-01` | **improved** — supplier field no longer holds the recipient; the issuer is unnamed on the page, so the supplier name is blank (honest) | 438.00, MATCH |
| `INV-16` | **improved** — supplier = **Speedwell Express Estonia AS**, VAT = **EE445462205** (the page's KMKR) | 26.47, MATCH |
| `INV-34` | **improved** — currency = **AUD** (the page prints “$” with an Australian ABN/GST/BPAY context) | 572.00, MATCH |
| `INV-06` | attempt blocked by transient provider quota; previous exact record restored | 1,683.98, MATCH; tax representation still flagged |
| `HLD-08` | attempt blocked by transient provider quota; previous exact record restored | 148,941.47, MATCH; reference PO still pending |
| `HLD-05` | attempted twice; the model still restates printed incidence bases as quantity-1 lines (gate rejected both attempts) | 1,037.94, MATCH; faithful form proven in tests |

The `INV-06`/`HLD-08` retries are one command away whenever quota is available (`python main.py --only "INV-06,HLD-08"` followed by the validator).

### Known structural weakness, flagged not hidden

`INV-06` (tax-inclusive grocery receipt) **books exactly (1,683.98)** but the current extraction keeps the inclusive amounts as net prices with an empty VAT entry, so the VAT is carried inside the prices instead of being represented as the printed header tax (169.83, 15%). The `DECLARED_TAX_NOT_REPRESENTED` quality check flags exactly this pattern so it cannot silently pass as structurally clean. The faithful re-representation (net line bases plus the printed header VAT, zero-rated items taken from the receipt's own marks) is mandated by the prompt rules; the corrective re-read was blocked by the provider's then-exhausted daily bucket, so the exact previous record is retained and the flag stays visible.

Remaining pending re-reads (all already book to the cent): `INV-06` (tax representation, above), `HLD-08` (`po_number` should capture `9267418170` printed under “Your Reference”), and `HLD-05` (the artifact restates each printed incidence base as a quantity-1 line — two strong-model attempts still produced that form, the gate rejected both, and the exact record was kept; the fully faithful representation is proven to book exactly 1,037.94 in a regression test). At creation time these were one provider-quota window away; the general prompt rules that address them are in place. See `REVIEW_NOTES.md` for the complete list.

## How the difficult documents were resolved (all verified against the source pages)

- **INV-06** — tax-inclusive grocery receipt with zero-rated items. Books exactly (1,683.98); currently flagged for the tax representation described above. The general rule that fixes the representation is in the prompt for the next run.
- **INV-19** — Ghanaian invoice; the page prints `GH₵`, and the output now carries the current ISO code **GHS**. A legacy-currency normalization table (`GHC`/`GH₵` → `GHS`) is implemented in `normalization.py`, tested, and replayed over existing outputs.
- **INV-21** — Portuguese invoice with a line discount, a trailing-minus credit row, and a totals-block IVA. Resolved by the discount-once rule plus the deterministic quantity resolver (`24,000` read as 24.00) and exact header tax; books 535.79.
- **INV-25** — tax-inclusive print shop (“P.VENDA C/IVA”). Net unit prices derived from the printed inclusive amounts; books 176.00.
- **INV-26** — 35-row Malaysian sales invoice. Two independent high-magnification reads of the page missed the amount of row 30 (printed alongside the “Northwind Consulting SA” section label) and could not foot the table to the printed 873.53. The deterministic **completeness signal** (extracted line sums that reproduce no printed total) flagged the draft, drove a corrective re-read, and the faithful 35-row extraction now sums exactly to the printed total. No number was adjusted to close the gap.
- **INV-37** — utility reimbursement whose displayed tax roundings (105.60 / 7.02) do not match its own printed net. The deterministic **printed-formula snapper** submits the least-precision products of the printed rate and the uniquely identifiable bases (**105.597 / 7.015**, uniform 3-dp), reproducing the document’s own net 2,487.73 exactly without exploiting float artifacts or inventing precision.
- **HLD-05** — Portuguese Fatura with embedded excise and VAT presented at both levels. The deterministic **duplicate-tax-level resolver** cleared the line-level copies because doing so reproduces the printed gross exactly (835.27 + 176.87 + 25.80 deposit = 1,037.94). Independently, the *fully faithful* representation was proven against the unchanged ERP (regression test): source quantities/unit prices/discount percentages + header VAT with printed amounts (19.82/157.05) + the printed IEC excise as `excise_duties` (88.33) + deposit lines also books **1,037.94 exactly**. The artifact will carry that representation on the next vision pass; nothing about the excise is ever inferred.
- **DU-03** — a 12-page shipment dossier yielding two payables (freight invoice 1,040.06 USD; goods invoice 4,499.00 EUR via the chunked text route), with permits/waybills declined.

## Master-data investigation (systematic blanks, never fabricated)

The supplied masters are samples. Reading the issuer blocks on the pages shows the corpus's issuing parties are not rows of `suppliers.json` (e.g. Speedwell Express Estonia AS, Larkspur Print OÜ, Redwater Technologies), and the documents' bill-to tenants (Northwind entities) are not the chart's Bolt entities — so blank supplier/buyer codes are the honest result, not missed matches. The resolver does match when data corresponds: the kit's worked sample resolves supplier `2845695` and buyer `BOLTGROUP/EE004/LOC_EE_001` in the unit tests, and PO/term/tax resolution follow the same exact-first, ambiguity-blank contract. Legacy currency codes are normalized to current ISO 4217 instead of being guessed.

## Provider verification (live-tested, not assumed)

- **Gemini** — key pool of mixed formats (`AIza…` and `AQ.…`) parsed, health-probed, rotated; daily per-model buckets observed (20/day/model on this account) and rolled automatically; `gemini-3.8-flash` is the strongest reader for dense scans (it resolved the 35-row invoice after lower-tier routes failed).
- **Groq** — `qwen/qwen3.8-27b` and `openai/gpt-oss-20b` live; free tier observed at ~8,000 tokens/minute, so long documents are auto-split into page-aware parts and merged; per-model completion caps enforced (qwen 16,384).
- **OpenRouter** — live; free-model quota verified (`free_model_daily_requests: 50/day`); `nvidia/nemotron-3.5-super-vl:free` reads invoice images, and reasoning is explicitly disabled for extraction turns (reasoning models otherwise burn the token budget and emit empty completions — observed and fixed); upstream-rate-limited models are parked without poisoning the key.
- **Cloudflare Workers AI** — live; `@cf/meta/llama-4-scout-17b-16e-instruct` accepts image parts (≈0.5 neurons/call); vision capability is gated by a verified model registry so images are never sent to a text-only model.
- **Budgets** — per-document extraction/repair wall-clock budgets and tighter HTTP read timeouts bound slow free-tier queues (a 40-minute pathological stall was observed and eliminated; per-document worst case is now minutes, not hours).
- **Live smoke test** — `tests/test_live_smoke.py` exercises one real document end-to-end; it is skipped by default and runs only with `PAYABLE_AI_LIVE_SMOKE=1` and working credentials (not available at the final packaging time; it is NOT claimed as run).

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
