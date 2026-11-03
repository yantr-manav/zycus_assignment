"""Live provider smoke test (opt-in; skipped unless explicitly enabled).

Unit and integration tests never touch the network. This module exercises ONE
real document end-to-end through the configured provider ladder, and only runs
when credentials and quota are available:

    PAYABLE_AI_LIVE_SMOKE=1 python -m pytest tests/test_live_smoke.py -q

It is intentionally excluded from the default suite so CI and offline reviews
never depend on provider availability.
"""

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LIVE = os.getenv("PAYABLE_AI_LIVE_SMOKE", "").strip().lower() in {"1", "true", "yes"}
SMOKE_DOCUMENT = ROOT / "documents" / "INV-33.pdf"


@pytest.mark.skipif(
    not LIVE,
    reason="live provider smoke disabled (set PAYABLE_AI_LIVE_SMOKE=1 with working credentials)",
)
def test_live_provider_smoke(tmp_path):
    from payable_ai.config import load_settings
    from payable_ai.llm import LLMRouter
    from payable_ai.master_data import MasterData
    from payable_ai.pipeline import process_one
    from payable_ai.validation import reconcile

    assert SMOKE_DOCUMENT.exists()
    settings = load_settings()
    router = LLMRouter(settings)
    router.ensure_available()
    masters = MasterData(ROOT / "master_data")

    diagnostics = process_one(SMOKE_DOCUMENT, tmp_path, masters, router, settings)

    assert diagnostics["status"] != "ERROR_NO_VALID_EXTRACTION"
    result = json.loads((tmp_path / "INV-33.json").read_text(encoding="utf-8"))
    assert result["payables"], "expected one payable from the smoke document"
    assert reconcile(result["payables"][0])["status"] == "MATCH"
