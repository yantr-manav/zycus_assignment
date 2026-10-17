from __future__ import annotations
import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings
from .keys import KeyPool, Pacer, key_label, redact
from .pdf_io import PDFDocument, split_document_text, trim_document_text, ensure_full_ocr
from .prompting import SYSTEM_PROMPT, EXTRACTION_REFERENCE, extraction_prompt, repair_prompt

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"

# Workers AI vision models that accept image parts on the OpenAI-compatible route.
# Everything else is treated as text-only: images are never sent to a model we
# cannot confirm accepts them (a text-only model would either error or ignore
# the image silently, and silently ignoring is the worse failure mode).
_CF_VISION_MODELS = {
    "@cf/meta/llama-3.2-11b-vision-instruct",
    "@cf/meta/llama-4-scout-17b-16e-instruct",
    "@cf/google/gemma-4-26b-a4b-it",
    "@cf/google/gemma-4-31b-it",
}


class ProviderHTTPError(Exception):
    """HTTP failure from an OpenAI-compatible provider, carrying its status."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"{status_code}: {message}")
        self.status_code = status_code
        self.message = message


def _norm_invoice_number(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def merge_extraction_parts(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-part extractions of one long document into a single result.

    Payables are concatenated; when the same invoice number appears in more than
    one part (an invoice spanning a part boundary), the more detailed record
    wins. Non-payable parts stay declines only when no part found a payable.
    """
    payables: list[dict[str, Any]] = []
    by_number: dict[str, int] = {}
    is_payable = False
    doc_types: list[str] = []
    reasons: list[str] = []
    classification: list[str] = []
    declined_reason = ""
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("is_payable"):
            is_payable = True
        if str(part.get("document_type", "")).strip():
            doc_types.append(str(part["document_type"]).strip())
        if str(part.get("classification_reason", "")).strip():
            classification.append(str(part["classification_reason"]).strip())
        if str(part.get("declined_reason", "")).strip():
            declined_reason = declined_reason or str(part["declined_reason"]).strip()
        for payable in part.get("payables", []) or []:
            if not isinstance(payable, dict):
                continue
            number = _norm_invoice_number(payable.get("invoice_number"))
            if number and number in by_number:
                existing = payables[by_number[number]]
                if len(payable.get("line_items") or []) > len(existing.get("line_items") or []):
                    payables[by_number[number]] = payable
                continue
            if number:
                by_number[number] = len(payables)
            payables.append(payable)
    merged_type = doc_types[0] if doc_types else "UNKNOWN"
    if payables and merged_type in {"OTHER", "UNKNOWN"}:
        # A part that carried the actual invoice should name the type.
        for part in parts:
            if isinstance(part, dict) and (part.get("payables") or []):
                candidate = str(part.get("document_type", "")).strip()
                if candidate and candidate.upper() not in {"OTHER", "UNKNOWN"}:
                    merged_type = candidate
                    break
    return {
        "document_type": merged_type,
        "is_payable": is_payable or bool(payables),
        "classification_reason": " | ".join(dict.fromkeys(classification))[:600],
        "declined_reason": declined_reason if not payables else "",
        "payables": payables,
    }


@dataclass
class ConversationState:
    """One flowing per-document context, reused across extraction and repair turns.

    Keeping the turn-by-turn state (Gemini: contents incl. page images; Groq:
    messages) lets the repair turn continue the same conversation: the model
    still sees the pages and its own draft, no re-upload, and the stable prompt
    prefix stays eligible for provider-side prefix caching.
    """
    provider: str
    model: str
    transcript: list[Any] = field(default_factory=list)
    last_model_text: str = ""
    used_vision: bool = False


@dataclass
class LLMResult:
    data: dict[str, Any]
    provider: str
    model: str
    attempts: list[str]
    conversation: ConversationState | None = None


def _extract_json(text: str) -> dict[str, Any]:
    if not text or not text.strip():
        raise ValueError("LLM returned an empty response")
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("LLM response root must be a JSON object")
    return obj


