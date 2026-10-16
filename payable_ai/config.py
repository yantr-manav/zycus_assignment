from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

from .keys import keys_from_env


@dataclass(frozen=True)
class Settings:
    gemini_api_keys: tuple[str, ...]
    groq_api_keys: tuple[str, ...]
    gemini_model: str
    gemini_fallback_models: tuple[str, ...]
    groq_model: str
    groq_fallback_models: tuple[str, ...]
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
    # Backward-compatible singular accessors (tests and tools read these).
    gemini_api_key: str = field(default="")
    groq_api_key: str = field(default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "gemini_api_key", self.gemini_api_keys[0] if self.gemini_api_keys else "")
        object.__setattr__(self, "groq_api_key", self.groq_api_keys[0] if self.groq_api_keys else "")


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


def load_settings(provider_override: str | None = None) -> Settings:
    load_dotenv()
    provider = (provider_override or os.getenv("PAYABLE_AI_PROVIDER", "auto")).strip().lower()
    if provider not in {"auto", "gemini", "groq"}:
        raise ValueError("Provider must be one of: auto, gemini, groq")
    gemini_keys = keys_from_env("GEMINI")
    groq_keys = keys_from_env("GROQ")
    fallbacks = tuple(
        m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()
    )
    groq_fallbacks = tuple(
        m.strip() for m in os.getenv("GROQ_FALLBACK_MODELS", "").split(",") if m.strip()
    )
    return Settings(
        gemini_api_keys=tuple(gemini_keys),
        groq_api_keys=tuple(groq_keys),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip(),
        gemini_fallback_models=fallbacks,
        groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip(),
        groq_fallback_models=groq_fallbacks,
        groq_max_document_chars=_int_env("GROQ_MAX_DOCUMENT_CHARS", 24000, 4000, 200000),
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
    )
