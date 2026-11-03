"""Packaging-gate regression tests (deterministic, no API calls)."""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import package_submission as pkg  # noqa: E402


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    inputs = tmp_path / "documents"
    outputs = tmp_path / "output"
    inputs.mkdir()
    outputs.mkdir()
    (inputs / "INV-01.pdf").write_bytes(b"%PDF-not-parsed-by-the-validator")
    return inputs, outputs


def test_packager_blocks_a_missing_output(tmp_path):
    inputs, outputs = _prepare(tmp_path)
    errors, _ = pkg.validate(ROOT, inputs, outputs)
    assert any("Missing output" in error for error in errors)


def test_packager_blocks_an_empty_placeholder(tmp_path):
    inputs, outputs = _prepare(tmp_path)
    (outputs / "INV-01.json").write_text(
        json.dumps({"file": "INV-01.pdf", "payables": [], "declined": []}), encoding="utf-8"
    )
    errors, _ = pkg.validate(ROOT, inputs, outputs)
    assert any("empty placeholder" in error for error in errors)


def test_packager_accepts_a_real_reconciled_output(tmp_path):
    inputs, outputs = _prepare(tmp_path)
    shutil.copy2(ROOT / "output" / "INV-01.json", outputs / "INV-01.json")
    errors, _ = pkg.validate(ROOT, inputs, outputs)
    assert errors == []


def test_packager_blocks_a_payable_that_does_not_reconcile(tmp_path):
    inputs, outputs = _prepare(tmp_path)
    broken = json.loads((ROOT / "output" / "INV-01.json").read_text(encoding="utf-8"))
    broken["payables"][0]["gross_total"] = "999.99"  # printed total no longer reproduced
    (outputs / "INV-01.json").write_text(json.dumps(broken), encoding="utf-8")
    errors, _ = pkg.validate(ROOT, inputs, outputs)
    assert any("ERP reconciliation" in error for error in errors)