def _error_status(exc: Exception) -> int | None:
    for attr in ("status", "status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _retry_after_seconds(exc: Exception) -> float | None:
    """Extract the provider's own retry delay where it states one.

    Google embeds ``retryDelay`` ("62959s") or "Please retry in 17h29m19.8s";
    Groq/OpenAI-style errors carry a ``retry-after`` header and often
    "try again in 37.5s".
    """
    text = str(exc)
    m = re.search(r"retryDelay[\"']?\s*:\s*[\"']?(\d+(?:\.\d+)?)s", text)
    if m:
        try:
            return float(m.group(1)) + 2.0
        except ValueError:
            pass
    m = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s", text)
    if m:
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        seconds = float(m.group(3))
        return hours * 3600 + minutes * 60 + seconds + 2.0
    m = re.search(r"try again in (\d+(?:\.\d+)?)s", text)
    if m:
        try:
            return float(m.group(1)) + 2.0
        except ValueError:
            pass
    response = getattr(exc, "response", None)
    header = None
    if response is not None:
        try:
            header = response.headers.get("retry-after")
        except Exception:
            header = None
    if header:
        try:
            return float(header) + 2.0
        except ValueError:
            pass
    return None


def classify_provider_error(exc: Exception) -> tuple[str, float | None]:
    """Map a provider exception onto KeyPool failure classes.

    Strings are matched on the *redacted* text so credentials can never be
    classified into a log line. Classification is deliberately conservative:
    a key is only marked invalid on an explicit auth signal — a generic 400
    (e.g. "request too large") must not kill a healthy key.
    """
    message = redact(str(exc)).lower()
    status = _error_status(exc)
    retry_after = _retry_after_seconds(exc)
    if status in (401, 403) or "api key not valid" in message or "api_key_invalid" in message:
        return KeyPool.INVALID, retry_after
    if status == 400 and ("invalid api key" in message or "invalid authentication" in message or "unauthenticated" in message):
        return KeyPool.INVALID, retry_after
    if status == 413 or "request too large" in message or "request is too large" in message or "max_completion_tokens" in message:
        return KeyPool.REQUEST, retry_after
    if status == 422 or "empty completion" in message:
        # Reasoning-style models can consume the whole budget before emitting any
        # JSON; the key is healthy, the request simply does not fit this model.
        return KeyPool.REQUEST, retry_after
    if status == 429 or "rate limit" in message or "rate_limit" in message or "quota" in message or "resource exhausted" in message or "resource_exhausted" in message:
        return KeyPool.QUOTA, retry_after
    return KeyPool.TRANSIENT, retry_after


def _is_daily_exhaustion(message: str, retry_after: float | None) -> bool:
    if retry_after is not None and retry_after > 1800:
        return True
    lowered = message.lower()
    return any(token in lowered for token in ("per day", "per-day", "perday", "daily", "free-models-per"))


class LLMRouter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.gemini = KeyPool("gemini", list(settings.gemini_api_keys))
        self.groq = KeyPool("groq", list(settings.groq_api_keys))
        self.openrouter = KeyPool("openrouter", list(settings.openrouter_api_keys))
        cloudflare_token = settings.cloudflare_api_token if settings.cloudflare_account_id else ""
        self.cloudflare = KeyPool("cloudflare", [cloudflare_token] if cloudflare_token else [])
        self._pacer = Pacer(settings.min_call_interval_seconds)
        self._gemini_clients: dict[str, Any] = {}
        self._groq_clients: dict[str, Any] = {}
        self._http = httpx.Client(timeout=httpx.Timeout(180.0, connect=20.0, read=150.0))
        self._openrouter_vision_support: dict[str, bool] = {}
        # (provider, model) pairs with no remaining quota today or unavailable.
        self._dead_models: set[tuple[str, str]] = set()
        # (provider, model) -> monotonic time until which the model is skipped
        # (e.g. an OpenRouter upstream model that is temporarily saturated).
        self._model_cooldown: dict[tuple[str, str], float] = {}
        # Backwards-compatible handles for tools/tests that introspect the router.
        self._gemini = bool(self.gemini)
        self._groq = bool(self.groq)

    # ------------------------------------------------------------------ setup

    def _gemini_client(self, key: str):
        if key not in self._gemini_clients:
            from google import genai
            from google.genai import types
            self._gemini_clients[key] = genai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=300_000),
            )
        return self._gemini_clients[key]

    def _groq_client(self, key: str):
        if key not in self._groq_clients:
            from groq import Groq
            self._groq_clients[key] = Groq(api_key=key, max_retries=0, timeout=240.0)
        return self._groq_clients[key]

    def _pool(self, provider: str) -> KeyPool:
        return {
            "gemini": self.gemini,
            "groq": self.groq,
            "openrouter": self.openrouter,
            "cloudflare": self.cloudflare,
        }[provider]

    def _candidates(self, provider: str) -> list[str]:
        catalog = {
            "gemini": [self.settings.gemini_model, *self.settings.gemini_fallback_models],
            "groq": [self.settings.groq_model, *self.settings.groq_fallback_models],
            "openrouter": [self.settings.openrouter_model, *self.settings.openrouter_fallback_models],
            "cloudflare": [self.settings.cloudflare_model, *self.settings.cloudflare_fallback_models],
        }
        seen: set[str] = set()
        out: list[str] = []
        now = time.monotonic()
        for model in catalog.get(provider, []):
            if not model or model in seen or (provider, model) in self._dead_models:
                continue
            if self._model_cooldown.get((provider, model), 0.0) > now:
                continue
            seen.add(model)
            out.append(model)
        return out

    def _cool_model(self, provider: str, model: str, seconds: float = 300.0) -> None:
        self._model_cooldown[(provider, model)] = time.monotonic() + seconds

    def _provider_ready(self, provider: str) -> bool:
        pool = self._pool(provider)
        return bool(pool) and pool.next_key(wait=False) is not None

    def available(self) -> list[str]:
        out = []
        for provider in ("gemini", "groq", "openrouter", "cloudflare"):
            if self._pool(provider):
                out.append(provider)
        return out

    def ensure_available(self) -> None:
        configured = self.settings.provider
        if configured != "auto" and not self._pool(configured):
            env_hint = {
                "gemini": "GEMINI_API_KEY",
                "groq": "GROQ_API_KEY",
                "openrouter": "OPENROUTER_API_KEY",
                "cloudflare": "CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID",
            }[configured]
            raise RuntimeError(f"{configured} selected but not configured ({env_hint}). Check .env")
        if not self.available():
            raise RuntimeError(
                "No LLM provider configured. Add GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY "
                "and/or CLOUDFLARE_ACCOUNT_ID+CLOUDFLARE_API_TOKEN to .env"
            )

    def _order(self, doc: PDFDocument) -> list[str]:
        available = self.available()
        configured = self.settings.provider
        if configured != "auto":
            preferred = [configured]
        else:
            preferred = list(self.settings.ladder_vision if doc.needs_vision else self.settings.ladder_text)
        merged = list(dict.fromkeys(preferred + list(self.settings.ladder_vision) + list(self.settings.ladder_text)))
        return [p for p in merged if p in available]

    def key_status(self) -> dict[str, Any]:
        """Per-key health for diagnostics; key values are never exposed."""
        return {
            "gemini": self.gemini.status(),
            "groq": self.groq.status(),
            "openrouter": self.openrouter.status(),
            "cloudflare": self.cloudflare.status(),
            "dead_models": sorted(f"{provider}:{model}" for provider, model in self._dead_models),
        }

    # ------------------------------------------------------- vision capability

    def _openrouter_model_catalog(self) -> dict[str, dict[str, Any]]:
        """Fetch OpenRouter's model catalog once to learn per-model capabilities."""
        cached = getattr(self, "_openrouter_catalog_cache", None)
        if cached is not None:
            return cached
        catalog: dict[str, dict[str, Any]] = {}
        try:
            response = self._http.get(OPENROUTER_MODELS_URL, timeout=30)
            if response.status_code == 200:
                for row in response.json().get("data", []):
                    catalog[row.get("id", "")] = row
        except Exception:
            catalog = {}
        self._openrouter_catalog_cache = catalog
        return catalog

    def _supports_vision(self, provider: str, model: str) -> bool:
        if provider == "gemini":
            return True  # gemini flash tiers are multimodal
        if provider == "groq":
            return False  # no vision models on this account's catalog
        if provider == "cloudflare":
            return model in _CF_VISION_MODELS
        if provider == "openrouter":
            if model in self._openrouter_vision_support:
                return self._openrouter_vision_support[model]
            catalog = self._openrouter_model_catalog()
            row = catalog.get(model)
            if row is not None:
                modalities = (row.get("architecture") or {}).get("input_modalities", [])
                supported = "image" in modalities
            else:
                # Catalog unavailable: fall back to a conservative name heuristic.
                lowered = model.casefold()
                supported = any(token in lowered for token in ("vision", "-vl", "gemma-4", "scout", "dots", "omni"))
            self._openrouter_vision_support[model] = supported
            return supported
        return False

    def probe_keys(self) -> dict[str, Any]:
        """Check each configured key against its provider with a metadata call.

        One request per key, no tokens. Reports whether the primary model (or a
        configured fallback) is usable for that key.
        """
        report: dict[str, Any] = {"gemini": [], "groq": [], "openrouter": [], "cloudflare": []}
        if self.gemini:
            from google.genai import errors as genai_errors
            for key in self.gemini.keys():
                row = {"key": key_label(key), "state": "ready", "model": None}
                try:
                    self._gemini_client(key).models.get(model=self.settings.gemini_model)
                    row["model"] = self.settings.gemini_model
                    self.gemini.note_success(key)
                except genai_errors.ClientError as exc:
                    kind, _ = classify_provider_error(exc)
                    if kind == KeyPool.INVALID:
                        row["state"] = "invalid"
                        self.gemini.note_failure(key, KeyPool.INVALID)
                    elif _error_status(exc) == 404:
                        fallback = self._probe_fallback_models(key, row)
                        if fallback is None:
                            row["state"] = "model-unavailable"
                    else:
                        row["state"] = f"error:{_error_status(exc)}"
                except Exception as exc:
                    row["state"] = f"unreachable:{type(exc).__name__}"
                report["gemini"].append(row)
        if self.groq:
            for key in self.groq.keys():
                row = {"key": key_label(key), "state": "ready", "model": None}
                try:
                    available_ids = [m.id for m in self._groq_client(key).models.list().data]
                    if self.settings.groq_model in available_ids:
                        row["model"] = self.settings.groq_model
                    else:
                        row["state"] = "model-unavailable"
                    self.groq.note_success(key)
                except Exception as exc:
                    kind, _ = classify_provider_error(exc)
                    if kind == KeyPool.INVALID:
                        row["state"] = "invalid"
                        self.groq.note_failure(key, KeyPool.INVALID)
                    else:
                        row["state"] = f"error:{_error_status(exc) or type(exc).__name__}"
                report["groq"].append(row)
        if self.openrouter:
            for key in self.openrouter.keys():
                row = {"key": key_label(key), "state": "ready", "model": None, "vision": None}
                try:
                    response = self._http.get(OPENROUTER_KEY_URL, headers=self._openrouter_headers(key), timeout=30)
                    if response.status_code in (401, 403):
                        row["state"] = "invalid"
                        self.openrouter.note_failure(key, KeyPool.INVALID)
                    elif response.status_code == 200:
                        row["model"] = self.settings.openrouter_model
                        row["vision"] = self._supports_vision("openrouter", self.settings.openrouter_model)
                        self.openrouter.note_success(key)
                    else:
                        row["state"] = f"error:{response.status_code}"
                except Exception as exc:
                    row["state"] = f"unreachable:{type(exc).__name__}"
                report["openrouter"].append(row)
        if self.cloudflare:
            for key in self.cloudflare.keys():
                row = {"key": key_label(key), "state": "ready", "model": None, "vision": None}
                try:
                    response = self._http.get(
                        f"https://api.cloudflare.com/client/v4/accounts/{self.settings.cloudflare_account_id}/ai/models/search",
                        headers={"Authorization": f"Bearer {key}"},
                        params={"per_page": 100, "task": "Text Generation"},
                        timeout=30,
                    )
                    if response.status_code in (401, 403):
                        row["state"] = "invalid"
                        self.cloudflare.note_failure(key, KeyPool.INVALID)
                    elif response.status_code == 200:
                        names = [m.get("name", "") for m in (response.json().get("result") or [])]
                        if self.settings.cloudflare_model in names:
                            row["model"] = self.settings.cloudflare_model
                            row["vision"] = self._supports_vision("cloudflare", self.settings.cloudflare_model)
                        else:
                            row["state"] = "model-unavailable"
                        self.cloudflare.note_success(key)
                    else:
                        row["state"] = f"error:{response.status_code}"
                except Exception as exc:
                    row["state"] = f"unreachable:{type(exc).__name__}"
                report["cloudflare"].append(row)
        return report

    def _probe_fallback_models(self, key: str, row: dict[str, Any]) -> str | None:
        from google.genai import errors as genai_errors
        for candidate in self.settings.gemini_fallback_models:
            try:
                self._gemini_client(key).models.get(model=candidate)
                row["model"] = candidate
                return candidate
            except genai_errors.ClientError:
                continue
            except Exception:
                continue
        return None

    # -------------------------------------------------------------- extraction

    def extract(self, doc: PDFDocument, filename: str, review_signal: str = "") -> LLMResult:
        self.ensure_available()
        attempts: list[str] = []
        errors: list[str] = []
        order = self._order(doc)
        deadline = time.monotonic() + self.settings.extract_budget_seconds
        for index, provider in enumerate(order):
            # If this provider is cooling down and a later one exists, skip it
            # fast instead of sleeping through the cooldown mid-batch.
            if not self._provider_ready(provider) and index < len(order) - 1:
                attempts.append(f"{provider}:cooling-down-skipped")
                continue
            if index > 0 and time.monotonic() > deadline:
                attempts.append(f"{provider}:skipped (extract time budget spent)")
                continue
            try:
                if provider == "groq":
                    self._prepare_text_route(doc)
                    parts = split_document_text(doc.text, self.settings.groq_max_document_chars)
                    if len(parts) > 1:
                        # Long dossier on a token-limited text route: process part by
                        # part and merge; a single oversized request would be rejected.
                        part_results = []
                        model = self.settings.groq_model
                        state = None
                        for index, part_text in enumerate(parts, start=1):
                            prompt = (
                                extraction_prompt(part_text, filename, review_signal=review_signal)
                                + f"\n\nNOTE: this is PART {index} of {len(parts)} of the document; extract only facts visible "
                                  "in the pages shown here. A payable may continue in another part — return what these pages evidence."
                            )
                            part_data, model, _ = self._call(provider, prompt, doc=doc, include_images=False)
                            self._check_extraction(part_data)
                            part_results.append(part_data)
                        data = merge_extraction_parts(part_results)
                        attempts.append(f"{provider}:success({len(parts)} parts)")
                        return LLMResult(data, provider, model, attempts, conversation=None)
                if provider in {"openrouter", "cloudflare"} and doc.needs_vision:
                    # Vision-capable models receive images; text-only candidates in
                    # this provider's ladder need OCR text, so complete it locally
                    # before the call without failing the route when OCR is missing.
                    try:
                        ensure_full_ocr(doc)
                    except Exception as exc:
                        doc.warnings.append(f"on-demand OCR before {provider} failed: {type(exc).__name__}: {exc}")
                limit = self.settings.groq_max_document_chars if provider == "groq" else self.settings.max_document_chars
                text = trim_document_text(doc.text, limit)
                prompt = extraction_prompt(text, filename, review_signal=review_signal)
                data, model, state = self._call(
                    provider, prompt, doc=doc,
                    include_images=(doc.needs_vision and provider in {"gemini", "openrouter", "cloudflare"}),
                )
                self._check_extraction(data)
                attempts.append(f"{provider}:success")
                return LLMResult(data, provider, model, attempts, conversation=state)
            except Exception as exc:
                msg = redact(f"{provider}:{type(exc).__name__}:{str(exc)[:240]}")
                attempts.append(msg)
                errors.append(msg)
        raise RuntimeError("All configured LLM providers failed. " + " | ".join(errors))

    def _prepare_text_route(self, doc: PDFDocument) -> None:
        """Before a text-only call, complete OCR; fail closed if too much is missing."""
        any_image = any((not page.text.strip() or page.source == "image_only" or "ocr_skipped" in page.source) and page.image_jpeg for page in doc.pages)
        if any_image:
            ensure_full_ocr(doc)
        unresolved_pages = [
            page.page_number for page in doc.pages
            if (not page.text.strip() or page.source == "image_only" or "ocr_skipped" in page.source)
        ]
        max_unresolved = max(1, int(doc.page_count * 0.20))
        if len(unresolved_pages) > max_unresolved:
            raise RuntimeError(
                "Text-only fallback aborted because OCR remains incomplete on "
                f"{len(unresolved_pages)}/{doc.page_count} pages (pages {unresolved_pages[:12]}). "
                "Install/fix Tesseract or enable a Gemini vision route; do not extract from a partial transcript."
            )

    # ------------------------------------------------------------------ repair

    def repair(
        self,
        doc: PDFDocument,
        filename: str,
        payable: dict[str, Any],
        booked: float,
        printed_gross: str,
        preferred_provider: str,
        signals: list[dict[str, Any]] | None = None,
        state: ConversationState | None = None,
    ) -> LLMResult:
        # Grounding verification happens outside the router against doc.text; make
        # sure OCR-backed text exists wherever possible before any repair turn.
        if any((not page.text.strip() or page.source == "image_only" or "ocr_skipped" in page.source) and page.image_jpeg for page in doc.pages):
            ensure_full_ocr(doc)
        unresolved_pages = [
            page.page_number for page in doc.pages
            if (not page.text.strip() or page.source == "image_only" or "ocr_skipped" in page.source)
        ]
        max_unresolved = max(1, int(doc.page_count * 0.20))
        if preferred_provider == "groq" and len(unresolved_pages) > max_unresolved:
            raise RuntimeError(
                "Repair aborted because the text source remains incomplete; "
                f"OCR unresolved on {len(unresolved_pages)}/{doc.page_count} pages. "
                "A partial transcript is not reliable evidence for changing accounting fields."
            )

        # Warm conversation turns (which keep page images in context) are efficient
        # on Gemini's SDK; on the stateless OpenAI-compatible routes every turn
        # re-uploads the payload, so repairs there run text-only over the OCR/native
        # transcript — the same text the grounding gate verifies quotes against.
        warm = (
            state is not None
            and state.provider == preferred_provider
            and bool(state.transcript)
            and preferred_provider in {"gemini", "groq"}
        )
        merged = list(dict.fromkeys(
            [preferred_provider] + list(self.settings.ladder_vision) + list(self.settings.ladder_text)
        ))
        order = [p for p in merged if p in self.available()]
        errors: list[str] = []
        deadline = time.monotonic() + self.settings.repair_budget_seconds
        for index, provider in enumerate(order):
            if not self._provider_ready(provider) and index < len(order) - 1:
                errors.append(f"{provider}:cooling-down-skipped")
                continue
            if index > 0 and time.monotonic() > deadline:
                errors.append(f"{provider}:skipped (repair time budget spent)")
                continue
            try:
                if warm and provider == preferred_provider and state is not None:
                    prompt = repair_prompt(filename, payable, booked, printed_gross, signals=signals, document_text=None)
                    data, model, new_state = self._call_warm(provider, prompt, state)
                else:
                    limit = self.settings.groq_max_document_chars if provider == "groq" else min(self.settings.max_document_chars, 70000)
                    text = trim_document_text(doc.text, limit)
                    prompt = repair_prompt(filename, payable, booked, printed_gross, signals=signals, document_text=text)
                    data, model, new_state = self._call(
                        provider, prompt, doc=doc,
                        include_images=(doc.needs_vision and provider == "gemini"),
                    )
                if not isinstance(data.get("changes", []), list):
                    raise ValueError("Repair response 'changes' must be an array")
                return LLMResult(data, provider, model, [f"{provider}:repair_success"], conversation=new_state)
            except Exception as exc:
                errors.append(redact(f"{provider}:{type(exc).__name__}:{str(exc)[:240]}"))
            warm = False  # provider switched — a fresh context is required
        raise RuntimeError("Repair providers failed: " + " | ".join(errors))

    # -------------------------------------------------------------- provider IO

    def _call(self, provider: str, prompt: str, doc: PDFDocument, include_images: bool) -> tuple[dict[str, Any], str, ConversationState]:
        if provider == "gemini":
            return self._call_gemini(prompt, doc, include_images)
        if provider == "groq":
            return self._call_groq(prompt)
        if provider == "openrouter":
            return self._call_openrouter(prompt, doc, include_images)
        if provider == "cloudflare":
            return self._call_cloudflare(prompt, doc, include_images)
        raise ValueError(f"Unknown provider: {provider}")

    def _call_warm(self, provider: str, prompt: str, state: ConversationState) -> tuple[dict[str, Any], str, ConversationState]:
        if provider == "gemini":
            return self._call_gemini(prompt, None, False, state=state)
        if provider == "groq":
            return self._call_groq(prompt, state=state)
        if provider == "openrouter":
            return self._call_openrouter(prompt, None, False, state=state)
        if provider == "cloudflare":
            return self._call_cloudflare(prompt, None, False, state=state)
        raise ValueError(f"Unknown provider: {provider}")

    def _select_vision_pages(self, doc: PDFDocument) -> list[Any]:
        images = [p for p in doc.pages if p.image_jpeg]
        cap = self.settings.max_vision_pages
        if len(images) > cap:
            # Keep first and last pages (identity/totals commonly live there), then prioritize
            # weak-OCR pages, table-like pages, and finally evenly spaced pages.
            chosen: dict[int, Any] = {}
            chosen[images[0].page_number] = images[0]
            chosen[images[-1].page_number] = images[-1]
            table_pages = sorted(
                [p for p in images if PDFDocument._looks_like_table(p.text)],
                key=lambda p: (-PDFDocument.table_score(p.text), p.page_number),
            )
            weak = sorted(
                [p for p in images if p.ocr_confidence is None or p.ocr_confidence < 0.82],
                key=lambda p: (p.ocr_confidence if p.ocr_confidence is not None else -1.0, p.page_number),
            )
            for pool in (table_pages, weak):
                for page in pool:
                    if len(chosen) >= cap:
                        break
                    chosen[page.page_number] = page
            if len(chosen) < cap:
                slots = cap - len(chosen)
                targets = [round(j * (len(images) - 1) / max(1, slots - 1)) for j in range(slots)] if slots > 1 else [len(images) // 2]
                for target in targets:
                    page = images[target]
                    chosen[page.page_number] = page
                    if len(chosen) >= cap:
                        break
            images = [chosen[k] for k in sorted(chosen)][:cap]
        return images

    def _gemini_image_parts(self, doc: PDFDocument) -> list[Any]:
        from google.genai import types
        parts: list[Any] = []
        for page in self._select_vision_pages(doc):
            parts.append(types.Part.from_text(text=f"[RENDERED SOURCE PAGE {page.page_number}]"))
            parts.append(types.Part.from_bytes(data=page.image_jpeg, mime_type="image/jpeg"))
        return parts

    def _openai_image_parts(self, doc: PDFDocument) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for page in self._select_vision_pages(doc):
            parts.append({"type": "text", "text": f"[RENDERED SOURCE PAGE {page.page_number}]"})
            encoded = base64.b64encode(page.image_jpeg or b"").decode("ascii")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            })
        return parts

    def _handle_common_error(
        self,
        provider: str,
        candidate: str,
        exc: Exception,
        key: str | None,
        errors: list[str],
    ) -> str:
        """Returns a control code: 'next-candidate' | 'next-key' | 'raise-request'."""
        kind, retry_after = classify_provider_error(exc)
        pool = self._pool(provider)
        label = f"{provider}[{key_label(key) if key else '-'}]"
        status = _error_status(exc)
        if kind == KeyPool.INVALID:
            if key:
                pool.note_failure(key, KeyPool.INVALID)
            errors.append(redact(f"{label}:invalid key"))
            return "next-key"
        if kind == KeyPool.REQUEST:
            # The request is too large for this model's limits; another model may
            # accept it, but retrying the same key adds nothing.
            errors.append(redact(f"{label}:request-rejected:{str(exc)[:160]}"))
            return "next-candidate"
        if kind == KeyPool.QUOTA:
            if _is_daily_exhaustion(str(exc), retry_after):
                self._dead_models.add((provider, candidate))
                errors.append(redact(f"{label}:daily quota exhausted on {candidate}"))
                return "next-candidate"
            if provider == "openrouter" and "rate-limited upstream" in str(exc).casefold():
                # The key is healthy; this upstream model is saturated. Park the
                # model briefly and try the next candidate without burning key state.
                self._cool_model(provider, candidate, 300.0)
                errors.append(redact(f"{label}:upstream rate-limit on {candidate}"))
                return "next-candidate"
            if key:
                pool.note_failure(key, KeyPool.QUOTA, retry_after)
            errors.append(redact(f"{label}:quota {status} (retry in {retry_after or '?'}s)"))
            return "next-key"
        if status == 404:
            # Model not available for this key; try the next candidate model.
            errors.append(redact(f"{label}:model {candidate} unavailable"))
            return "next-candidate"
        if key:
            pool.note_failure(key, KeyPool.TRANSIENT, retry_after)
        errors.append(redact(f"{label}:{status or type(exc).__name__}:{str(exc)[:160]}"))
        return "next-key"

    def _call_gemini(
        self,
        prompt: str,
        doc: PDFDocument | None,
        include_images: bool,
        state: ConversationState | None = None,
    ) -> tuple[dict[str, Any], str, ConversationState]:
        from google.genai import types
        from google.genai import errors as genai_errors

        if state is not None:
            state = ConversationState(
                provider=state.provider,
                model=state.model,
                transcript=list(state.transcript),
                last_model_text=state.last_model_text,
                used_vision=state.used_vision,
            )
            state.transcript.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))
        else:
            parts = [types.Part.from_text(text=EXTRACTION_REFERENCE + "\n\n" + prompt)]
            if include_images and doc is not None:
                parts.extend(self._gemini_image_parts(doc))
            state = ConversationState(
                provider="gemini",
                model=self.settings.gemini_model,
                transcript=[types.Content(role="user", parts=parts)],
                used_vision=include_images,
            )

        errors: list[str] = []
        for _ in range(max(2, self.gemini.size * 2)):
            candidates = self._candidates("gemini")
            if not candidates:
                raise RuntimeError("no Gemini model with remaining quota: " + " | ".join(errors))
            key = self.gemini.next_key()
            if key is None:
                raise RuntimeError("no Gemini key currently available: " + " | ".join(errors))
            for candidate in candidates:
                try:
                    self._pacer.wait()
                    response = self._gemini_client(key).models.generate_content(
                        model=candidate,
                        contents=state.transcript,
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_PROMPT,
                            response_mime_type="application/json",
                            temperature=0.1,
                            max_output_tokens=self.settings.max_output_tokens,
                        ),
                    )
                    text = response.text or ""
                    data = _extract_json(text)
                    self.gemini.note_success(key)
                    state.model = candidate
                    state.last_model_text = text
                    state.transcript.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))
                    return data, candidate, state
                except genai_errors.ClientError as exc:
                    action = self._handle_common_error("gemini", candidate, exc, key, errors)
                    if action == "next-candidate":
                        continue
                    break  # next-key (or raise already propagated)
                except genai_errors.ServerError as exc:
                    # 5xx is usually model-specific demand; try the next model first.
                    errors.append(redact(f"gemini[{key_label(key)}]:{_error_status(exc)} on {candidate}:{str(exc)[:120]}"))
                    continue
                except (json.JSONDecodeError, ValueError) as exc:
                    errors.append(redact(f"gemini[{key_label(key)}]:response-invalid:{str(exc)[:160]}"))
                    self.gemini.note_failure(key, KeyPool.TRANSIENT, 5.0)
                    break  # next key
                except Exception as exc:
                    kind, retry_after = classify_provider_error(exc)
                    self.gemini.note_failure(key, kind, retry_after)
                    errors.append(redact(f"gemini[{key_label(key)}]:{type(exc).__name__}:{str(exc)[:160]}"))
                    break  # next key
        raise RuntimeError("Gemini attempts exhausted: " + " | ".join(errors))

    def _groq_max_tokens(self, model: str) -> int:
        """Per-model completion caps (qwen tops out at 16384 on Groq)."""
        cap = 16384 if model.casefold().startswith("qwen") else 65536
        return max(1024, min(self.settings.max_output_tokens, cap))

    def _call_groq(self, prompt: str, state: ConversationState | None = None) -> tuple[dict[str, Any], str, ConversationState]:
        if state is not None:
            state = ConversationState(
                provider=state.provider,
                model=state.model,
                transcript=list(state.transcript),
                last_model_text=state.last_model_text,
                used_vision=False,
            )
            state.transcript.append({"role": "user", "content": prompt})
        else:
            state = ConversationState(
                provider="groq",
                model=self.settings.groq_model,
                transcript=[
                    {"role": "system", "content": SYSTEM_PROMPT + " Return one valid JSON object only."},
                    {"role": "user", "content": EXTRACTION_REFERENCE + "\n\n" + prompt},
                ],
            )

        errors: list[str] = []
        for _ in range(max(2, self.groq.size * 2)):
            candidates = self._candidates("groq")
            if not candidates:
                raise RuntimeError("no Groq model with remaining quota: " + " | ".join(errors))
            key = self.groq.next_key()
            if key is None:
                raise RuntimeError("no Groq key currently available: " + " | ".join(errors))
            for candidate in candidates:
                try:
                    self._pacer.wait()
                    response = self._groq_client(key).chat.completions.create(
                        model=candidate,
                        messages=state.transcript,
                        response_format={"type": "json_object"},
                        temperature=0.1,
                        max_completion_tokens=self._groq_max_tokens(candidate),
                    )
                    content = response.choices[0].message.content or ""
                    data = _extract_json(content)
                    self.groq.note_success(key)
                    state.model = candidate
                    state.last_model_text = content
                    state.transcript.append({"role": "assistant", "content": content})
                    return data, candidate, state
                except Exception as exc:
                    action = self._handle_common_error("groq", candidate, exc, key, errors)
                    if action == "next-candidate":
                        continue
                    break  # next key (or raise already propagated)
        raise RuntimeError("Groq attempts exhausted: " + " | ".join(errors))

    # ------------------------------------------------- OpenAI-compatible routes

    def _openrouter_headers(self, key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/zycus-bookable-payable",
            "X-Title": "Bookable Payable AI",
        }

    def _cloudflare_headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _openai_compat_post(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        use_json_mode: bool,
    ) -> str:
        """POST an OpenAI-compatible chat request; return the raw content string.

        Models differ in JSON-mode support, so a rejected ``response_format`` is
        retried once without it (the prompt already demands JSON and the parser
        tolerates fences); any other HTTP failure surfaces as ``ProviderHTTPError``.
        """
        payload = dict(body)
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        response = self._http.post(url, headers=headers, json=payload)
        if response.status_code >= 400 and use_json_mode:
            text = response.text[:600]
            if "response_format" in text or ("json" in text.casefold() and response.status_code == 400):
                payload.pop("response_format", None)
                response = self._http.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            raise ProviderHTTPError(response.status_code, response.text[:400])
        try:
            payload_json = response.json()
        except Exception:
            raise ProviderHTTPError(response.status_code, "response was not JSON")
        choices = payload_json.get("choices") or []
        content = ""
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            if isinstance(content, list):  # some gateways return content parts
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not content and choices:
            content = choices[0].get("text") or ""
        if not content:
            finish = choices[0].get("finish_reason", "") if choices else ""
            raise ProviderHTTPError(422, f"empty completion (finish_reason={finish!r})")
        return content

    def _compat_user_content(self, text: str, doc: PDFDocument | None, include_images: bool, model: str, provider: str) -> Any:
        if include_images and doc is not None and self._supports_vision(provider, model):
            return [{"type": "text", "text": text}] + self._openai_image_parts(doc)
        return text

    def _call_openrouter(
        self,
        prompt: str,
        doc: PDFDocument | None,
        include_images: bool,
        state: ConversationState | None = None,
    ) -> tuple[dict[str, Any], str, ConversationState]:
        return self._call_openai_compatible(
            provider="openrouter",
            prompt=prompt,
            doc=doc,
            include_images=include_images,
            state=state,
            url=OPENROUTER_CHAT_URL,
            headers_fn=self._openrouter_headers,
            max_tokens=min(self.settings.max_output_tokens, 32768),
        )

    def _call_cloudflare(
        self,
        prompt: str,
        doc: PDFDocument | None,
        include_images: bool,
        state: ConversationState | None = None,
    ) -> tuple[dict[str, Any], str, ConversationState]:
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{self.settings.cloudflare_account_id}"
            "/ai/v1/chat/completions"
        )
        return self._call_openai_compatible(
            provider="cloudflare",
            prompt=prompt,
            doc=doc,
            include_images=include_images,
            state=state,
            url=url,
            headers_fn=self._cloudflare_headers,
            max_tokens=min(self.settings.max_output_tokens, 16384),
        )

    def _call_openai_compatible(
        self,
        provider: str,
        prompt: str,
        doc: PDFDocument | None,
        include_images: bool,
        state: ConversationState | None,
        url: str,
        headers_fn,
        max_tokens: int,
    ) -> tuple[dict[str, Any], str, ConversationState]:
        pool = self._pool(provider)
        warm_state: ConversationState | None = None
        if state is not None:
            warm_state = ConversationState(
                provider=state.provider,
                model=state.model,
                transcript=list(state.transcript),
                last_model_text=state.last_model_text,
                used_vision=state.used_vision,
            )
            warm_state.transcript.append({"role": "user", "content": prompt})

        errors: list[str] = []
        for _ in range(max(2, pool.size * 2)):
            candidates = self._candidates(provider)
            if not candidates:
                raise RuntimeError(f"no {provider} model with remaining quota: " + " | ".join(errors))
            key = pool.next_key()
            if key is None:
                raise RuntimeError(f"no {provider} key currently available: " + " | ".join(errors))
            for candidate in candidates:
                try:
                    if warm_state is not None:
                        turn_messages = list(warm_state.transcript)
                        turn_state = warm_state
                    else:
                        content = self._compat_user_content(
                            EXTRACTION_REFERENCE + "\n\n" + prompt, doc, include_images, candidate, provider
                        )
                        turn_messages = [
                            {"role": "system", "content": SYSTEM_PROMPT + " Return one valid JSON object only."},
                            {"role": "user", "content": content},
                        ]
                        turn_state = ConversationState(
                            provider=provider,
                            model=candidate,
                            transcript=turn_messages,
                            used_vision=isinstance(content, list),
                        )
                    self._pacer.wait()
                    body: dict[str, Any] = {
                        "model": candidate,
                        "messages": turn_messages,
                        "temperature": 0.1,
                        "max_tokens": max_tokens,
                    }
                    if provider == "openrouter":
                        # These are extraction turns, not puzzle-solving turns: the
                        # JSON answer is wanted, not a chain of thought. Reasoning
                        # models otherwise burn whole token budgets on analysis and
                        # emit nothing (observed as empty completions).
                        body["reasoning"] = {"enabled": False}
                    raw = self._openai_compat_post(url, headers_fn(key), body, use_json_mode=True)
                    data = _extract_json(raw)
                    pool.note_success(key)
                    turn_state.model = candidate
                    turn_state.last_model_text = raw
                    turn_state.transcript = turn_messages + [{"role": "assistant", "content": raw}]
                    return data, candidate, turn_state
                except Exception as exc:
                    action = self._handle_common_error(provider, candidate, exc, key, errors)
                    if action == "next-candidate":
                        continue
                    break  # next key
        raise RuntimeError(f"{provider} attempts exhausted: " + " | ".join(errors))

    # --------------------------------------------------------------- validation

    def _check_extraction(self, data: dict[str, Any]) -> None:
        if not isinstance(data.get("payables", []), list):
            raise ValueError("Extraction field 'payables' must be an array")
        if not isinstance(data.get("is_payable"), bool):
            # Conservative compatibility for providers that return string booleans.
            raw = str(data.get("is_payable", "")).lower()
            if raw in {"true", "false"}:
                data["is_payable"] = raw == "true"
            else:
                raise ValueError("Extraction must include boolean is_payable")
