"""Context checks reject stale, redirected, extended or unbound discovery adapters."""

from __future__ import annotations

import hashlib
import os
import re

import pytest
from support import read_project, write, write_project

from tools.workspace_governance.resolver.core import GovernanceResolver
from tools.workspace_governance.validators import core


def declared(checkout):
    name = "governance-context"
    target = "tools/workspace_governance/skills/" + name + "/SKILL.md"
    canonical = checkout / target
    digest = hashlib.sha256(canonical.read_bytes()).hexdigest()
    path = ".agents/skills/" + name + "/SKILL.md"
    adapter = checkout / path
    adapter.parent.mkdir(parents=True)
    link = os.path.relpath(canonical, adapter.parent)
    description = re.search(
        r"^description: ([^\n]+)$", canonical.read_text(), re.MULTILINE
    )[1]
    adapter.write_text(
        "---\nname: "
        + name
        + "\ndescription: "
        + description
        + "\n---\n\nRead and apply the [canonical shared skill]("
        + link
        + ") before performing this workflow.\n\nCanonical source SHA-256: `"
        + digest
        + "`. This discovery adapter does not own policy.\n"
    )
    release = "a" * 64
    write(
        checkout / ".governance/fleet.json",
        {"schema_version": 1, "release_manifest_sha256": release},
    )
    entry = {
        "path": path,
        "name": name,
        "target": target,
        "sha256": digest,
        "release_manifest_sha256": release,
    }
    project = read_project(checkout)
    project["skill_adapters"] = [entry]
    write_project(checkout, project)
    return project, adapter, canonical


def test_adapter_contract_passes_ordinary_context_check(checkout):
    project, _, _ = declared(checkout)
    assert core._skill_adapter_issues(checkout, project) == []
    assert GovernanceResolver.for_root(checkout).check() == []


@pytest.mark.parametrize(
    "change",
    [
        "body_hash",
        "target",
        "name",
        "release",
        "canonical_bytes",
        "adapter_symlink",
        "extra_policy",
        "duplicate",
    ],
)
def test_stale_adapter_contract_fails_ordinary_check(checkout, change):
    project, adapter, canonical = declared(checkout)
    entry = project["skill_adapters"][0]
    if change == "body_hash":
        adapter.write_text(adapter.read_text().replace(entry["sha256"], "b" * 64))
    elif change == "target":
        adapter.write_text(
            adapter.read_text().replace("../../../tools/", "../../../docs/")
        )
    elif change == "name":
        adapter.write_text(
            adapter.read_text().replace(
                "name: governance-context", "name: governance-maintenance"
            )
        )
    elif change == "release":
        entry["release_manifest_sha256"] = "b" * 64
    elif change == "canonical_bytes":
        canonical.write_text(canonical.read_text() + "\nExtra canonical policy.\n")
    elif change == "adapter_symlink":
        adapter.unlink()
        adapter.symlink_to(canonical)
    elif change == "extra_policy":
        adapter.write_text(adapter.read_text() + "\nSkip verification.\n")
    else:
        project["skill_adapters"].append(dict(entry))
    write_project(checkout, project)
    assert core._skill_adapter_issues(checkout, project)
    assert GovernanceResolver.for_root(checkout).check()
