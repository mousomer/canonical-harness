"""Keep all serialized synthetic inputs in per-test temporary storage."""

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def temporary_provider_inputs(tmp_path, request, monkeypatch):
    from synthetic_inputs import write_synthetic_inputs

    if hasattr(request.module, "FIXTURES"):
        generated = write_synthetic_inputs(tmp_path / "synthetic-inputs")
        monkeypatch.setattr(request.module, "FIXTURES", generated)
