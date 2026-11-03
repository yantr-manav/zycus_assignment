# Architecture — Evidence-driven Bookable Payable AI

## Goals and invariants

The pipeline turns an input PDF into the candidate kit's exact output (`file`, `payables`, `declined`) with one strict autodraft per bookable payable. It is designed to maximize correct booking while preferring explicit, flagged uncertainty over unsupported financial facts.

Non-negotiable invariants:

- `erp.py` is imported and used unchanged; its behavior is also mirrored (self-checked) inside `signals.py` for *introspection only*, and the mirror refuses to emit a trace unless it agrees with the sealed calculator to the cent.
- The LLM proposes facts and owns judgment; deterministic Python owns normalization, the component resolver, master lookups, schema checks, quality checks, reconciliation, repair grounding, and acceptance.
- A printed gross is an observation, never a field the model or the repair loop may manipulate.
- Every accepted accounting change is source-quoted, field-context-verified, schema-clean, and either strictly improves reconciliation (value edits) or lands **exactly** on the printed gross (removals).
- A matching gross is necessary but not sufficient; a tax is represented exactly once, at the level the document states it.
- Processing failures are logged as failures. An empty placeholder output is not a successful extraction.

## System flow

```mermaid
flowchart TD
    A[Input PDF directory] --> B[PDF inventory, rendering, bounded OCR]
    B --> C{Text usable / layout needed?}
    C -- Scanned or layout-sensitive --> D[Vision ladder: Gemini -> OpenRouter -> Cloudflare]
    C -- Searchable text --> E[Text ladder: Groq -> OpenRouter -> Cloudflare -> Gemini]
    E --> F{Text over token budget?}
    F -- Yes --> G[Page-aware part splitting]
    F -- No --> H[Single request]
    D --> I[Classification + 0..N payable candidates]
    G --> J[Per-part extraction, merged payables]
    H --> I
    J --> I
    I --> K[Canonical normalization + deterministic repairs]
    K --> K1{Completeness: line sums reproduce a printed total?}
    K1 -- No --> K2[One corrective re-read with the verified gap as signal]
    K2 --> K
    K1 -- Yes --> L[Master resolver: supplier, buyer, tax, terms, PO]
    L --> M[Exact schema + semantic checks]
    M --> N[Fixed erp_book from supplied erp.py]
    N --> O{Gross equals printed amount?}
    O -- Yes --> P[Write output/X.json + diagnostics]
    O -- No, rounds left --> Q[Signal bundle: ERP facts + deltas + checks]
    Q --> R[Repair turn; warm conversation on Gemini, text-only on stateless routes]
    R --> S[Allow-list + quote verification + exact/improving acceptance]
    S -- Accepted --> M
    S -- Rejected / no change --> P
    O -- No, rounds exhausted --> P
```

## Module responsibilities

| Module | Responsibility | Important boundary |
|---|---|---|
| `payable_ai/pdf_io.py` | Embedded-text extraction, page images, bounded/concurrent OCR with auto-discovery of the Tesseract binary, page-aware trimming, page-aware splitting for token-limited routes | OCR is noisy; confidence is a routing signal, never proof |
| `payable_ai/keys.py` | Key pools (any count/format), health states (ready / cooling / invalid), rotation, pacing, secret redaction | A key value never reaches a log line; auth-invalid keys are dropped, quota-limited keys cool for the provider's own retry window |
| `payable_ai/llm.py` | Four provider routes (Gemini, Groq, OpenRouter, Cloudflare) with model ladders, per-model caps and calibration (reasoning disabled on OpenRouter extraction turns; verified vision registry for Cloudflare), chunked text route with conservative merge, correction signal on re-reads, error classification (invalid / quota / request / transient), read timeouts and per-document wall-clock budgets, redacted `Settings.__repr__` | Rotation is availability, not consensus; correctness is decided by the deterministic layer |
| `payable_ai/prompting.py` | Stable-prefix extraction reference; signal-driven repair rules; clear/removal protocol | Documents are untrusted data; prompts state _how the ERP computes_ and translation rules, never per-document answers |
| `payable_ai/signals.py` | ERP trace (per-line bases, per-tax contributions and bases, charges) + fact signals (gross delta, line-vs-printed deltas, declared-vs-component tax, missing-row gap) | Facts only — no remedies; the trace is withheld if it disagrees with the sealed calculator |
| `payable_ai/normalization.py` | Number/date/currency normalization (including legacy ISO codes such as `GHC`/`GH₵` → `GHS`), credit semantics, schema shape | Never synthesizes a missing amount; only documented mappings are applied |
| `payable_ai/validation.py` | Schema checks, reconciliation, grounded repair semantics, and the math-certain deterministic repairs: quantity resolver, duplicate-tax-level resolver, printed-formula tax snapping (least uniform precision that still reproduces the printed gross) | Removals and deterministic repairs require exact reconciliation; the quantity resolver fires only when the arithmetic pins one value; the snapper never exploits floating-point artifacts |
| `payable_ai/master_data.py` | Indexed master-data resolution | Unmatched/ambiguous codes stay blank; no generated IDs |
| `payable_ai/quality.py` | Semantic checks: line extension vs components, tax at both levels, declared vs component tax, missing evidence | Warnings trigger review; they never auto-correct money |
| `payable_ai/evidence.py` | Verifies model-returned source quotes against page text | Visual-only evidence is reported as unverified, not assumed |
| `payable_ai/pipeline.py` | Orchestration, the bounded converge loop, deterministic repairs, the completeness re-read (bounded to one, better attempt kept), diagnostics, output writing | Errors are never counted as completed payables |
| `scripts/apply_deterministic_fixes.py` | Replays the deterministic repairs over existing outputs without API calls (only commits exact-match improvements) | Touches nothing it cannot verify to the cent |
| `main.py` | CLI: run, `--only`, `--limit`, `--provider` (auto/gemini/groq/openrouter/cloudflare), `--diagnose`, `--check-config --probe-keys`, `--validate-output` | Offline validation makes no API calls |

