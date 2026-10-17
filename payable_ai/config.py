from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

from .keys import keys_from_env


PROVIDERS = {"auto", "gemini", "groq", "openrouter", "cloudflare"}


@dataclass(frozen=True)
class Settings:
    gemini_api_keys: tuple[str, ...]
    groq_api_keys: tuple[str, ...]
    openrouter_api_keys: tuple[str, ...]
    cloudflare_api_token: str
    cloudflare_account_id: str
    gemini_model: str
    gemini_fallback_models: tuple[str, ...]
    groq_model: str
    groq_fallback_models: tuple[str, ...]
    openrouter_model: str
    openrouter_fallback_models: tuple[str, ...]
    cloudflare_model: str
    cloudflare_fallback_models: tuple[str, ...]
    ladder_vision: tuple[str, ...]
    ladder_text: tuple[str, ...]
    groq_max_document_chars: int
    provider: str
    max_vision_pages: int
    render_dpi: int
    ocr_mode: str
    max_document_chars: int
    retries: int
    repair_on_mismatch: bool
    repair_rounds: int
    min_call_interval_seconds: float
    max_output_tokens: int
    extract_budget_seconds: int
    repair_budget_seconds: int
    # Backward-compatible singular accessors (tests and tools read these).
    gemini_api_key: str = field(default="")
    groq_api_key: str = field(default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "gemini_api_key", self.gemini_api_keys[0] if self.gemini_api_keys else "")
        object.__setattr__(self, "groq_api_key", self.groq_api_keys[0] if self.groq_api_keys else "")

    def __repr__(self) -> str:
        """Never render credential material: tracebacks and logs include reprs."""
        parts = []
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if "key" in name or "token" in name:
                if isinstance(value, tuple):
                    value = tuple((f"{v[:4]}…[REDACTED]" if v else "") for v in value)
                elif isinstance(value, str):
                    value = f"{value[:4]}…[REDACTED]" if value else ""
            parts.append(f"{name}={value!r}")
        return "Settings(" + ", ".join(parts) + ")"


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def _list_env(name: str) -> tuple[str, ...]:
    return tuple(m.strip() for m in os.getenv(name, "").split(",") if m.strip())


def load_settings(provider_override: str | None = None) -> Settings:
    load_dotenv()
    provider = (provider_override or os.getenv("PAYABLE_AI_PROVIDER", "auto")).strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"Provider must be one of: {', '.join(sorted(PROVIDERS))}")
    gemini_keys = keys_from_env("GEMINI")
    groq_keys = keys_from_env("GROQ")
    openrouter_keys = keys_from_env("OPENROUTER")
    ladder_vision = _list_env("PROVIDER_LADDER_VISION") or ("gemini", "openrouter", "cloudflare", "groq")
    ladder_text = _list_env("PROVIDER_LADDER_TEXT") or ("groq", "openrouter", "cloudflare", "gemini")
    return Settings(
        gemini_api_keys=tuple(gemini_keys),
        groq_api_keys=tuple(groq_keys),
        openrouter_api_keys=tuple(openrouter_keys),
        cloudflare_api_token=os.getenv("CLOUDFLARE_API_TOKEN", "").strip(),
        cloudflare_account_id=os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip(),
        gemini_fallback_models=_list_env("GEMINI_FALLBACK_MODELS"),
        groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip(),
        groq_fallback_models=_list_env("GROQ_FALLBACK_MODELS"),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free").strip(),
        openrouter_fallback_models=_list_env("OPENROUTER_FALLBACK_MODELS"),
        cloudflare_model=os.getenv("CF_MODEL", "@cf/meta/llama-4-scout-17b-16e-instruct").strip(),
        cloudflare_fallback_models=_list_env("CF_FALLBACK_MODELS"),
        ladder_vision=tuple(ladder_vision),
        ladder_text=tuple(ladder_text),
        groq_max_document_chars=_int_env("GROQ_MAX_DOCUMENT_CHARS", 8000, 4000, 200000),
        provider=provider,
        max_vision_pages=_int_env("MAX_VISION_PAGES", 20, 1, 100),
        render_dpi=_int_env("PDF_RENDER_DPI", 120, 90, 250),
        ocr_mode=os.getenv("OCR_MODE", "auto").strip().lower(),
        max_document_chars=_int_env("MAX_DOCUMENT_CHARS", 90000, 10000, 400000),
        retries=_int_env("LLM_RETRIES", 2, 0, 4),
        repair_on_mismatch=os.getenv("REPAIR_ON_MISMATCH", "true").lower() in {"true", "1", "yes"},
        repair_rounds=_int_env("REPAIR_ROUNDS", 2, 0, 4),
        min_call_interval_seconds=float(os.getenv("MIN_CALL_INTERVAL_SECONDS", "5")),
        max_output_tokens=_int_env("MAX_OUTPUT_TOKENS", 24576, 1024, 65536),
        # Wall-clock budgets bound the damage from a slow/queued free-tier route:
        # per-document extraction stops trying providers past this point, and the
        # repair loop stops asking once the budget is spent.
        extract_budget_seconds=_int_env("EXTRACT_BUDGET_SECONDS", 480, 60, 3600),
        repair_budget_seconds=_int_env("REPAIR_BUDGET_SECONDS", 240, 30, 1800),
    )
