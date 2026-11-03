# Bookable Payable AI — Zycus AI/ML Graduate Assignment

An evidence-driven PDF-to-payable pipeline built around the exact schema and the fixed `erp.py` calculator supplied in the candidate kit. Native PDF text extraction plus bounded local OCR, Gemini vision extraction, Groq structured text extraction (chunked for long dossiers), deterministic master-data resolution, exact ERP reconciliation, and a **bounded, signal-driven reconcile-repair loop** that the sealed calculator itself drives.

> **Honesty first:** a syntactically valid JSON file is not proof of a correct booking. The pipeline never claims a batch is correct merely because JSON files exist; `logs/run_summary.json`, `logs/run.jsonl` and `--validate-output` are part of the workflow, and every unresolved case is reported rather than papered over.

## 1. What it does

1. Discovers PDFs in an input folder and retains page boundaries in an internal text representation.
2. Reads embedded PDF text with PyMuPDF. Scanned pages are rendered for vision extraction, and local Tesseract OCR is (a) run eagerly when a text-only route will be used and (b) completed on demand so evidence quotes and repair changes can be verified against real page text.
3. Classifies each PDF (**zero, one, or several payables**) and declines what is not a payable: proformas, delivery notes, customs permits, waybills, dunning letters, statements, forms.
4. Extracts raw accounting facts into a strict intermediate object. The model may not invent missing values, merge tax rates, or move taxes between line and header levels; the prompt states *how the ERP computes* and the rules of faithful translation, not per-document answers.
5. Normalizes decimal separators, dates, currencies, and credit semantics. A deterministic **component resolver** restores a line quantity when printed row arithmetic pins it uniquely (e.g. `24,000` on a Portuguese invoice is 24.00; a pack-scaled line books as 1 price-unit), and never touches tax-inclusive totals.
6. Resolves supplier, buyer, tax, payment-term and PO codes from `master_data/` with exact-identifier-first, ambiguity-stays-blank semantics.
7. Recomputes the gross with the supplied `erp_book()` and compares it to the printed amount.
8. On mismatch, runs a **signal-driven repair loop** (bounded, converging): the deterministic layer states *facts* about what the ERP derived (per-line bases, per-tax contributions, deltas, declared-vs-component tax), the model proposes source-quoted corrections, and a change is accepted only when it is schema-clean, grounded in the page text, and mathematically improves — or, for removals, lands **exactly** on the printed gross. Corrections are never produced by arithmetic balancing.
9. Writes exactly one output JSON per input PDF plus diagnostics under `logs/`.

## 2. Architecture

```text
PDF folder
   │
   ├── PyMuPDF: embedded text + page rendering
   ├── Tesseract OCR (bounded, concurrent, on-demand completion)
   │
   ├── LLM router (provider + model ladders, key pools, pacing, budgets)
   │     ├── Gemini vision — page images + page-labelled text
   │     ├── OpenRouter vision — free vision models, reasoning disabled
   │     ├── Cloudflare Workers AI vision — verified vision models only
   │     └── Groq text — JSON mode; long documents auto-split into
   │        page-aware parts and merged (token-limited free tier)
   │
   ├── Typed-shape normalization
   ├── Deterministic repairs (math-certain, exact-match gated):
   │     line quantities pinned by printed row arithmetic → duplicated
   │     tax levels cleared → printed-formula tax products snapped
   ├── Master resolver — suppliers / tax / buyer org / terms / PO
   ├── Exact schema checks + quality checks + erp_book() reconciliation
   ├── Completeness check — line sums that reproduce no printed total
   │     trigger ONE corrective re-read (verified gap as the signal)
   ├── Signal-driven repair loop (≤ REPAIR_ROUNDS, warm conversation)
   │     facts from the ERP → model judgment → grounded, verifiable edits
   └── output/<PDF-stem>.json + logs/run.jsonl
```

The components are intentionally separated. The LLM interprets the document and owns judgment; ordinary Python owns numeric normalization, schema checks, matching, path allow-lists, the math-certain repairs, and the final accounting recomputation. See [ARCHITECTURE.md](ARCHITECTURE.md) for the flow diagram, the repair protocol, and the provider/quota design. See [DESIGN.md](DESIGN.md) for what the documents actually teach. See [VERIFICATION.md](VERIFICATION.md) for what was actually run, document by document.

## 3. Setup

### Prerequisites

- Python 3.10+.
- At least one provider credential: Gemini (Google AI Studio), Groq (Groq Console), OpenRouter (openrouter.ai), and/or Cloudflare Workers AI (`CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`). The router uses whichever are configured.
- Tesseract OCR for scanned-page text (used for evidence verification and text-only routes; the vision routes still work without it, but repairs on scanned pages then fail closed). The code auto-discovers common install locations and honours `TESSERACT_CMD`.
- Optional: Docker (see §8).

