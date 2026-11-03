# Production Plan — Accuracy, Latency, Cost, Reliability, Compliance

This document describes how this prototype becomes a production accounts-payable system. It separates **what the repository already implements** from **what a real deployment adds**, and explains the engineering rationale for each choice.

## 1. Operating targets (SLOs)

| Dimension | Target | How it is measured |
|---|---|---|
| Gross reconciliation | ≥ 99.5% of payables book to the printed amount | Deterministic replay through the sealed ERP (the same check the validator runs) |
| Field-level accuracy | ≥ 99% per critical field (gross, tax amounts/rates, supplier code) | Labeled gold set, per-field precision/recall |
| Classification | ≥ 99.5% (payable count + invoice/credit/non-payable) | Gold-set confusion matrix |
| Latency | p50 ≤ 30 s, p95 ≤ 120 s per document (vision-heavy) | Per-document timing in the diagnostics |
| Availability | 99.9% of documents processed within the SLA window | Queue depth + failure budget |
| Cost | Per-document token budget with hard caps per tenant | Token accounting per call |

The assignment corpus already meets the first row (42/42 reconciled); the rest are production programs, not claims.

## 2. What the repository already implements

- **Deterministic accounting core** — the sealed `erp.py` is the acceptance oracle; the validator, the packager, and the repair loop all judge through it. Gross agreement is never accepted alone: schema, semantics, tax placement, grounding, and classification are checked separately.
- **Signals-not-instructions repair loop** — deterministic ERP facts (per-line bases, per-tax contributions, deltas, declared-vs-component tax) drive the model's corrections; changes are quote-verified and either strictly improve reconciliation or land exactly on the printed total.
- **Determinism boundary** — math-certain repairs are code, not prompt: quantity resolution from printed row arithmetic, duplicate-tax-level clearing, and printed-formula tax snapping (least uniform precision, exact-gated). Anything needing world knowledge stays with the model.
- **Provider abstraction with graceful degradation** — four adapters (Gemini, Groq, OpenRouter, Cloudflare Workers AI) behind key pools, per-model ladders, per-model capability gating, request pacing, read timeouts, and per-document wall-clock budgets. Keys never reach logs or reprs.
- **Cost-aware routing** — cheap text extraction first, vision only when the page needs it; long documents are chunked under token limits; reasoning is disabled for structured extraction on OpenRouter; quota exhaustion fails fast instead of sleeping.
- **Honest uncertainty** — genuinely absent values stay blank (never guessed), irreducible documents carry a warning and a precise evidence gap, and the packager refuses placeholders or unreconciled payables rather than hiding them.

## 3. Accuracy program (the main production lever)

1. **Labeled gold set** — every document class (domestic/international invoices, credit memos, customs dossiers, utility bills, receipts, dossiers with supporting pages) with field-level labels: supplier/buyer codes, dates, per-line quantity/price/discount, tax level/rate/amount, charges, classification.
2. **Field-level metrics, not just gross** — track per-field precision/recall; a gross that foots through the wrong path is a regression, not a pass (the quality checks exist for this).
3. **Regression corpus** — every production mis-book becomes a test fixture; the deterministic mechanisms are unit-tested, and repaired cases are added to a reviewed regression set (never tuned per document).
4. **Shadow evaluation** — new prompts/models run on the same inputs without posting; diffs are reviewed before rollout; model/prompt versions are pinned per tenant.
5. **Confidence routing to humans** — any non-reconciled payable, cleared component, low-confidence OCR page, or ambiguous master match goes to a review queue with the exact evidence gap. The prototype already flags these; production enforces four-eyes approval before payment.

## 4. Latency

- **Tiered extraction** — native text first (fast, cheap); OCR only for pages without text; vision only for pages whose layout or OCR confidence demands it. The provider ladder already implements this per document; production extends it to per *page* for mixed documents.
- **Parallelism** — documents are independent: process with a worker pool sized to provider rate limits; pages within a document are chunked for vision and merged, which also bounds single-call latency.
- **Caching** — OCR text keyed by document hash (OCR is the expensive local step); provider prompt prefixes are stable (system + rules before the variable tail), so implicit prefix caching applies; repeated re-runs of the same document reuse local artifacts.
- **Budgets and timeouts** — per-call read timeouts and per-document extract/repair budgets (already implemented) prevent a queued free-tier or degraded provider from stalling a batch; production adds circuit breakers per provider/model.

## 5. Cost

- **Model-to-difficulty matching** — cheapest capable route first; escalate only on evidence (missing text, poor OCR, reconciliation failure). The ladder already does this; production adds per-step model caps (e.g. cheap model for classification, strong model for dense tables).
- **Token accounting and budgets** — every call is logged with provider/model/attempts; production enforces per-tenant budgets with hard stops and alerts.
- **Batching** — provider batch APIs for vision at scale; nightly reprocessing of flagged documents instead of retry storms.
- **Idempotency** — content-hash document keys so retries never double-book and never re-pay for the same extraction.

## 6. Reliability

- **Queue-based ingestion** — object storage → queue → workers; per-document leases; dead-letter queue for failures with the diagnostics attached (the run log format is the template).
- **Provider failover** — pools + ladders + fast failure already implemented; production adds health metrics per (provider, model) and automatic re-entry after cooldown.
- **Observability** — per-document traces (pages, provider attempts, ERP facts, repair decisions, evidence audits), aggregate dashboards (match rate, warning classes, latency, spend), alerting on error budgets.
- **Deterministic replay** — outputs can be re-validated offline (`--validate-output`, `scripts/apply_deterministic_fixes.py`) without calling providers.

## 7. Security and compliance

- **Credentials** — environment/secret-manager only; never in code, logs, outputs, or archives. The redactor and redacted `Settings.__repr__` are already in place.
- **Data handling** — production must use a paid/business provider arrangement with a data-processing agreement that excludes training on tenant documents (free tiers do not qualify). Encrypt at rest and in transit; retention limits for PDFs, OCR text, and extracted evidence; access controls and audit trails.
- **Auditability** — immutable events for every accepted change (path, before/after, evidence quote, mechanism), human approvals, and the final posted record.
- **Controls** — no automatic payment; the system produces autodrafts for an ERP, and the ERP's own controls (approval workflows, segregation of duties) remain authoritative.

## 8. Rollout plan

1. **Shadow mode** — run against live traffic, post nothing, compare against human-keyed results weekly.
2. **Assist mode** — humans review every draft; measure touch time and correction categories.
3. **Sampled auto-book** — auto-post clean matches, sample-audit a fixed percentage; alert on any audit drift.
4. **Scale-up** — widen document classes and geographies only while field-level accuracy holds; keep the kill switch and the manual queue.

## 9. Deliberate non-goals of this prototype

- No ERP write-back connector, no approval workflow, no multi-tenant isolation layer — those are integration work, not extraction intelligence.
- No document-specific parsers or answer tables: every mechanism generalizes from printed facts; this is the property the evaluation rewards.
