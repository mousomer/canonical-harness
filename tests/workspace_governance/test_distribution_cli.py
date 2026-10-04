"""Guarded control dispatch uses trusted scratch contexts, never product scripts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.policy_kit_distribution import cli
from tools.workspace_governance import fleet


@pytest.fixture
def control(tmp_path, monkeypatch):
    kit = tmp_path / "kit"
    private = tmp_path / "private"
    kit.mkdir()
    private.mkdir(mode=0o700)
    registry = {
        "schema_version": 1,
        "checkouts": [
            {
                "id": "first",
                "path": str(tmp_path / "consumer"),
                "repository_id": "fixture",
                "integration_mode": "direct",
            },
            {
                "id": "held",
                "path": str(tmp_path / "held"),
                "repository_id": "held-fixture",
                "integration_mode": "direct",
            },
        ],
    }
    local = {
        "private_bundle": str(private),
        "registry": str(private / "registry.json"),
        "plan": str(private / "plan.json"),
        "evidence": str(private / "evidence.json"),
        "audit": str(private / "audit.json"),
        "baseline_evidence": str(private / "baseline.json"),
        "registry_sha256": fleet.digest(fleet.canonical(registry)),
        "blocked_execution": [{"id": "held", "reason": "private-sensitive-reason"}],
    }
    fleet.atomic(Path(local["registry"]), fleet.canonical(registry))
    context = {
        "kit": kit,
        "local": local,
        "active": {"release_manifest_sha256": "a" * 64},
        "release_path": kit / "release.json",
        "runtime_root": ROOT,
        "lock_path": kit / "distribution.lock",
        "release": {},
    }
    calls = []

    def result(status, **extra):
        return {
            "schema_version": 1,
            "checkouts": [{"id": "first", "status": status, "errors": []}],
            "failures": [],
            **extra,
        }

    def plan(*args, **kwargs):
        calls.append(("plan", args, kwargs))
        return result("unchanged")

    def audit(*args, **kwargs):
        calls.append(("audit", args, kwargs))
        return result("incomplete", ok=False, failures=["first"])

    def verify(*args, **kwargs):
        calls.append(("verify", args, kwargs))
        outcome = result(
            "baseline_passed" if kwargs["baseline_only"] else "passed", ok=True
        )
        fleet.atomic(kwargs["output_path"], fleet.canonical(outcome))
        return outcome

    def apply(*args, **kwargs):
        calls.append(("apply", args, kwargs))
        return result("installed")

    def undo(*args, **kwargs):
        calls.append(("undo", args, kwargs))
        return result("undone")

    api = SimpleNamespace(
        plan=plan,
        audit=audit,
        collect_acceptance=verify,
        apply=apply,
        undo=undo,
        atomic=fleet.atomic,
        canonical=fleet.canonical,
        load=fleet.load,
        registry_rows=fleet.registry_rows,
    )
    monkeypatch.setattr(cli, "_selected_fleet", lambda root: api)
    return context, calls, api


def invoke(control, command="sync", tail=None):
    context, _calls, _api = control
    return cli.dispatch(command, tail or [], context_provider=lambda: context)


@pytest.mark.parametrize("tail", [[], ["plan"], ["--dry-run"]])
def test_sync_defaults_only_to_plan(control, tail, capsys):
    assert invoke(control, tail=tail) == 0
    assert [call[0] for call in control[1]] == ["plan"]
    output = json.loads(capsys.readouterr().out)
    assert output["product_execution"] == "not_requested"


def test_default_verify_hold_rejects_before_dispatch_and_hides_reason(control, capsys):
    assert invoke(control, tail=["verify"]) == 2 and control[1] == []
    output = capsys.readouterr()
    assert "HELD_PRODUCT_VERIFICATION" in output.err
    assert "private-sensitive-reason" not in output.out + output.err


def test_selected_product_and_baseline_remain_distinct(control):
    assert invoke(control, tail=["verify", "--checkout", "first"]) == 0
    assert not control[1][-1][2]["baseline_only"]
    assert invoke(control, tail=["baseline", "--checkout", "held"]) == 0
    assert control[1][-1][2]["baseline_only"]
    assert control[1][-1][2]["output_path"] == Path(
        control[0]["local"]["baseline_evidence"]
    )


def test_optional_baseline_hold_is_enforced_before_dispatch(control):
    control[0]["local"]["blocked_baseline_execution"] = [
        {"id": "held", "reason": "baseline needs review"}
    ]
    assert (
        invoke(control, tail=["baseline", "--checkout", "held"]) == 2
        and control[1] == []
    )


@pytest.mark.parametrize(
    "tail",
    [
        ["verify", "--checkout", "missing"],
        ["verify", "--checkout", "first", "--checkout", "first"],
        ["verify", "--checkout", "first", "--unsafe", "private-sensitive-argument"],
        ["plan", "private-sensitive-argument"],
        ["invalid-private-sensitive-command"],
    ],
)
def test_invalid_selectors_and_arguments_are_bounded_refusals(control, tail, capsys):
    assert invoke(control, tail=tail) == 2 and control[1] == []
    output = capsys.readouterr()
    assert "private-sensitive" not in output.out + output.err


def test_default_audit_is_structural_and_uses_separate_existing_evidence(control):
    context, calls, _api = control
    fleet.atomic(Path(context["local"]["evidence"]), b"{}\n")
    fleet.atomic(Path(context["local"]["baseline_evidence"]), b"{}\n")
    assert invoke(control, command="audit") == 1
    assert [call[0] for call in calls] == ["audit"]
    assert calls[0][2] == {
        "run_checks": True,
        "evidence_path": Path(context["local"]["evidence"]),
        "baseline_evidence_path": Path(context["local"]["baseline_evidence"]),
    }


def test_saved_plan_must_match_registry_release_and_private_root(control):
    context, calls, _api = control
    local = context["local"]
    planned = Path(local["plan"])
    receipt = Path(local["private_bundle"]) / "receipt.json"
    declaration = {
        "schema_version": 1,
        "release_manifest_sha256": context["active"]["release_manifest_sha256"],
        "registry_sha256": local["registry_sha256"],
        "registry_path": local["registry"],
    }
    fleet.atomic(planned, fleet.canonical(declaration))
    assert (
        invoke(
            control, tail=["apply", "--plan", str(planned), "--receipt", str(receipt)]
        )
        == 0
    )
    assert calls[-1][0] == "apply"
    declaration["registry_sha256"] = "b" * 64
    fleet.atomic(planned, fleet.canonical(declaration))
    calls.clear()
    assert (
        invoke(
            control, tail=["apply", "--plan", str(planned), "--receipt", str(receipt)]
        )
        == 2
        and not calls
    )


def test_receipt_paths_cannot_alias_live_state_or_escape_private_bundle(control):
    context, calls, _api = control
    assert (
        invoke(control, tail=["undo", "--receipt", context["local"]["registry"]]) == 2
    )
    assert (
        invoke(
            control, tail=["undo", "--receipt", str(context["kit"] / "outside.json")]
        )
        == 2
    )
    assert not calls


def test_changed_config_is_refreshed_after_lock_before_dispatch(control):
    context, calls, _api = control
    count = 0

    def provider():
        nonlocal count
        count += 1
        if count == 2:
            context["local"]["registry_sha256"] = "f" * 64
        return context

    assert cli.dispatch("sync", ["plan"], context_provider=provider) == 2
    assert count == 2 and not calls


def test_state_lock_moves_are_rejected(control):
    context, calls, _api = control
    count = 0

    def provider():
        nonlocal count
        count += 1
        updated = dict(context)
        if count == 2:
            updated["lock_path"] = context["kit"] / "other.lock"
        return updated

    assert cli.dispatch("sync", ["plan"], context_provider=provider) == 2 and not calls


def test_private_runtime_error_never_reaches_console(control, capsys):
    def failed(*args, **kwargs):
        raise OSError("private-sensitive-location")

    control[2].plan = failed
    assert invoke(control) == 2
    output = capsys.readouterr()
    assert "private-sensitive" not in output.out + output.err
    assert "DISTRIBUTION_VALIDATION_FAILED" in output.err


def test_approved_runtime_import_rejects_foreign_cached_module(tmp_path):
    root = tmp_path / "selected"
    target = root / "tools/workspace_governance/fleet.py"
    target.parent.mkdir(parents=True)
    target.write_text("# Do not execute this foreign stub.\n")
    with pytest.raises(cli.ControlError, match="FOREIGN_RUNTIME_IMPORT"):
        cli._selected_fleet(root)


def test_private_symlink_destinations_refuse_before_dispatch(control):
    context, calls, _api = control
    path = Path(context["local"]["plan"])
    target = Path(context["local"]["private_bundle"]) / "target.json"
    path.symlink_to(target)
    assert invoke(control) == 2 and not calls


def selfcheck_context(control, tmp_path):
    context, _calls, _api = control
    kit = context["kit"]
    runtime_source = kit / "tools/workspace_governance/module.py"
    runtime_source.parent.mkdir(parents=True)
    runtime_source.write_text("# Frozen machinery\n")
    control_source = kit / "tools/policy_kit_distribution/cli.py"
    control_source.parent.mkdir(parents=True)
    control_source.write_text("# Frozen control\n")
    row = lambda path: {
        "path": path.relative_to(kit).as_posix(),
        "sha256": fleet.digest(path.read_bytes()),
        "mode": 0o644,
    }
    context["release"] = {
        "files": [row(runtime_source)],
        "distribution": {"files": [row(control_source)]},
    }
    archive = kit / "compatibility/1.1.3"
    archive.mkdir(parents=True)
    entries = []
    for index in range(19):
        source = archive / ("original-" + str(index))
        source.write_text("exact legacy " + str(index) + "\n")
        entries.append(
            {
                "path": source.name,
                "sha256": fleet.digest(source.read_bytes()),
                "mode": 0o644,
            }
        )
    manifest = {"files": entries}
    (archive / "manifest.json").write_bytes(fleet.canonical(manifest))
    context["active"].update(
        legacy_manifest_sha256=fleet.digest(fleet.canonical(manifest)),
        distribution_version="1.3.0",
    )
    (kit / "POLICY_VERSION.json").write_bytes(
        fleet.canonical({"policy_version": "1.3.0"})
    )
    runtime = tmp_path / "immutable-runtime"
    context["runtime_root"] = runtime
    links = []
    for manager in (".codex", ".claude"):
        for name in ("governance-context", "governance-maintenance"):
            directory = runtime / "tools/workspace_governance/skills" / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "SKILL.md").write_text("immutable skill\n")
            discovery = tmp_path / manager / "skills" / name
            discovery.parent.mkdir(parents=True, exist_ok=True)
            discovery.symlink_to(directory)
            links.append({"path": str(discovery), "target": str(directory)})
    context["local"]["skill_links"] = links
    return context


def test_selfcheck_validates_editable_sources_legacy_and_immutable_links(
    control, tmp_path, capsys
):
    selfcheck_context(control, tmp_path)
    assert invoke(control, command="selfcheck") == 0
    output = json.loads(capsys.readouterr().out)
    assert output["skill_links"] == 4 and output["legacy_source_files"] == 19
    assert not control[1]


def test_selfcheck_rejects_candidate_skill_target(control, tmp_path):
    context = selfcheck_context(control, tmp_path)
    link = context["local"]["skill_links"][0]
    path = Path(link["path"])
    path.unlink()
    editable = context["kit"] / "tools/workspace_governance/skills/governance-context"
    editable.mkdir(parents=True)
    path.symlink_to(editable)
    link["target"] = str(editable)
    assert invoke(control, command="selfcheck") == 2


def test_live_artifact_overwrites_preserve_exact_private_history(control):
    context, _calls, _api = control
    path = Path(context["local"]["plan"])
    original = b'{"historic":"preserve exact bytes"}\n'
    fleet.atomic(path, original)
    assert invoke(control) == 0
    history = Path(context["local"]["private_bundle"]) / "history"
    archived = list(history.iterdir())
    assert len(archived) == 1 and archived[0].read_bytes() == original
    assert archived[0].stat().st_mode & 0o777 == 0o600
    assert history.stat().st_mode & 0o777 == 0o700