### Install (Windows PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Open `.env` and fill your keys. **Keys never belong in source, logs, screenshots, or the submission archive.** `.env` is git-ignored.

### Keys and free-tier realities (read this before a batch)

Everything below is real behavior observed against free-tier quotas; the pipeline is engineered for it:

- **Multiple keys, mixed formats, one pool.** Any number of keys can be supplied. All are opaque strings (classic `AIza…` and newer `AQ.…` formats both work):
  ```dotenv
  GEMINI_API_KEY=key1,key2,key3        # comma / semicolon / whitespace separated
  GEMINI_API_KEYS=key1 key2            # explicit list
  GEMINI_API_KEY_2=key2                # numbered extras (_2.._9)
  ```
  Keys are health-tracked and rotated: an auth-invalid key is dropped for the session; a quota-limited key cools down for the provider's own retry window; the others take over. Key values never reach a log line — every error string passes through a redactor, and `Settings.__repr__` redacts key fields so tracebacks cannot leak them either.
- **Four providers, verified live.** Gemini (vision), Groq (text), OpenRouter (vision-capable free models), and Cloudflare Workers AI (vision) are first-class routes with the same key pooling, health states, and per-model ladders:
  - **Gemini** — per-model daily buckets (observed `20/day` on this account); rolls model-by-model, then provider-by-provider. Worst-case per-document wall time is bounded by `EXTRACT_BUDGET_SECONDS`/`REPAIR_BUDGET_SECONDS`.
  - **Groq** — token-metered (observed `8,000 tokens/minute`); long documents auto-split into page-aware parts (`GROQ_MAX_DOCUMENT_CHARS`) and merge; per-model completion caps enforced (qwen: 16,384).
  - **OpenRouter** — free-model quota verified via the key endpoint (`50 free-model requests/day`); extraction turns explicitly disable reasoning (`reasoning: {enabled: false}`) because reasoning models otherwise burn the whole token budget before emitting JSON; upstream-rate-limited models are parked briefly without poisoning the key.
  - **Cloudflare Workers AI** — OpenAI-compatible route; vision gated by a verified model registry so images are never sent to a text-only model; neuron-metered (≈0.5 neurons per page call observed).
  - `PROVIDER_LADDER_VISION` / `PROVIDER_LADDER_TEXT` control the order; unavailable providers are skipped fast instead of sleeping through cooldowns.
- **A daily limit is never slept through.** Cooldowns longer than the batch window fail that route fast and move on; short rate-limit cooldowns wait briefly. `MIN_CALL_INTERVAL_SECONDS` (default 5 s) paces requests under per-minute limits.
- **Budget a full pass.** One request per document on the chosen route, plus at most `REPAIR_ROUNDS` extra requests for documents that fail to reconcile and one corrective re-read only when the completeness check finds lines that reproduce no printed total. `--limit` and `--only` let you spend quota only where it matters.
- **Free-tier data note:** provider pricing pages indicate free-tier content may be used to improve provider products. Do not send confidential financial documents through free tiers without a data-handling decision; use a business/paid arrangement for production.

Example `.env`:

```dotenv
GEMINI_API_KEY=your_gemini_key_or_pool_here
GROQ_API_KEY=your_groq_key_here
OPENROUTER_API_KEY=your_openrouter_key_here
CLOUDFLARE_API_TOKEN=your_workers_ai_token
CLOUDFLARE_ACCOUNT_ID=your_account_id
GEMINI_MODEL=gemini-3.8-flash
GEMINI_FALLBACK_MODELS=gemini-3.6-flash,gemini-3-flash-preview,gemini-2.5-flash
GROQ_MODEL=qwen/qwen3.8-27b
GROQ_FALLBACK_MODELS=openai/gpt-oss-120b,openai/gpt-oss-20b
OPENROUTER_MODEL=nvidia/nemotron-3.5-super-vl:free
OPENROUTER_FALLBACK_MODELS=google/gemma-4-31b-it:free,dots-studio/dots-3-note-preview:free
CF_MODEL=@cf/meta/llama-4-scout-17b-16e-instruct
CF_FALLBACK_MODELS=@cf/meta/llama-3.2-11b-vision-instruct
PROVIDER_LADDER_VISION=gemini,openrouter,cloudflare,groq
PROVIDER_LADDER_TEXT=groq,openrouter,cloudflare,gemini
PAYABLE_AI_PROVIDER=auto
MIN_CALL_INTERVAL_SECONDS=5
REPAIR_ROUNDS=2
EXTRACT_BUDGET_SECONDS=480
REPAIR_BUDGET_SECONDS=240
GROQ_MAX_DOCUMENT_CHARS=8000
MAX_VISION_PAGES=20
PDF_RENDER_DPI=120
OCR_MODE=auto
OCR_WORKERS=4
OCR_PAGE_TIMEOUT_SECONDS=15
MAX_DOCUMENT_CHARS=90000
MAX_OUTPUT_TOKENS=24576
LLM_RETRIES=2
REPAIR_ON_MISMATCH=true
```

