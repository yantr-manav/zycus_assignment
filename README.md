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
   ├── LLM router (provider + model ladder, key pool, pacing)
   │     ├── Gemini vision — page images + page-labelled text
   │     └── Groq text — JSON mode; long documents auto-split into
   │        page-aware parts and merged (token-limited free tier)
   │
   ├── Typed-shape normalization + deterministic component resolver
   ├── Master resolver — suppliers / tax / buyer org / terms / PO
   ├── Exact schema checks + quality checks + erp_book() reconciliation
   ├── Signal-driven repair loop (≤ REPAIR_ROUNDS, warm conversation)
   │     facts from the ERP → model judgment → grounded, verifiable edits
   └── output/<PDF-stem>.json + logs/run.jsonl
```

The components are intentionally separated. The LLM interprets the document and owns judgment; ordinary Python owns numeric normalization, schema checks, matching, path allow-lists, deterministic component restoration, and the final accounting recomputation. See [ARCHITECTURE.md](ARCHITECTURE.md) for the flow diagram, the repair protocol, and the provider/quota design. See [DESIGN.md](DESIGN.md) for what the documents actually teach. See [VERIFICATION.md](VERIFICATION.md) for what was actually run and what is still open.

## 3. Setup

### Prerequisites

- Python 3.10+.
- A Gemini API key (Google AI Studio) and/or Groq API key (Groq Console).
- Tesseract OCR for scanned-page text (used for evidence verification and text-only routes; the vision route still works without it, but repairs on scanned pages then fail closed). The code auto-discovers common install locations and honours `TESSERACT_CMD`.
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
  Keys are health-tracked and rotated: an auth-invalid key is dropped for the session; a quota-limited key cools down for the provider's own retry window; the others take over. Key values never reach a log line — every error string passes through a redactor.
- **Daily quota is per model.** Free-tier Gemini limits requests per model per day (observed: `20/day` for a flash model, reset window returned by the API as a multi-hour retry delay). The router therefore carries a **model ladder** (`GEMINI_MODEL` + `GEMINI_FALLBACK_MODELS`) and rolls to the next model when one bucket is exhausted, and to the next provider when all are. The same ladder exists for Groq (`GROQ_MODEL` + `GROQ_FALLBACK_MODELS`, e.g. `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`).
- **A daily limit is never slept through.** Cooldowns longer than the batch window fail that route fast and move on; short rate-limit cooldowns wait briefly. `MIN_CALL_INTERVAL_SECONDS` (default 5 s) paces requests under per-minute limits.
- **Groq free tier is token-limited** (observed: 8,000 tokens/minute). Long scan dossiers exceed it in one request, so the text route auto-splits the document into page-aware parts below `GROQ_MAX_DOCUMENT_CHARS` and merges the payables (deduplicating an invoice that spans a part boundary). Smaller models may also cap completion tokens (qwen: 16,384) — the router clamps per model.
- **Budget a full pass.** One request per document on the chosen route, plus at most `REPAIR_ROUNDS` extra requests for documents that fail to reconcile. `--limit` and `--only` let you spend quota only where it matters.
- **Free-tier data note:** provider pricing pages indicate free-tier content may be used to improve provider products. Do not send confidential financial documents through free tiers without a data-handling decision; use a business/paid arrangement for production.

Example `.env`:

```dotenv
GEMINI_API_KEY=your_gemini_key_or_pool_here
GROQ_API_KEY=your_groq_key_here
GEMINI_MODEL=gemini-3.8-flash
GEMINI_FALLBACK_MODELS=gemini-3.6-flash,gemini-3-flash-preview,gemini-2.5-flash
GROQ_MODEL=openai/gpt-oss-20b
GROQ_FALLBACK_MODELS=openai/gpt-oss-120b,qwen/qwen3.8-27b
PAYABLE_AI_PROVIDER=auto
MIN_CALL_INTERVAL_SECONDS=5
REPAIR_ROUNDS=2
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

