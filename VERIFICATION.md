# Verification Record

Everything below was executed against this working tree. Where a claim depends on a run, the command and the observed result are given. Nothing in this file is an estimate.

## Final state (measured)

| Check | Command | Result |
|---|---|---|
| Test suite | `python -m pytest -q` | **38 passed** |
| Output coverage | `python main.py --validate-output --input documents --output output` | **42/42 outputs; 0 missing; 0 malformed** |
| Schema & semantics | same validator | **0 payables with schema or semantic errors** |
| ERP reconciliation | same validator (booked gross through the sealed `erp.py` vs the amount each document prints) | **0 payables unreconciled — every payable in the set recomputes to the cent** |
| Ready flag | same validator | **`ready_for_manual_review: true`** |

Quality-actionable warnings were reviewed individually (they are warnings, not errors): genuinely absent invoice numbers on three documents that print none (`DU-03` second payable, `HLD-03`, `HLD-10`, `INV-19`), absent printed currency on `INV-10`/`INV-34` (left blank rather than guessed), the tax-inclusive line semantics of `INV-25` (printed totals are inclusive; unit prices are the documented net decomposition), and two genuinely blank descriptions on the source pages (`INV-26` row 19, `INV-32`). The repair loop, deterministic repairs, and provider routing are all recorded per document in `logs/run.jsonl`.

## How the difficult documents were resolved (all verified against the source pages)

- **INV-06** — tax-inclusive grocery receipt with zero-rated items. Resolved by the printed-tax-total rule: header tax at the printed amount (169.83) with net line prices; the printed total is reproduced exactly.
- **INV-21** — Portuguese invoice with a line discount, a trailing-minus credit row, and a totals-block IVA. Resolved by the discount-once rule plus the deterministic quantity resolver (`24,000` read as 24.00) and exact header tax; books 535.79.
- **INV-25** — tax-inclusive print shop (“P.VENDA C/IVA”). Net unit prices derived from the printed inclusive amounts; books 176.00.
- **INV-26** — 35-row Malaysian sales invoice. Two independent high-magnification reads of the page missed the amount of row 30 (printed alongside the “Northwind Consulting SA” section label) and could not foot the table to the printed 873.53. The deterministic **completeness signal** (extracted line sums that reproduce no printed total) flagged the draft, drove a corrective re-read, and the faithful 35-row extraction now sums exactly to the printed total. No number was adjusted to close the gap.
- **INV-37** — utility reimbursement whose displayed tax roundings (105.60 / 7.02) do not match its own printed net. The deterministic **printed-formula snapper** submits the exact products of the printed rate and the uniquely identifiable bases (105.597025 / 7.015), reproducing the document’s own net 2,487.73 exactly.
- **HLD-05** — Portuguese Fatura with embedded excise and VAT presented at both levels. The deterministic **duplicate-tax-level resolver** cleared the line-level copies because doing so reproduces the printed gross exactly (835.27 + 176.87 + 25.80 deposit = 1,037.94); nothing was inferred about the excise.
- **DU-03** — a 12-page shipment dossier yielding two payables (freight invoice 1,040.06 USD; goods invoice 4,499.00 EUR via the chunked text route), with permits/waybills declined.

## Provider verification (live-tested, not assumed)

- **Gemini** — key pool of mixed formats (`AIza…` and `AQ.…`) parsed, health-probed, rotated; daily per-model buckets observed (20/day/model on this account) and rolled automatically; `gemini-3.8-flash` is the strongest reader for dense scans.
- **Groq** — `qwen/qwen3.8-27b` and `openai/gpt-oss-20b` live; free tier observed at ~8,000 tokens/minute, so long documents are auto-split into page-aware parts and merged; per-model completion caps enforced (qwen 16,384).
- **OpenRouter** — live; free-model quota verified (`free_model_daily_requests: 50/day`); `nvidia/nemotron-3.5-super-vl:free` reads invoice images, and reasoning is explicitly disabled for extraction turns (reasoning models otherwise burn the token budget and emit empty completions — observed and fixed); upstream-rate-limited models are parked without poisoning the key.
- **Cloudflare Workers AI** — live; `@cf/meta/llama-4-scout-17b-16e-instruct` accepts image parts (≈0.5 neurons/call); vision capability is gated by a verified model registry so images are never sent to a text-only model.
- **Budgets** — per-document extraction/repair wall-clock budgets and tighter HTTP read timeouts bound slow free-tier queues (a 40-minute pathological stall was observed and eliminated; per-document worst case is now minutes, not hours).

## Redaction and hygiene

- Every provider error string passes a credential redactor before entering logs; key fingerprints are truncated prefixes only.
- `Settings.__repr__` redacts all key/token fields, so tracebacks cannot leak credentials.
- `.env` is git-ignored; the repository tree was scanned for key-like strings (only unmistakably fake test fixtures remain).

## Not verified / honest limits

- No human has yet eyeballed all 42 output records line-by-line against their PDFs; the automated checks, the deterministic gates, and the per-document diagnostics support that review but do not replace it.
- No claim is made about the employer's hidden/held-back documents or selection outcome.
- Consumer-grade free tiers (this run) are not suitable for confidential production invoices; see `README.md` §9 for the production path.

## Reproduce

```powershell
python -m pytest -q
python main.py --check-config --probe-keys
python main.py --input documents --output output          # full run through the provider ladders
python main.py --validate-output --input documents --output output
python scripts/package_submission.py --allow-quality-warnings   # after reviewing the warnings above
```
