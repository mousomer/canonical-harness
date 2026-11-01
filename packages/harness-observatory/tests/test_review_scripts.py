import importlib.util
from pathlib import Path


def load(name):
    path = Path(__file__).parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_github_slug_accepts_only_github_repositories():
    module = load("census_ci")
    assert module.github_slug("git@github.com:owner/repo.git") == "owner/repo"
    assert module.github_slug("https://github.com/owner/repo.git") == "owner/repo"
    assert module.github_slug("/private/repository") is None


def test_audit_limit_is_hard_bounded():
    module = load("audit_reconstruction")
    assert module.choose([], 20) == []


def test_github_slug_rejects_non_github_urls():
    module = load("census_ci")
    assert module.github_slug("https://example.com/owner/repo.git") is None


def source_probe(python_path):
    import os
    import subprocess
    import sys

    root = Path(__file__).parents[1]
    script = (root / "scripts" / "verify.sh").read_text()
    probe = script.split(" - <<'VERIFY_SOURCE'\n", 1)[1]
    probe = probe.split("\nVERIFY_SOURCE\n", 1)[0]
    return subprocess.run(
        [sys.executable, "-c", probe],
        cwd=root,
        env={**os.environ, "PYTHONPATH": python_path, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_source_verifier_accepts_checkout_and_selected_environment():
    result = source_probe(str(Path(__file__).parents[1] / "src"))
    assert result.returncode == 0
    assert result.stdout == "Source and verifier package origins passed.\n"


def test_source_verifier_rejects_foreign_application_origin(tmp_path):
    package = tmp_path / "harness_observatory"
    package.mkdir()
    (package / "__init__.py").write_text("")
    result = source_probe(str(tmp_path))
    assert result.returncode != 0
    assert result.stderr.strip() == "SOURCE_IMPORT_ORIGIN_INVALID:harness_observatory"


def test_source_verifier_rejects_foreign_dependency_origin(tmp_path):
    import os

    (tmp_path / "duckdb.py").write_text("")
    source = Path(__file__).parents[1] / "src"
    result = source_probe(os.pathsep.join((str(tmp_path), str(source))))
    assert result.returncode != 0
    assert result.stderr.strip() == "SOURCE_IMPORT_ORIGIN_INVALID:duckdb"
