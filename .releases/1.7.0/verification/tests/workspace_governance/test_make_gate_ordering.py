"""Observe generated Make recipe ordering, including rejected executions."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from tools.workspace_governance import fleet
from tools.workspace_governance.fleet_support import MAKE_ENVIRONMENT_VARIABLES, make_runner
from test_fleet import apply_fixture, setup, write


RUNNER = {"kind": "make", "path": "Makefile", "targets": ["test", "test-sanitize"]}
PRODUCTS = {"core", "experiment", "sanitize"}


def invocation(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for key in MAKE_ENVIRONMENT_VARIABLES:
        env.pop(key, None)
    return subprocess.run(
        ["make", *args], cwd=root, env=env, capture_output=True, text=True,
        check=False, timeout=20,
    )


def fixture_runner(root: Path) -> dict:
    write(root / "gov", "#!/bin/sh\nprintf 'gate-start\\n' >> events\nsleep 0.05\n[ ! -f fail-gov ] || exit 1\nprintf 'gate-passed\\n' >> events\n", 0o755)
    text = (
        ".PHONY: test test-core test-sanitize prepare routing\n"
        "test: test-core\n\t@printf 'experiment\\n' >> events\n"
        "test-core: prepare\n\t@printf 'core\\n' >> events\n"
        "test-sanitize: prepare\n\t@printf 'sanitize\\n' >> events\n"
        "prepare:\n\t@printf 'build\\n' >> events\n"
        "routing:\n\t@printf 'routing\\n' >> events\n\t@test ! -f fail-routing\n"
    )
    # A previous installation plus a project-owned prerequisite must survive
    # regeneration without making the prerequisite depend on its own gate.
    text = make_runner(text, RUNNER)
    text += "\nworkspace-governance-check: routing\n"
    runner = {**RUNNER, "build_prerequisites": ["prepare"]}
    write(root / "Makefile", make_runner(text, runner))
    return runner


@pytest.mark.parametrize("args", [
    ["test"], ["-j4", "test"], ["test", "test-sanitize"],
    ["-j4", "test", "test-sanitize"], ["test-core"],
    ["-j4", "test-core"], ["-k", "-j4", "test", "test-sanitize"],
])
@pytest.mark.parametrize("failure", [None, "fail-gov", "fail-routing"])
def test_recipe_ordering_and_rejection(tmp_path, args, failure):
    fixture_runner(tmp_path)
    if failure:
        write(tmp_path / failure, "rejected fixture\n")
    result = invocation(tmp_path, *args)
    events = (tmp_path / "events").read_text().splitlines()
    if failure:
        assert result.returncode != 0, result.stdout + result.stderr
        assert not PRODUCTS.intersection(events), events
        assert "gate-passed" not in events
        if failure == "fail-routing":
            assert "gate-start" not in events
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        expected = {"core"} if "test-core" in args else {"core", "experiment"}
        if "test-sanitize" in args:
            expected.add("sanitize")
        assert PRODUCTS.intersection(events) == expected
        assert events.index("routing") < events.index("gate-start")
        assert all(events.index("gate-passed") < events.index(event) for event in expected)
        assert events.count("gate-start") == 1


def test_order_only_edges_preserve_automatic_variables_and_artifact_freshness(tmp_path):
    write(tmp_path / "gov", "#!/bin/sh\nexit 0\n", 0o755)
    write(tmp_path / "source", "source\n")
    text = (
        ".PHONY: test\n"
        "test: asset\n\t@printf 'test\\n' >> events\n"
        "asset: source\n"
        "\t@printf '%s|%s|%s\\n' '$^' '$<' '$?' >> events\n"
        "\t@cp source asset\n"
    )
    runner = {"targets": ["test"], "build_prerequisites": ["source"]}
    write(tmp_path / "Makefile", make_runner(text, runner))
    assert invocation(tmp_path, "test").returncode == 0
    before = (tmp_path / "asset").stat().st_mtime_ns
    assert invocation(tmp_path, "-j4", "test").returncode == 0
    assert (tmp_path / "asset").stat().st_mtime_ns == before
    assert (tmp_path / "events").read_text().splitlines() == ["source|source|source", "test", "test"]


def test_continued_multitarget_literal_rules_and_comments(tmp_path):
    write(tmp_path / "gov", "#!/bin/sh\n[ ! -f fail-gov ]\n", 0o755)
    text = ".PHONY: test core second\ntest: core \\\n second # both branches\ncore second:\n\t@printf 'product\\n' >> events\n"
    installed = make_runner(text, {"targets": ["test"]})
    write(tmp_path / "Makefile", installed)
    write(tmp_path / "fail-gov", "rejection\n")
    assert invocation(tmp_path, "-j4", "test").returncode != 0
    assert not (tmp_path / "events").exists()
    assert make_runner(installed, {"targets": ["test"]}) == installed


@pytest.mark.parametrize("text,runner,diagnostic", [
    ("test: hidden\n", {"targets": ["test"]}, "explicit literal"),
    ("test: $(CHECKS)\n", {"targets": ["test"]}, "build_prerequisites"),
    ("test::\n\t@true\n", {"targets": ["test"]}, "double-colon"),
    ("test &: prerequisite\n\t@true\n", {"targets": ["test"]}, "grouped-target"),
    ("test: %.o: %.c\n\t@true\n", {"targets": ["test"]}, "static-pattern"),
    ("test: core\ncore: test\n", {"targets": ["test"]}, "cycle"),
    ("ifeq (a,a)\ntest:\n\t@true\nendif\n", {"targets": ["test"]}, "conditional"),
    ("$(eval test: hidden)\ntest:\n", {"targets": ["test"]}, "eval"),
    (".SECONDEXPANSION:\ntest:\n", {"targets": ["test"]}, "secondary"),
    (".RECIPEPREFIX = >\ntest:\n>@true\n", {"targets": ["test"]}, "recipe prefix"),
    ("include other.mk\ntest:\n", {"targets": ["test"]}, "include"),
    ("-include $(DEPS)\ntest:\n", {"targets": ["test"]}, "build_includes"),
    ("-include extra.mk\ntest: build\nbuild:\n", {"targets": ["test"], "build_prerequisites": ["build"], "build_includes": ["$(DEPS)"]}, "build_includes"),
    ("test:\n", {"targets": ["test"], "build_prerequisites": ["test"]}, "root"),
    ("test:\n", {"targets": ["test"], "build_prerequisites": ["unused"]}, "unused"),
    ("test: build\nbuild:\n", {"targets": ["test"], "build_prerequisites": ["build", "build"]}, "duplicate"),
    ("test:\n", {"targets": ["test"], "build_includes": ["unused.mk"]}, "unused"),
])
def test_unsupported_or_unreviewed_graphs_fail_closed(text, runner, diagnostic):
    with pytest.raises(fleet.FleetError, match=diagnostic):
        make_runner(text, runner)


def test_reviewed_build_boundary_and_exact_optional_include_are_supported(tmp_path):
    write(tmp_path / "gov", "#!/bin/sh\n[ ! -f fail-gov ]\n", 0o755)
    text = (
        "BUILD = artifact\nDEPS = missing.d\n.PHONY: test check all\n"
        "test: check\ncheck: all $(BUILD)/test\n\t@printf product >> events\n"
        "all: $(BUILD)\n$(BUILD)/test:\n\t@true\n"
        "$(BUILD):\n\t@true\n-include $(DEPS)\n"
    )
    runner = {"targets": ["test"], "build_prerequisites": ["all", "$(BUILD)/test"], "build_includes": ["$(DEPS)"], "build_rules": ["$(BUILD)", "$(BUILD)/test"]}
    installed = make_runner(text, runner)
    assert "check test: | workspace-governance-check" in installed
    write(tmp_path / "Makefile", installed)
    write(tmp_path / "fail-gov", "rejection\n")
    assert invocation(tmp_path, "-j4", "test").returncode != 0
    assert not (tmp_path / "events").exists()


def test_gate_ancestry_exclusion_cannot_hide_a_declared_product_root(tmp_path):
    text = make_runner("test:\n", {"targets": ["test"]})
    text += "\nworkspace-governance-check: test\n"
    with pytest.raises(fleet.FleetError, match="gate.*root"):
        make_runner(text, {"targets": ["test"]})


def test_guarded_make_plan_apply_idempotence_and_undo_preserve_owner_bytes(setup):
    root, registry, release = setup
    original = (
        "# Project owner text\n.PHONY: test test-core\n"
        "test: test-core\n\t@printf top > native-ran\n"
        "test-core:\n\t@printf core > make-product-ran\n"
    )
    write(root / "Makefile", original)
    value = fleet.load(registry)
    value["checkouts"][0]["verification"] = {
        "argv": ["make", "-j4", "test"],
        "runner": {"kind": "make", "path": "Makefile", "targets": ["test"]},
    }
    registry.write_bytes(fleet.canonical(value))
    receipt, _ = apply_fixture(setup)
    installed = (root / "Makefile").read_text()
    assert installed.startswith(original)
    assert "test test-core: | workspace-governance-check" in installed
    assert fleet.plan(registry, release)["checkouts"][0]["status"] == "unchanged"
    # Unrelated files are outside receipt restoration ownership.
    write(root / "owner-note.txt", "subsequent owner edit\n")
    assert fleet.undo(receipt)["checkouts"][0]["status"] == "undone"
    assert (root / "Makefile").read_text() == original
    assert (root / "owner-note.txt").read_text() == "subsequent owner edit\n"


def test_make_undo_refuses_to_overwrite_later_owner_runner_edit(setup):
    root, registry, release = setup
    write(root / "Makefile", "test:\n\t@true\n")
    value = fleet.load(registry)
    value["checkouts"][0]["verification"] = {
        "argv": ["make", "test"],
        "runner": {"kind": "make", "path": "Makefile", "targets": ["test"]},
    }
    registry.write_bytes(fleet.canonical(value))
    receipt, _ = apply_fixture(setup)
    with (root / "Makefile").open("a") as stream:
        stream.write("# subsequent owner edit\n")
    assert fleet.undo(receipt)["checkouts"][0]["status"] == "undo_refused"
    assert (root / "Makefile").read_text().endswith("# subsequent owner edit\n")


@pytest.mark.parametrize("recipe", ["\t@true\n", "; @true\n"])
def test_project_owned_gate_recipe_is_rejected(recipe):
    text = make_runner("test:\n\t@true\n", {"targets": ["test"]})
    text += "\nworkspace-governance-check:" + ("\n" if recipe.startswith("\t") else "") + recipe
    with pytest.raises(fleet.FleetError, match="gate recipe"):
        make_runner(text, {"targets": ["test"]})


@pytest.mark.parametrize("expression", ["$(MORE_TESTS)", "%.o", "$(shell whoami)"])
def test_dynamic_build_rules_need_exact_owner_declaration(expression):
    text = f"test: build\nbuild:\n{expression}: hidden-check\n"
    with pytest.raises(fleet.FleetError, match="build_rules|effectful"):
        make_runner(text, {"targets": ["test"], "build_prerequisites": ["build"]})


@pytest.mark.parametrize("name,value", [
    ("build_prerequisites", "/private/build"),
    ("build_prerequisites", "../build"),
    ("build_includes", "/private/dependencies.d"),
    ("build_rules", "$(shell uname)"),
    ("build_rules", "$(filter /private%, $(OBJECTS))"),
    ("build_rules", "../%.o"),
])
def test_build_declarations_keep_portable_metadata_public_safe(name, value):
    with pytest.raises(fleet.FleetError, match="public-safe"):
        make_runner("test:\n", {"targets": ["test"], name: [value]})


def test_build_declarations_survive_portable_fleet_metadata(setup):
    root, registry, release = setup
    write(root / "Makefile", "test: build\n\t@true\nbuild: $(BUILD)\n$(BUILD):\n\t@true\n-include $(DEPS)\n")
    runner = {
        "kind": "make", "path": "Makefile", "targets": ["test"],
        "build_prerequisites": ["build"], "build_includes": ["$(DEPS)"],
        "build_rules": ["$(BUILD)"],
    }
    value = fleet.load(registry)
    value["checkouts"][0]["verification"] = {"argv": ["make", "test"], "runner": runner}
    registry.write_bytes(fleet.canonical(value))
    apply_fixture(setup)
    metadata = fleet.load(root / ".governance/fleet.json")
    assert metadata["verification"]["runner"] == runner


def test_product_recipe_cannot_also_be_a_gate_prerequisite():
    text = make_runner("test: core\ncore:\n\t@printf product\n", {"targets": ["test"]})
    text += "\nworkspace-governance-check: core\n"
    with pytest.raises(fleet.FleetError, match="product recipe overlaps"):
        make_runner(text, {"targets": ["test"]})


@pytest.mark.parametrize("expression", ["$(EXTRA)", "${EXTRA}"])
def test_standalone_dynamic_rule_expansion_is_rejected(expression):
    text = f"EXTRA = test: hidden\n{expression}\ntest: core\ncore:\n\t@true\nhidden:\n\t@printf product\n"
    with pytest.raises(fleet.FleetError, match="unsupported top-level"):
        make_runner(text, {"targets": ["test"]})


@pytest.mark.parametrize("directive", [
    "override .RECIPEPREFIX = >", "export .RECIPEPREFIX = >",
    "override export .RECIPEPREFIX = >", "private .RECIPEPREFIX = >",
])
def test_recipe_prefix_modifiers_are_rejected(directive):
    with pytest.raises(fleet.FleetError, match="recipe prefix"):
        make_runner(directive + "\ntest:\n", {"targets": ["test"]})


def test_escaped_alias_for_literal_verification_target_is_rejected():
    text = "te\\st: hidden\ntest: core\ncore:\n\t@true\nhidden:\n\t@printf product\n"
    with pytest.raises(fleet.FleetError, match="escaped target"):
        make_runner(text, {"targets": ["test"]})


@pytest.mark.parametrize("text,diagnostic", [
    ("test: a | b | c\na b c |:\n\t@printf product\n", "multiple order-only"),
    (".EXTRA_PREREQS = hidden\ntest:\nhidden:\n\t@printf product\n", "special variable"),
    ("override .EXTRA_PREREQS := hidden\ntest:\n", "special variable"),
    (".SHELLFLAGS = -c -i\ntest:\n", "special variable"),
    (".IGNORE:\ntest:\n\t@printf product\n", "IGNORE"),
    (".IGNORE: workspace-governance-check\ntest:\n", "IGNORE"),
])
def test_make_graph_transforms_that_can_bypass_gate_reject(text, diagnostic):
    with pytest.raises(fleet.FleetError, match=diagnostic):
        make_runner(text, {"targets": ["test"]})


@pytest.mark.parametrize("expression", ["$(call eval,test: hidden)", "${call eval,test: hidden}", "$(guile injected)"])
def test_assignment_function_indirection_cannot_inject_verification_rules(expression):
    text = f"INJECT := {expression}\ntest: core\ncore:\n\t@true\nhidden:\n\t@printf product\n"
    with pytest.raises(fleet.FleetError, match="eval/call/guile"):
        make_runner(text, {"targets": ["test"]})


@pytest.mark.parametrize("expression", ["$(shell printf injected)", "${shell printf injected}", "$(file >dependencies.d,test: hidden)"])
def test_top_level_effectful_build_facts_cannot_rewrite_reviewed_includes(expression):
    text = f"INJECT := {expression}\ntest: build\nbuild:\n-include dependencies.d\n"
    with pytest.raises(fleet.FleetError, match="effectful"):
        make_runner(text, {"targets": ["test"], "build_prerequisites": ["build"], "build_includes": ["dependencies.d"]})


@pytest.mark.parametrize("assignment", [
    "MAKEFLAGS = -i", "override MAKEFLAGS := -i", "export MFLAGS = -i",
    "override export MAKEFLAGS := -i", "SHELL = /usr/bin/true",
    "GNUMAKEFLAGS += -i", "MAKEFILES = injected.mk", "MAKEOVERRIDES = CC=true",
])
def test_consumer_make_control_assignments_cannot_ignore_or_replace_gate(assignment):
    text = assignment + "\ntest: core\ncore:\n\t@printf product\n"
    with pytest.raises(fleet.FleetError, match="control variable"):
        make_runner(text, {"targets": ["test"]})
