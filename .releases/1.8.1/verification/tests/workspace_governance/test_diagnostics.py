"""Actionability comes from fixed rule classes, never arbitrary checker output."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest
from support import baseline_script, scrubbed_environment
from tools.workspace_governance import diagnostics
from tools.workspace_governance.cli import gov

PRIVATE = "SYNTHETIC_PRIVATE_SOURCE_DO_NOT_ECHO"


def test_real_policy_checker_failure_identifies_missing_files(tmp_path):
    result = subprocess.run(
        ["bash", str(baseline_script("scripts/check_policy_compliance_template.sh"))],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    report = diagnostics.summarize(
        "policy_compliance", result.returncode, result.stdout, result.stderr
    )
    assert report["exit_status"] == 1
    assert report["error_count"] == 9
    assert any(
        row.get("location") == ".policy/policy_template_hashes.json"
        for row in report["diagnostics"]
    )
    assert "policy.missing-file" in diagnostics.render(report)


def test_real_sanitation_warnings_keep_classes_not_matched_rows(tmp_path):
    environment = scrubbed_environment(
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1"
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=environment)
    (tmp_path / "with spaces.txt").write_text(
        "/home/" + PRIVATE + "\nC:\\" + PRIVATE + "\n../" + PRIVATE + "\n"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, env=environment)
    result = subprocess.run(
        ["bash", str(baseline_script("scripts/check_git_sanitation_template.sh"))],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        env=environment,
    )
    report = diagnostics.summarize(
        "git_sanitation", result.returncode, result.stdout, result.stderr
    )
    assert report["exit_status"] == 0 and report["warning_count"] == 4
    assert len(report["diagnostics"]) == 4
    assert {row["rule_id"] for row in report["diagnostics"]} == {
        "sanitation.absolute-unix",
        "sanitation.absolute-windows",
        "sanitation.parent-traversal",
        "sanitation.spaced-filenames",
    }
    assert PRIVATE not in json.dumps(report) and PRIVATE not in diagnostics.render(
        report
    )


def test_template_drift_diagnostic_omits_untrusted_expected_hash(tmp_path):
    (tmp_path / ".policy").mkdir()
    (tmp_path / "template.txt").write_text("Approved file bytes.\n")
    (tmp_path / ".policy/policy_template_hashes.json").write_text(
        json.dumps({"hash_algorithm": "sha256", "templates": {"template.txt": PRIVATE}})
    )
    result = subprocess.run(
        [
            "bash",
            str(baseline_script("scripts/check_policy_template_drift_template.sh")),
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        env=scrubbed_environment(PYTHON_BIN=sys.executable),
    )
    report = diagnostics.summarize(
        "template_drift",
        result.returncode,
        result.stdout,
        result.stderr,
        allowed_locations=["template.txt"],
    )
    assert report["diagnostics"][0]["rule_id"] == "template.content-drift"
    assert report["diagnostics"][0]["location"] == "template.txt"
    assert PRIVATE not in json.dumps(report)


def test_unknown_output_only_supplies_digest_and_fixed_repair():
    report = diagnostics.summarize(
        "consumer_owned_checker", 23, (PRIVATE + "\n").encode()
    )
    assert (
        report["exit_status"] == 23
        and report["diagnostics"][0]["rule_id"] == "checker.failed"
    )
    assert report["unclassified_output_lines"] == 1
    assert PRIVATE not in json.dumps(report)


def test_structured_governance_reason_and_private_paths_never_echoed():
    value = [
        {
            "code": "CONFLICTING_VALUE",
            "fact": PRIVATE,
            "reason": PRIVATE,
            "repair": PRIVATE,
            "sources": ["/private/" + PRIVATE, "docs/authority.md"],
        }
    ]
    report = diagnostics.summarize(
        "consumer_ownership_check",
        1,
        json.dumps(value).encode(),
        allowed_locations=["docs/authority.md"],
    )
    assert report["diagnostics"][0]["location"] == "docs/authority.md"
    assert report["diagnostics"][0]["rule_id"] == "CONFLICTING_VALUE"
    assert PRIVATE not in json.dumps(report)


def test_skipped_file_count_large_fixture_has_one_line_quiet_success():
    value = {
        "profile": "git-visible-text-patterns-v1",
        "inspected_files": 12,
        "finding_count": 0,
        "skipped_count": 100_000,
        "error_count": 0,
        "findings": [],
        "errors": [],
        "skipped": [{"reason": "binary", "path": PRIVATE} for _ in range(50)],
    }
    report = diagnostics.summarize("pack_sanitation", 0, json.dumps(value).encode())
    rendered = diagnostics.render(report)
    assert report["metrics"]["skipped_count"] == 100_000 and report[
        "sampled_skip_reasons"
    ] == {"binary": 50}
    assert len(rendered.splitlines()) == 1 and len(rendered) <= 256
    assert PRIVATE not in rendered and PRIVATE not in json.dumps(report)


def test_warning_bound_counts_all_classes_without_printing_arbitrary_prose():
    output = ("WARNING: " + PRIVATE + "\n") * 100_000
    report = diagnostics.summarize("consumer_checker", 0, output.encode())
    assert report["classification_input_truncated"]
    assert report["warning_count"] > 12 and len(report["diagnostics"]) == 12
    assert len(diagnostics.render(report).splitlines()) <= 14
    assert PRIVATE not in diagnostics.render(report)


@pytest.mark.parametrize(
    "location",
    ["../outside", "/private/absolute", "safe.txt\n" + PRIVATE, "safe.txt: " + PRIVATE],
)
def test_locations_from_output_require_declared_allowlist(location):
    output = ("Missing required file: " + location).encode()
    report = diagnostics.summarize("policy_compliance", 1, output)
    assert all("location" not in row for row in report["diagnostics"])
    assert PRIVATE not in json.dumps(report)


def test_known_relative_control_characters_are_escaped():
    name = "directory/with\tcontrol.txt"
    value = {
        "profile": "git-visible-text-patterns-v1",
        "finding_count": 1,
        "findings": [{"path": name, "line": 4, "pattern_id": "private-key"}],
    }
    report = diagnostics.summarize(
        "pack_sanitation", 1, json.dumps(value).encode(), allowed_locations=[name]
    )
    rendered = diagnostics.render(report)
    assert "\\t" in rendered and "\t" not in rendered


def test_nested_summary_accepts_only_fixed_governance_rules():
    value = {
        "kind": "bounded-checker-diagnostics",
        "diagnostics": [
            {"rule_id": "PACK_DRIFT", "severity": "error", "remediation": PRIVATE},
            {"rule_id": PRIVATE, "remediation": PRIVATE},
        ],
        "error_count": 2,
    }
    report = diagnostics.summarize("consumer_generated", 1, json.dumps(value).encode())
    assert (
        report["error_count"] == 1
        and report["diagnostics"][0]["rule_id"] == "PACK_DRIFT"
    )
    assert PRIVATE not in json.dumps(report)


def test_gov_summary_pass_is_bounded(checkout, capsys):
    assert gov.main(["--root", str(checkout), "check", "--summary"]) == 0
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1 and "workspace_check: passed" in output


def test_execution_drains_large_output_without_raw_logs(tmp_path):
    source = (
        "import sys; "
        f"sys.stdout.buffer.write(({PRIVATE!r}+'\\n').encode()*100_000); "
        f"sys.stderr.buffer.write(({PRIVATE!r}+'\\n').encode()*50_000); "
        "sys.exit(23)"
    )
    report, code = diagnostics.execute(
        "consumer_checker", [sys.executable, "-c", source], tmp_path
    )
    stdout = (PRIVATE + "\n").encode() * 100_000
    stderr = (PRIVATE + "\n").encode() * 50_000
    assert code == 23 and report["exit_status"] == 23
    assert report["execution"]["stdout_bytes"] == len(stdout)
    assert report["execution"]["stderr_bytes"] == len(stderr)
    assert report["execution"]["stdout_sha256"] == hashlib.sha256(stdout).hexdigest()
    assert report["execution"]["stderr_sha256"] == hashlib.sha256(stderr).hexdigest()
    assert "stdout_preview" not in report["execution"]
    assert "stderr_preview" not in report["execution"]
    assert report["classification_input_truncated"]
    assert report["counts_scope"] == "bounded captured prefix"
    assert len(diagnostics.render(report).splitlines()) <= 14
    assert PRIVATE not in json.dumps(report)
    assert list(tmp_path.iterdir()) == []


def test_execution_rejects_checker_before_running_any_command(tmp_path):
    marker = tmp_path / "execution-marker"
    source = "from pathlib import Path; Path('execution-marker').touch()"
    with pytest.raises(ValueError):
        diagnostics.execute(PRIVATE, [sys.executable, "-c", source], tmp_path)
    assert not marker.exists()


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf")])
def test_execution_requires_finite_positive_numeric_timeout(tmp_path, timeout):
    with pytest.raises(ValueError):
        diagnostics.execute(
            "consumer_checker",
            [sys.executable, "-c", "pass"],
            tmp_path,
            timeout=timeout,
        )


def test_execution_suppresses_shell_startup_injection(tmp_path):
    startup = tmp_path / "startup.sh"
    marker = tmp_path / "startup-marker"
    startup.write_text("touch startup-marker; exit 91\n")
    report, code = diagnostics.execute(
        "consumer_checker",
        ["bash", "-c", f"printf '%s\\n' '{PRIVATE}'; exit 23"],
        tmp_path,
        environment=scrubbed_environment(BASH_ENV=str(startup)),
    )
    assert code == 23 and not marker.exists()
    assert PRIVATE not in json.dumps(report)


def test_execution_timeout_has_bounded_actionable_rule(tmp_path):
    report, code = diagnostics.execute(
        "consumer_checker",
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        timeout=0.05,
    )
    assert code == 124 and report["execution"]["timed_out"]
    assert report["diagnostics"][0]["rule_id"] == "checker.timeout"
    assert report["error_count"] == 1 and report["execution"]["duration_seconds"] < 3


def test_unavailable_execution_has_safe_actionable_rule(tmp_path):
    report, code = diagnostics.execute(
        "consumer_checker", [str(tmp_path / PRIVATE)], tmp_path
    )
    assert code == 2 and report["execution_unavailable"]
    assert report["diagnostics"][0]["rule_id"] == "checker.unavailable"
    assert PRIVATE not in json.dumps(report)