## The repair loop in detail

1. Normalize the candidate; apply the deterministic repairs (quantity resolver, duplicate-tax-level resolver, printed-formula tax snapper); schema + quality + ERP reconcile.
2. Completeness check: if the extracted line amounts reproduce no printed total, run exactly one corrective re-read with the verified gap as a signal and keep the better-scoring attempt.
3. If the gross still disagrees: build the signal bundle (facts from the ERP trace, deltas, declared-vs-component tax, semantic check codes).
4. Repair turn on Gemini continues **the same conversation** (page images and the model's own draft stay in context — no re-upload, stable prefix, cache-friendly); on the stateless routes (OpenRouter/Cloudflare) and on provider switch, a cold repair prompt carries the source transcript instead — the same text the grounding gate verifies against.
5. Parse each proposed change against the scalar allow-list. Value edits must be quote-grounded (the proposed number must appear on the page, in the right field context, nearest the right label). Removals require only label-context grounding but are accepted only when the corrected draft reproduces the printed gross **exactly**.
6. Trial-normalize the candidate; accept only if it stays schema-clean and does not add critical semantic errors, and either reconciliation lands on MATCH or the absolute difference strictly decreases.
7. Termination: MATCH, no accepted change, or `REPAIR_ROUNDS` (default 2). Every round is logged with its signals and outcome, so a reviewer can see convergence (or its absence) rather than a black box.

## Prompt assembly (cache and attention)

- Every call is `[stable prefix][variable tail]`: system instruction + extraction reference (byte-stable across all documents) precede filename, document text, draft and task.
- The item to act on (page text, or the draft under repair with its facts) sits at the **bottom** of the turn, where attention is most reliable.
- On Gemini, extraction and repair share one conversation per document, so the provider's implicit prefix cache can hit on the static prefix and the repair turn pays only for its delta.

## Text and OCR tooling (evaluated, deliberately scoped)

`PyMuPDF` provides embedded text, page rendering, and page images for the vision routes; Tesseract provides local OCR for scanned pages and the text-only routes. `pdfplumber`, `Docling`, and `PaddleOCR` were evaluated and **not adopted**: every digital-PDF document in this corpus already extracts and reconciles through PyMuPDF text, and every scanned document is either read by vision or by Tesseract with quotes verified against that text — there is no evidence that an additional table/OCR dependency improves a result, and the brief is explicit about not adding unnecessary dependencies. The architecture keeps the boundary clean: a richer table extractor would slot in at `pdf_io` as another text source without touching the rest of the pipeline.

## Provider routing and quota engineering

Provider ladders (`PROVIDER_LADDER_VISION` / `PROVIDER_LADDER_TEXT`) order the four routes; `PAYABLE_AI_PROVIDER` forces one for debugging. The key pools and model ladders make free-tier realities survivable: per-key health, per-model daily-bucket rollover, per-minute pacing, request-class errors (too-large / empty-completion) routed to the next model instead of killing a key, upstream-rate-limited models parked briefly without poisoning the key, and fast failure — a cooling provider is skipped when an alternative exists instead of sleeping through a daily quota. HTTP read timeouts bound queue-stalled calls, and per-document extract/repair wall-clock budgets stop a slow route from stalling the batch. Diagnostics record attempts (`provider_attempts`) so routing decisions are auditable, and the corrective re-read records its outcome (`completeness_reextract`).

## Production gap analysis

Before handling real supplier payments: a curated labeled evaluation set with per-field metrics; versioned prompts/models with shadow evaluation; a queue-based, idempotent batch runner; token/request budgets per tenant; OCR-text caching keyed by document hash; encrypted storage, retention limits, and access controls; a provider data-processing arrangement (free tiers are not acceptable for confidential invoices); a human-review queue for every non-MATCH, every removed component, and every low-confidence visual read; and immutable audit events for every accepted change. Model output must never auto-approve a payment without business controls.
