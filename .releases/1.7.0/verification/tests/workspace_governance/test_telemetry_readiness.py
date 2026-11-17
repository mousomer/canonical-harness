"""Readiness comes from a verified private append, never enabled configuration alone."""

from __future__ import annotations

import json

import pytest
from support import write

from tools.workspace_governance import telemetry
from tools.workspace_governance.cli import gov
from tools.workspace_governance.resolver.core import GovernanceResolver


def enabled(checkout):
    write(
        checkout / ".governance/workspace.local.json",
        {"schema_version": 1, "telemetry": {"enabled": True}},
    )
    return GovernanceResolver.for_root(checkout).telemetry_settings()


def test_disabled_status_and_probe_create_nothing(checkout):
    settings = GovernanceResolver.for_root(checkout).telemetry_settings()
    result, code = telemetry.selftest(settings, checkout)
    assert code == 0 and result["state"] == "disabled" and result["probe"] == "not_run"
    assert not settings.directory.exists()


def test_configured_store_is_unknown_until_actual_probe(checkout):
    settings = enabled(checkout)
    assert telemetry.status(settings)["state"] == "unknown"
    assert not settings.directory.exists()
    result, code = telemetry.selftest(settings, checkout)
    assert code == 0 and result["state"] == "recording_observed"
    assert result["observed_at"] and len(result["store_identity"]) == 64
    assert result["current_writability"] == "unknown" and result["synthetic_probe"]
    assert result["product_evidence"] is False
    events = list(telemetry.iter_events(settings.directory))
    assert len(events) == 1 and events[0]["payload"]["synthetic_probe"] is True
    assert events[0]["payload"]["program"] == "gov-telemetry-selftest"
    assert telemetry.event_issues(events[0]) == []
    assert settings.directory.stat().st_mode & 0o077 == 0
    assert (
        settings.key_directory.joinpath(telemetry.READINESS_FILENAME).stat().st_mode
        & 0o077
        == 0
    )
    assert not settings.directory.joinpath(telemetry.KEY_FILENAME).exists()
    assert str(settings.directory) not in json.dumps(result)


@pytest.mark.parametrize(
    "tampering", ["deleted_event", "altered_receipt", "key_rotation", "event_symlink"]
)
def test_status_refuses_unverified_last_append(checkout, tampering):
    settings = enabled(checkout)
    assert telemetry.selftest(settings, checkout)[1] == 0
    receipt = settings.key_directory / telemetry.READINESS_FILENAME
    event_file = next(settings.directory.glob("events-*.jsonl"))
    if tampering == "deleted_event":
        event_file.unlink()
    elif tampering == "altered_receipt":
        data = json.loads(receipt.read_text())
        data["observed_at"] = "2999-01-01T00:00:00Z"
        receipt.write_text(json.dumps(data))
    elif tampering == "key_rotation":
        (settings.key_directory / telemetry.KEY_FILENAME).write_bytes(
            b"x" * telemetry.KEY_BYTES
        )
    else:
        event_file.unlink()
        event_file.symlink_to(receipt)
    assert telemetry.status(settings)["state"] == "unknown"


def test_explicit_unwritable_probe_fails_without_secret_text(checkout, monkeypatch):
    settings = enabled(checkout)

    def denied(*args, **kwargs):
        raise PermissionError("private-token-do-not-print")

    monkeypatch.setattr(telemetry, "append_event", denied)
    result, code = telemetry.selftest(settings, checkout)
    assert code == 1 and result["state"] == "unwritable_observed"
    assert "private-token" not in json.dumps(result)
    assert telemetry.status(settings)["state"] == "unwritable_observed"


def test_record_failure_never_changes_product_command_status(
    checkout, monkeypatch, capsys
):
    monkeypatch.setattr(gov, "_dispatch", lambda *args: 7)

    def failed(*args):
        raise RuntimeError("private-token-do-not-print")

    monkeypatch.setattr(gov, "_record", failed)
    assert gov.main(["--root", str(checkout), "check"]) == 7
    warning = capsys.readouterr().err
    assert "RuntimeError" in warning and "private-token" not in warning


def test_status_command_is_read_only_and_does_not_self_record(
    checkout, monkeypatch, capsys
):
    settings = enabled(checkout)
    monkeypatch.setattr(
        gov, "_record", lambda *args: pytest.fail("status must not append")
    )
    assert gov.main(["--root", str(checkout), "telemetry", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "unknown"
    assert not settings.directory.exists()


def test_real_store_path_conflict_is_not_configuration_readiness(checkout):
    settings = enabled(checkout)
    settings.directory.parent.mkdir(parents=True)
    settings.directory.write_text("This path is not a recording directory.\n")
    result, code = telemetry.selftest(settings, checkout)
    assert code == 1 and result["state"] == "unwritable_observed"
    assert result["product_evidence"] is False


def test_missing_configuration_is_unknown_not_disabled(tmp_path):
    settings = telemetry.resolve_settings(None, None)
    assert telemetry.status(settings)["state"] == "unknown"
    assert telemetry.selftest(settings, tmp_path)[1] == 1


def test_normal_record_persists_verified_readiness(checkout):
    settings = enabled(checkout)
    key = telemetry.telemetry_key(settings.key_directory)

    def event():
        return telemetry.build_command_execution(
            root=checkout,
            workspace_id=settings.workspace_id,
            project_id=None,
            producer_version=telemetry.producer_version(),
            program="gov",
            arguments=[],
            key=key,
            exit_status=0,
        )

    assert telemetry.record(settings, event) is not None
    result = telemetry.status(settings)
    assert (
        result["state"] == "recording_observed" and result["synthetic_probe"] is False
    )
    assert result["product_evidence"] is False


@pytest.mark.parametrize(
    "timestamp",
    ["../../invalid-persistence-time", "2026-10-04T00:00:00", "not-a-timestamp-at-all"],
)
def test_invalid_persistence_times_cannot_select_external_event_paths(
    checkout, timestamp
):
    settings = enabled(checkout)
    key = telemetry.telemetry_key(settings.key_directory)
    event = telemetry.build_command_execution(
        root=checkout,
        workspace_id=settings.workspace_id,
        project_id=None,
        producer_version=telemetry.producer_version(),
        program="gov",
        arguments=[],
        key=key,
    )
    event["recorded_at"] = timestamp
    with pytest.raises(telemetry.TelemetryError):
        telemetry.append_event(settings.directory, event)
    assert not settings.directory.exists()