Model IDs change; the API itself tells you what exists (`--check-config --probe-keys` lists per-key status with a free metadata call, and model-not-found errors roll the ladder).

## 4. Run and test

```powershell
# A. Verify keys/SDKs (no tokens; --probe-keys adds one metadata call per key)
python main.py --check-config --probe-keys

# B. Local PDF/OCR diagnosis (no LLM calls)
python main.py --diagnose --input documents

# C. Smoke-test two documents before spending quota
python main.py --input documents --output output --limit 2

# D. The submission command — full folder
python main.py --input documents --output output

# E. Re-run only specific documents (comma-separated stems or globs)
python main.py --only "INV-06,INV-21,DU-0*"

# F. Force one provider (debugging)
python main.py --provider gemini --only INV-25
python main.py --provider groq   --only INV-25

# G. Offline validation of existing outputs — no API calls
python main.py --validate-output --input documents --output output

# H. Replay the deterministic (math-certain) repairs over existing outputs — no API calls
python scripts/apply_deterministic_fixes.py

# I. Tests (deterministic; the live smoke test is skipped unless enabled)
python -m pytest -q

# J. Live provider smoke test (opt-in; needs credentials and quota)
$env:PAYABLE_AI_LIVE_SMOKE='1'; python -m pytest tests/test_live_smoke.py -q
```

The run writes `output/<PDF-stem>.json`, one per input PDF, and streams diagnostics to `logs/run.jsonl` (provider attempts, per-line ERP facts, repair rounds, master matches, evidence audits). On an unrecoverable provider failure the batch continues but marks the file `ERROR_NO_VALID_EXTRACTION` — **that placeholder is not a submission artifact**; re-run it after fixing the cause. `--validate-output` reports coverage, schema, semantics, and reconciliation without calls. The test suite is deterministic and offline by default; the live smoke test is the only module that touches a provider and is skipped unless `PAYABLE_AI_LIVE_SMOKE=1` is set.

## Repository layout

```text
├── main.py                      CLI: batch run, --only, --diagnose, --check-config, --validate-output
├── erp.py                       sealed ERP recompute (unchanged from the candidate kit)
├── payable_ai/
│   ├── pipeline.py              orchestration, bounded repair loop, completeness re-read
│   ├── llm.py                   provider adapters + pools + ladders + budgets
│   ├── keys.py                  key pools, rotation, pacing, redaction
│   ├── prompting.py             stable-prefix prompts; signals-driven repair protocol
│   ├── signals.py               deterministic ERP facts + completeness signal
│   ├── validation.py            schema, reconciliation, grounding, deterministic repairs
│   ├── normalization.py         numbers, dates, currency (incl. legacy ISO codes), credit semantics
│   ├── master_data.py           indexed master-data resolution (exact-first, ambiguity blank)
│   ├── quality.py               semantic checks and honest flags
│   ├── evidence.py              source-quote verification
│   ├── pdf_io.py                PyMuPDF text, page rendering, bounded OCR, page-aware splitting
│   └── config.py                environment-driven settings (redacted repr)
├── scripts/
│   ├── package_submission.py    validation-gated submission packaging
│   ├── apply_deterministic_fixes.py   offline replay of math-certain repairs
│   └── rerun_failing.py         re-run only documents that fail reconciliation
├── tests/                       deterministic unit/integration tests + opt-in live smoke
├── documents/                   the 42 candidate PDFs
├── master_data/                 supplied reference data
├── output/                      one generated JSON per input PDF (graded artifacts)
└── docs/PRODUCTION.md           accuracy, latency, cost, reliability, compliance plan
```

## 5. Output contract

Each output matches the candidate brief's outer shape (`file`, `payables[]`, `declined[]`) and every payable matches `AUTODRAFT_SCHEMA.md` exactly — no extra keys, all values strings. Diagnostics — evidence quotes, match reasons, ERP traces, repair decisions — live in `logs/`, not in the payable record. Credit memos use `invoice_type: "CREDIT_MEMO"` with positive magnitudes, per the brief.

## 6. How the solver works (for reviewers)

