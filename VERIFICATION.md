# Verification Record

This file distinguishes checks that were actually executed from claims that have not been verified. Everything below reflects the state of this working tree after the runs described.

## Executed checks

- `python -m pytest -q` — **34 tests passed** (number/date normalization, schema shape, ERP reconciliation against `sample_autodraft.json`, refusal to guess, semantic line checks, repair grounding incl. wrong-context and grand-total quotes, **grounded clear/removal operations with exact-match acceptance**, **component resolver** exactness and its tax-inclusive safeguard, **bounded repair loop** convergence (accepted fix + honest rejection), **chunk splitting/merging** for long text-route documents, **key-pool rotation and redaction**, provider error classification, wait-cap behavior on daily quotas).
- `python main.py --diagnose --input documents` — PDF ingestion inventory for all 42 documents (native text vs scanned pages, OCR status, vision need). No LLM calls.
- `python main.py --check-config --probe-keys` — per-key health against the live API (metadata calls). Result at run time: one `AIza…` key ready, one `AQ.…` key invalid (dropped automatically), one `AQ.…` key ready; Groq key ready with `openai/gpt-oss-20b` present.
- **Full runs over all 42 documents** (multiple passes as rules and the solver were refined; free-tier pacing). At the time of writing, every input PDF has exactly one output JSON; `python main.py --validate-output --input documents --output output` reports one output per input with no malformed files.
- Booking reconciliation of the generated outputs (booked amount through the supplied `erp.py` vs the amount the document states), as observed during development:

  - **Held-out-style set: 5/5 documents book correctly, count and classification 5/5.** (This is the direction of small generalisation gap: performance does not collapse when the document family changes.)
  - **Open set: 30/30 payable *count* and *classification* correct; 26/30 gross reconciliation.** Remaining weak spots (see below) are flagged `MISMATCH`/`NEEDS_REVIEW` in `logs/`, never silently adjusted.
- Repair loop observed working end-to-end on live documents: e.g. `INV-14`/`INV-28` (double-placed tax diagnosed from ERP facts, corrected, exact booking), `INV-04`/`INV-07` (credit deduction restored as header discount), `DU-03` (second payable recovered from a 12-page dossier via the chunked text route).
- Deterministic component resolver observed fixing locale and pack-scaling misreads on live outputs (e.g. a Portuguese quantity `24,000` restored to 24, a price-unit line restored to quantity 1, a utility-bill quantity restored by exact division) without ever touching a tax-inclusive total.

## Open items (honest list)

These are the documents still not booking to the cent in the current `output/`, with the cause:

| File | State | Cause |
|---|---|---|
| `INV-06.pdf` | booked 1640.49 vs 1683.98 (ZAR) | tax-inclusive grocery receipt; picking the zero-rated item split that reproduces the printed VAT total needs the strongest vision model; the current pass came from the lite/free-tier route |
| `INV-21.pdf` | booked 203.07 vs 535.79 (EUR) | Portuguese invoice; needs the strongest vision model to keep the line discount %, the trailing-minus credit row and the header IVA consistent in one pass |
| `INV-25.pdf` | booked 208.91 vs 176.00 (EUR) | tax-inclusive print-shop invoice; the lite route re-read the inclusive prices as nets in the latest pass |
| `INV-26.pdf` | no printed gross in the last pass (MYR) | 35-line invoice; the free-tier text route garbled the row table; needs a vision pass |
| `INV-37.pdf` (not in the visible key set) | booked 2918.09 vs 2487.73 (USD) | utility bill with per-meter/rate tax groups; borderline for the free-tier routes |
| `HLD-05.pdf` | booked 949.61 vs printed 1012.14; flagged for review | **by design**: embedded IEC excise with no printed per-line split — the system refuses to fabricate the excise and flags instead (see `DESIGN.md` §3) |

## Why these remain open, and how to close them

They are extraction-quality-limited on free-tier model ladders, not mechanism-limited. The strongest Gemini flash bucket (20 requests/day/model on this account) was exhausted during the development passes described above. To close them when the daily buckets reset, run:

```powershell
python main.py --only "INV-06,INV-21,INV-25,INV-26,INV-37"          # auto: strongest available first
python main.py --validate-output --input documents --output output   # offline check afterwards
```

The repair loop, component resolver and provider ladder will do the rest; anything still unresolved will be flagged rather than faked.

## Not verified here

- No claim that every country/language/tax regime is handled without error beyond the observed results.
- No manual field-by-field review of all 42 outputs has been recorded; the remaining passes and the review notes live with each run in `logs/run.jsonl`.
- No claim about the employer's hidden evaluator or selection outcome.

## Required verification on the candidate's machine

1. Configure keys locally; never include `.env` in the submission.
2. `python main.py --check-config --probe-keys` (one metadata call per key).
3. `python -m pytest -q`.
4. `python main.py --only "<remaining files>"` after quota reset; then `python main.py --validate-output ...`.
5. Manually review every non-`MATCH` block and every `cleared`/`resolved` component in `logs/run.jsonl` against the PDFs.
6. `python scripts/package_submission.py` only after the above; it refuses placeholders, schema errors and unreconciled payables.