# H. Tests
python -m pytest -q
```

The run writes `output/<PDF-stem>.json`, one per input PDF, and streams diagnostics to `logs/run.jsonl` (provider attempts, per-line ERP facts, repair rounds, master matches, evidence audits). On an unrecoverable provider failure the batch continues but marks the file `ERROR_NO_VALID_EXTRACTION` — **that placeholder is not a submission artifact**; re-run it after fixing the cause. `--validate-output` reports coverage, schema, semantics, and reconciliation without calls.

## 5. Output contract

Each output matches the candidate brief's outer shape (`file`, `payables[]`, `declined[]`) and every payable matches `AUTODRAFT_SCHEMA.md` exactly — no extra keys, all values strings. Diagnostics — evidence quotes, match reasons, ERP traces, repair decisions — live in `logs/`, not in the payable record. Credit memos use `invoice_type: "CREDIT_MEMO"` with positive magnitudes, per the brief.

## 6. How the solver works (for reviewers)

The loop is the point, so here is the machinery in one paragraph each: **(1) Signals, not instructions** — `payable_ai/signals.py` computes an ERP trace (per-line base from `quantity × unit_price − discounts`, per-tax contributions and their bases, header net base, charges) that is refused unless it sums to the sealed `erp_book()` result, then states deltas as facts (“the ERP derives 100.00 on this line; the page prints 119.00”). The model chooses the remedy. **(2) Determinism boundary at ambiguity** — math-certain components (a quantity uniquely pinned by printed row arithmetic; a neutralization whose candidate lands exactly on the printed gross) are owned by deterministic code; everything that requires world knowledge or document inference is the model’s call. **(3) One flowing context, cache-friendly** — the extraction reference is a byte-stable prefix; the repair turn continues the *same conversation* (the model re-sees its own draft and the page images) instead of re-sending a fragmented prompt. **(4) Cost routing** — vision only where layout matters, a text route for searchable documents, per-model fallback ladders, free-tier pacing, and per-document spend that ends the moment reconciliation passes.

## 7. Known limits (kept honest)

- OCR can confuse separators, `0/O`, `1/I`, minus signs, and table columns; a low-confidence page routes to vision, never to silent confidence.
- Some documents are **not deterministically reconcilable from the page alone** (e.g. an embedded excise duty whose per-line split exists only as an inference). Those are flagged for review — never “fixed” by inventing a per-line duty. See `DESIGN.md` for the specific document.
- The component resolver only restores quantities when the arithmetic is exact; ambiguous cases are left to extraction and the repair loop.
- A tax-inclusive or pack-scaled line requires the model to translate the printed presentation into ERP components; the layer verifies the result, it cannot manufacture the translation.
- Buyer identity can be genuinely absent from a document; a blank code is the correct answer then, not a guess.

## 8. Docker (alternative single command)

```bash
docker build -t bookable-payable .
docker run --rm -e GEMINI_API_KEY=... -e GROQ_API_KEY=... -v "$PWD/output:/app/output" bookable-payable
```

The image installs Tesseract (English/German/Portuguese language data) and runs the same `python main.py --input documents --output output`. The venv-based flow in §3 is the primary path; Docker is provided so the system can be re-run anywhere with one command.

## 9. Production path (what changes at scale)

This is a production-shaped prototype, not a certified AP system. The deliberate upgrades for a real deployment:

- **Throughput:** replace sequential per-file processing with a queue (one worker per document), provider batch APIs for vision, and idempotent per-document jobs keyed by a content hash so retries never double-book.
- **Quota & cost:** move to paid tiers with per-tenant budgets; keep the model ladder for version rollouts; add token accounting per document; cache OCR text keyed by document hash (OCR is the expensive local step).
- **Accuracy:** a labeled gold set with per-field precision/recall (not just gross reconciliation), regression tests for every fixed failure, and prompt/model versioning with shadow evaluation before rollout.
- **Control:** a human-review queue for every non-`MATCH`, every cleared component, and every below-confidence vision read; four-eyes approval before anything reaches payment; immutable audit events for each accepted change.
- **Security/compliance:** encrypted storage, PII retention limits, access controls, and a provider data-processing arrangement that forbids training on the tenant’s documents (the free tiers do not qualify).
- **Scale of reference data:** replace flat JSON masters with indexed lookups (the resolver’s contract — exact IDs first, ambiguity blank — is built to survive a move to a database or search index).

## 10. Submission hygiene

Before zipping/committing: run the full folder with no `--limit`; inspect `logs/run_summary.json` and every non-`MATCH` record; confirm one output per input; remove `.env`, `.venv`, caches and local logs from the archive; keep `output/*.json`, source, `requirements.txt`, docs, `erp.py`, `AUTODRAFT_SCHEMA.md`, `master_data/`, `documents/` and tests. `scripts/package_submission.py` validates coverage, schema, master-code existence and reconciliation, and refuses to package placeholders or unreconciled payables (use `--allow-quality-warnings` only after manual review).