The loop is the point, so here is the machinery in one paragraph each: **(1) Signals, not instructions** — `payable_ai/signals.py` computes an ERP trace (per-line base from `quantity × unit_price − discounts`, per-tax contributions and their bases, header net base, charges) that is refused unless it sums to the sealed `erp_book()` result, then states deltas as facts (“the ERP derives 100.00 on this line; the page prints 119.00”). The model chooses the remedy. **(2) Determinism boundary at ambiguity** — math-certain repairs are owned by deterministic code, each exact-match gated against the document's own printed gross: a quantity uniquely pinned by printed row arithmetic is restored (locale misreads, pack scaling); the same tax placed at both levels is de-duplicated (`resolve_duplicate_taxes`); a tax displayed rounded but derived from a printed rate × printed base is snapped to the exact product (`recompute_printed_formula_taxes`). Everything requiring world knowledge or document inference stays the model's call. **(3) One flowing context, cache-friendly** — the extraction reference is a byte-stable prefix; on Gemini the repair turn continues the *same conversation* (the model re-sees its own draft and the page images). On the stateless OpenAI-compatible routes (OpenRouter/Cloudflare), repairs run text-only over the OCR/native transcript — the same text the grounding gate verifies quotes against — because re-uploading the page images on every turn made free-tier calls arbitrarily slow. **(4) Cost routing** — vision only where layout matters, a text route for searchable documents, per-model fallback ladders, free-tier pacing, per-call read timeouts and per-document wall-clock budgets, and spend that ends the moment reconciliation passes. **(5) Completeness re-read** — when the extracted line amounts reproduce none of the document's printed totals (rows dropped or mis-transcribed — a transcription deficiency, not a placement one), one corrective re-read of the same document runs with the verified gap as a signal, and the better-scoring attempt is kept.

## 7. Known limits (kept honest)

- OCR can confuse separators, `0/O`, `1/I`, minus signs, and table columns; a low-confidence page routes to vision, never to silent confidence.
- Dense tables need a strong reader: on the 35-row Malaysian invoice, two independent high-magnification reads (and a free-tier vision model) missed a row whose amount is printed beside a section label; the completeness signal caught the un-footable draft, and the strong vision pass found the row. Free-tier models are economical, not infallible — review the flagged warnings.
- The deterministic repairs only fire on exact arithmetic; ambiguous cases are left to extraction and the repair loop. A tax-inclusive or pack-scaled line requires the model to translate the printed presentation into ERP components; the layer verifies the result, it cannot manufacture the translation.
- Buyer identity, currencies and invoice numbers can be genuinely absent from a document; a blank is the correct answer then, not a guess (documented in `VERIFICATION.md` and `REVIEW_NOTES.md`).
- **Master-data findings (investigated):** this corpus's issuing parties are not rows of the supplied sample masters (verified by reading the issuer blocks on the pages), and the documents' bill-to tenants are not the chart's Bolt entities; blank codes are therefore the honest result. The resolver does match when data corresponds — the kit's worked sample resolves supplier `2845695`, covered by tests. Legacy currency codes are normalized to current ISO 4217 (e.g. `GHC` → `GHS`).
- **Deferred fidelity re-reads:** four records book to the cent but would benefit from a fresh vision pass for field-level fidelity (`INV-01`/`INV-16` supplier field currently holds the recipient; `INV-06` carries inclusive prices with the VAT flagged as unrepresented; `HLD-08` should capture the “Your Reference” PO; `HLD-05`'s lines restate printed incidence bases as quantities of 1). The general rules that fix all of these are in the prompt, and the faithful HLD-05 representation is proven to book exactly in the regression tests; executing them requires provider credentials, which were unavailable in the final environment.
- Free tiers have hard, documented quotas (per-model daily buckets, token-per-minute caps, free-model request caps). The ladders, pacing, budgets and resumability make a batch survivable, not instant.

## 8. Docker (alternative single command)

```bash
docker build -t bookable-payable .
docker run --rm -e GEMINI_API_KEY=... -e GROQ_API_KEY=... -v "$PWD/output:/app/output" bookable-payable
```

The image installs Tesseract (English/German/Portuguese language data) and runs the same `python main.py --input documents --output output`. The venv-based flow in §3 is the primary path; Docker is provided so the system can be re-run anywhere with one command.

## 9. Production path

Accuracy, latency, cost, reliability, compliance, and rollout are specified in [docs/PRODUCTION.md](docs/PRODUCTION.md). In short: a labeled gold set with field-level metrics, human review for every flagged record, queue-based idempotent processing, OCR caching, model-to-difficulty routing, per-tenant budgets, encrypted storage with a provider data-processing agreement (free tiers do not qualify), and a shadow → assist → sampled-auto-book rollout with a kill switch.

## 10. Submission hygiene

Before zipping/committing: run the full folder with no `--limit`; inspect `logs/run_summary.json` and every non-`MATCH` record; confirm one output per input; remove `.env`, `.venv`, caches and local logs from the archive; keep `output/*.json`, source, `requirements.txt`, docs, `erp.py`, `AUTODRAFT_SCHEMA.md`, `master_data/`, `documents/` and tests. `scripts/package_submission.py` validates coverage, schema, master-code existence and reconciliation, and refuses to package placeholders or unreconciled payables (use `--allow-quality-warnings` only after manual review).
