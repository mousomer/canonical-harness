from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

DEFAULT_NON_CODE_SEGMENTS = {
    "docs",
    "doc",
    "assets",
    "inputs",
    "outputs",
    "config",
    "configs",
    "notebooks",
    "benchmarks",
    "packaging",
    "migrations",
    "schema",
    "scripts",
    "tools",
    "data",
}
DEFAULT_TEST_SEGMENTS = {"test", "tests"}

DEFAULT_STATUS_ORDER = {
    "balanced": 0,
    "watch": 1,
    "skewed": 2,
    "rebalance_signal": 3,
}

DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "code_default": {
        "py_files": {"target_band": [6, 15], "soft_band": [2, 28], "target_margin": 1},
        "avg_loc_per_file": {"target_band": [40, 220], "soft_band": [10, 420], "target_margin": 20},
        "py_loc_total": {"target_band": [250, 2500], "soft_band": [40, 8000], "target_margin": 250},
        "weights": {"py_files": 0.5, "avg_loc_per_file": 0.3, "py_loc_total": 0.2},
    },
    "tests_lenient": {
        "py_files": {"target_band": [2, 80], "soft_band": [1, 180], "target_margin": 8},
        "avg_loc_per_file": {"target_band": [10, 420], "soft_band": [1, 1000], "target_margin": 60},
        "py_loc_total": {"target_band": [80, 15000], "soft_band": [1, 50000], "target_margin": 1200},
        "weights": {"py_files": 0.5, "avg_loc_per_file": 0.3, "py_loc_total": 0.2},
    },
    "non_code_exempt": {
        "py_files": {"target_band": [0, 9999], "soft_band": [0, 99999], "target_margin": 0},
        "avg_loc_per_file": {"target_band": [0, 9999], "soft_band": [0, 99999], "target_margin": 0},
        "py_loc_total": {"target_band": [0, 99999], "soft_band": [0, 999999], "target_margin": 0},
        "weights": {"py_files": 0.34, "avg_loc_per_file": 0.33, "py_loc_total": 0.33},
    },
}

DEFAULT_CLASS_TO_PROFILE = {
    "code_default": "code_default",
    "tests_lenient": "tests_lenient",
    "non_code_exempt": "non_code_exempt",
}

DEFAULT_CLASS_GATE = {
    "code_default": True,
    "tests_lenient": True,
    "non_code_exempt": False,
}


def _round2(value: float) -> float:
    return round(float(value), 2)


def _clamp01(value: float) -> float:
    if value <= 0:
        return 0.0
    if value >= 1:
        return 1.0
    return value


def fuzzy_band_score(
    *,
    value: float,
    target_min: float,
    target_max: float,
    soft_min: float,
    soft_max: float,
    target_margin: float = 0.0,
) -> float:
    if soft_min > target_min or target_max > soft_max:
        raise ValueError("Invalid fuzzy band bounds")
    if target_margin < 0:
        raise ValueError("target_margin must be >= 0")
    if soft_min == target_min == target_max == soft_max:
        return 1.0
    plateau_min = max(soft_min, target_min - target_margin)
    plateau_max = min(soft_max, target_max + target_margin)
    if plateau_min <= value <= plateau_max:
        return 1.0
    if value < plateau_min:
        if value <= soft_min or plateau_min == soft_min:
            return 0.0
        return _clamp01((value - soft_min) / (plateau_min - soft_min))
    if value >= soft_max or plateau_max == soft_max:
        return 0.0
    return _clamp01((soft_max - value) / (soft_max - plateau_max))


def fuzzy_weighted_status(score: float) -> str:
    if score >= 0.85:
        return "balanced"
    if score >= 0.65:
        return "watch"
    if score >= 0.4:
        return "skewed"
    return "rebalance_signal"


def classify_folder_path(folder_path: str, config: dict[str, Any]) -> str:
    parts = [part.lower() for part in folder_path.split("/") if part]
    class_overrides = config.get("class_overrides", {})
    override = class_overrides.get(folder_path)
    if isinstance(override, str) and override:
        return override

    non_code_segments = set(config.get("non_code_segments") or DEFAULT_NON_CODE_SEGMENTS)
    if any(part in non_code_segments for part in parts):
        return "non_code_exempt"

    test_segments = set(config.get("test_segments") or DEFAULT_TEST_SEGMENTS)
    if any(part in test_segments for part in parts):
        return "tests_lenient"

    return "code_default"


def _config_dict(cfg: dict[str, Any] | None) -> dict[str, Any]:
    return cfg if isinstance(cfg, dict) else {}


def _profile_value(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    merged = dict(DEFAULT_PROFILES[name])
    raw = cfg.get("profiles", {})
    if isinstance(raw, dict):
        item = raw.get(name)
        if isinstance(item, dict):
            merged.update(item)
    return merged


def _status_order(cfg: dict[str, Any]) -> dict[str, int]:
    raw = cfg.get("status_order")
    if not isinstance(raw, dict):
        return dict(DEFAULT_STATUS_ORDER)
    out: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, int):
            out[key] = value
    return out or dict(DEFAULT_STATUS_ORDER)


def discover_python_files(repo_root: Path, roots: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for rel_root in roots:
        root = (repo_root / rel_root).resolve()
        if not root.exists() or not root.is_dir():
            continue
        out.extend(
            p
            for p in sorted(root.rglob("*.py"))
            if ".git" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts
        )
    return sorted(set(out))


def _python_loc(path: Path) -> int:
    count = 0
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


def folder_rows_from_files(repo_root: Path, paths: list[Path]) -> list[dict[str, Any]]:
    folder_map: dict[str, dict[str, Any]] = {}
    python_folders: set[str] = set()

    for path in paths:
        rel_folder = path.parent.relative_to(repo_root).as_posix()
        python_folders.add(rel_folder)
        loc = _python_loc(path)
        row = folder_map.setdefault(rel_folder, {"path": rel_folder, "py_files": 0, "py_loc_total": 0})
        row["py_files"] = int(row["py_files"]) + 1
        row["py_loc_total"] = int(row["py_loc_total"]) + loc

    sorted_paths = sorted(python_folders)
    leaf_folders = {
        folder
        for folder in sorted_paths
        if not any(other.startswith(f"{folder}/") for other in sorted_paths if other != folder)
    }

    rows: list[dict[str, Any]] = []
    for path in sorted_paths:
        row = dict(folder_map[path])
        row["leaf_folder"] = path in leaf_folders
        rows.append(row)
    return rows


def compute_folder_balance(
    metrics_cfg: dict[str, Any],
    folders: list[dict[str, Any]],
    *,
    tracked_leaf_folders: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cfg = _config_dict(metrics_cfg)
    class_cfg = _config_dict(cfg.get("classification"))
    class_to_profile = dict(DEFAULT_CLASS_TO_PROFILE)
    raw_map = cfg.get("class_to_profile")
    if isinstance(raw_map, dict):
        class_to_profile.update({k: v for k, v in raw_map.items() if isinstance(k, str) and isinstance(v, str)})

    class_gate = dict(DEFAULT_CLASS_GATE)
    raw_gate = cfg.get("class_gate_eligibility")
    if isinstance(raw_gate, dict):
        for key, value in raw_gate.items():
            if isinstance(key, str) and isinstance(value, bool):
                class_gate[key] = value

    gate_overrides = class_cfg.get("gate_overrides", {})
    if not isinstance(gate_overrides, dict):
        gate_overrides = {}

    tracked_index: dict[str, dict[str, Any]] = {}
    for tracked in tracked_leaf_folders or []:
        if isinstance(tracked, dict) and isinstance(tracked.get("path"), str):
            tracked_index[tracked["path"]] = tracked

    status_counts: dict[str, int] = {}
    leaf_status_counts: dict[str, int] = {}
    leaf_fuzzy_counts: dict[str, int] = {}
    class_counts: dict[str, int] = {}

    rows: list[dict[str, Any]] = []
    for raw in sorted(folders, key=lambda item: str(item.get("path", ""))):
        row = dict(raw)
        path = str(row.get("path", ""))
        py_files = int(row.get("py_files", 0))
        py_loc_total = int(row.get("py_loc_total", 0))
        avg_loc = _round2(py_loc_total / py_files) if py_files else 0.0
        leaf = bool(row.get("leaf_folder"))

        folder_class = classify_folder_path(path, class_cfg)
        class_counts[folder_class] = class_counts.get(folder_class, 0) + 1

        profile_name = class_to_profile.get(folder_class, "code_default")
        profile = _profile_value(cfg, profile_name)

        py_files_cfg = profile["py_files"]
        loc_per_file_cfg = profile["avg_loc_per_file"]
        loc_total_cfg = profile["py_loc_total"]
        weights_cfg = profile["weights"]

        files_score = fuzzy_band_score(
            value=float(py_files),
            target_min=float(py_files_cfg["target_band"][0]),
            target_max=float(py_files_cfg["target_band"][1]),
            soft_min=float(py_files_cfg["soft_band"][0]),
            soft_max=float(py_files_cfg["soft_band"][1]),
            target_margin=float(py_files_cfg.get("target_margin", 0)),
        )
        loc_per_file_score = fuzzy_band_score(
            value=float(avg_loc),
            target_min=float(loc_per_file_cfg["target_band"][0]),
            target_max=float(loc_per_file_cfg["target_band"][1]),
            soft_min=float(loc_per_file_cfg["soft_band"][0]),
            soft_max=float(loc_per_file_cfg["soft_band"][1]),
            target_margin=float(loc_per_file_cfg.get("target_margin", 0)),
        )
        loc_per_folder_score = fuzzy_band_score(
            value=float(py_loc_total),
            target_min=float(loc_total_cfg["target_band"][0]),
            target_max=float(loc_total_cfg["target_band"][1]),
            soft_min=float(loc_total_cfg["soft_band"][0]),
            soft_max=float(loc_total_cfg["soft_band"][1]),
            target_margin=float(loc_total_cfg.get("target_margin", 0)),
        )

        fuzzy_score = (
            files_score * float(weights_cfg["py_files"])
            + loc_per_file_score * float(weights_cfg["avg_loc_per_file"])
            + loc_per_folder_score * float(weights_cfg["py_loc_total"])
        )
        fuzzy_status = fuzzy_weighted_status(fuzzy_score)

        gate_eligible = bool(class_gate.get(folder_class, True))
        if path in gate_overrides and isinstance(gate_overrides[path], bool):
            gate_eligible = gate_overrides[path]

        tracked = tracked_index.get(path)
        if tracked and isinstance(tracked.get("allow_non_code"), bool) and tracked.get("allow_non_code"):
            gate_eligible = True

        row.update(
            {
                "avg_loc_per_file": avg_loc,
                "folder_class": folder_class,
                "folder_profile": profile_name,
                "gate_eligible": gate_eligible,
                "exclude_from_code_balance": not gate_eligible,
                "gate_candidate": bool(leaf and path in tracked_index),
                "balancer": {
                    "fuzzy_weighted_score": _round2(fuzzy_score),
                    "fuzzy_weighted_status": fuzzy_status,
                    "weights": {
                        "py_files": float(weights_cfg["py_files"]),
                        "avg_loc_per_file": float(weights_cfg["avg_loc_per_file"]),
                        "py_loc_total": float(weights_cfg["py_loc_total"]),
                    },
                    "fuzzy_components": {
                        "py_files": {
                            "value": py_files,
                            "score": _round2(files_score),
                            "target_band": list(py_files_cfg["target_band"]),
                            "soft_band": list(py_files_cfg["soft_band"]),
                            "target_margin": py_files_cfg.get("target_margin", 0),
                        },
                        "avg_loc_per_file": {
                            "value": avg_loc,
                            "score": _round2(loc_per_file_score),
                            "target_band": list(loc_per_file_cfg["target_band"]),
                            "soft_band": list(loc_per_file_cfg["soft_band"]),
                            "target_margin": loc_per_file_cfg.get("target_margin", 0),
                        },
                        "py_loc_total": {
                            "value": py_loc_total,
                            "score": _round2(loc_per_folder_score),
                            "target_band": list(loc_total_cfg["target_band"]),
                            "soft_band": list(loc_total_cfg["soft_band"]),
                            "target_margin": loc_total_cfg.get("target_margin", 0),
                        },
                    },
                },
            }
        )
        rows.append(row)

        status_counts[fuzzy_status] = status_counts.get(fuzzy_status, 0) + 1
        if leaf:
            leaf_status_counts[fuzzy_status] = leaf_status_counts.get(fuzzy_status, 0) + 1
            if gate_eligible:
                leaf_fuzzy_counts[fuzzy_status] = leaf_fuzzy_counts.get(fuzzy_status, 0) + 1

    leaf_rows = [row for row in rows if bool(row.get("leaf_folder"))]
    eligible_leaf_rows = [row for row in leaf_rows if bool(row.get("gate_eligible"))]

    def _avg_score(items: list[dict[str, Any]]) -> float:
        if not items:
            return 0.0
        return _round2(sum(float(item["balancer"]["fuzzy_weighted_score"]) for item in items) / len(items))

    tracked_records: list[dict[str, Any]] = []
    for path in sorted(tracked_index):
        tracked = tracked_index[path]
        row = next((item for item in rows if item.get("path") == path), None)
        tracked_records.append(
            {
                "path": path,
                "configured_profile": tracked.get("profile"),
                "baseline_score": tracked.get("baseline_score"),
                "baseline_status": tracked.get("baseline_status"),
                "allow_non_code": bool(tracked.get("allow_non_code", False)),
                "found": row is not None,
                "leaf_folder": bool(row.get("leaf_folder")) if row else False,
                "folder_profile": row.get("folder_profile") if row else None,
                "folder_class": row.get("folder_class") if row else None,
                "gate_eligible": bool(row.get("gate_eligible")) if row else False,
                "current_score": row.get("balancer", {}).get("fuzzy_weighted_score") if row else None,
                "current_status": row.get("balancer", {}).get("fuzzy_weighted_status") if row else None,
            }
        )

    rebalance_priority = [
        {
            "path": row["path"],
            "folder_class": row["folder_class"],
            "py_files": row["py_files"],
            "py_loc_total": row["py_loc_total"],
            "avg_loc_per_file": row["avg_loc_per_file"],
            "fuzzy_weighted_score": row["balancer"]["fuzzy_weighted_score"],
            "fuzzy_weighted_status": row["balancer"]["fuzzy_weighted_status"],
        }
        for row in sorted(
            [item for item in eligible_leaf_rows],
            key=lambda item: (float(item["balancer"]["fuzzy_weighted_score"]), -int(item["py_files"])),
        )[:10]
    ]

    summary = {
        "python_folder_count": len(rows),
        "leaf_python_folder_count": len(leaf_rows),
        "gate_eligible_leaf_folder_count": len(eligible_leaf_rows),
        "python_file_count": sum(int(row["py_files"]) for row in rows),
        "leaf_python_file_count": sum(int(row["py_files"]) for row in leaf_rows),
        "gate_eligible_leaf_python_file_count": sum(int(row["py_files"]) for row in eligible_leaf_rows),
        "python_loc_total": sum(int(row["py_loc_total"]) for row in rows),
        "leaf_python_loc_total": sum(int(row["py_loc_total"]) for row in leaf_rows),
        "gate_eligible_leaf_python_loc_total": sum(int(row["py_loc_total"]) for row in eligible_leaf_rows),
        "leaf_fuzzy_weighted_balance_score_avg": _avg_score(leaf_rows),
        "gate_eligible_leaf_fuzzy_weighted_balance_score_avg": _avg_score(eligible_leaf_rows),
        "folder_class_counts": class_counts,
    }

    return {
        "heuristic": {
            "profiles": {
                key: _profile_value(cfg, key)
                for key in {"code_default", "tests_lenient", "non_code_exempt"}
            },
            "classification": {
                "non_code_segments": sorted(set(class_cfg.get("non_code_segments") or DEFAULT_NON_CODE_SEGMENTS)),
                "test_segments": sorted(set(class_cfg.get("test_segments") or DEFAULT_TEST_SEGMENTS)),
                "class_overrides": class_cfg.get("class_overrides", {}),
                "gate_overrides": class_cfg.get("gate_overrides", {}),
            },
            "status_order": _status_order(cfg),
        },
        "summary": summary,
        "balancer": {
            "fuzzy_weighted_status_counts_all_leaf_folders": leaf_status_counts,
            "fuzzy_weighted_status_counts_gate_eligible_leaf_folders": leaf_fuzzy_counts,
            "rebalance_priority_folders": rebalance_priority,
            "status_counts_all_folders": status_counts,
        },
        "gate": {
            "mode": cfg.get("gate_mode"),
            "scope": cfg.get("scope"),
            "tracked_folders": tracked_records,
        },
        "folders": rows,
    }


def parse_backlog_rows(backlog_text: str, mode: str, default_priority: str = "P2") -> list[tuple[str, str, str]]:
    if mode == "tet4d_bkl":
        section_match = re.search(
            r"^## 3\\. Active Open Backlog / TODO.*?(?=^## |\\Z)",
            backlog_text,
            flags=re.MULTILINE | re.DOTALL,
        )
        if section_match is None:
            return []
        section = section_match.group(0)
        item_rx = re.compile(
            r"^\\d+\\.\\s+`?\\[(P[0-3])\\]\\[(BKL-[A-Z0-9-]+)\\]\\s+(.+?)`?\\s*$",
            flags=re.MULTILINE,
        )
        return [(m.group(1), m.group(2), m.group(3)) for m in item_rx.finditer(section)]

    if mode in {"markdown_table", "markdown_table_section_priority"}:
        rows: list[tuple[str, str, str]] = []
        section_priority = default_priority
        open_status = {"todo", "in_progress", "blocked", "open"}
        for raw in backlog_text.splitlines():
            line = raw.strip()
            if mode == "markdown_table_section_priority":
                sec = re.match(r"^##\\s+P([0-3])\\b", line, flags=re.IGNORECASE)
                if sec:
                    section_priority = f"P{sec.group(1)}"

            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) < 3:
                continue
            issue_id = cells[0]
            title = cells[1]
            status = cells[2].lower()
            if not re.match(r"^(BL|BKL)-", issue_id):
                continue
            if status not in open_status:
                continue

            priority = section_priority
            explicit = re.search(r"\[(P[0-3])\]", issue_id)
            if explicit:
                priority = explicit.group(1)
            rows.append((priority, issue_id, title))
        return rows

    return []


def compute_tech_debt(
    *,
    arch_stage: int,
    tech_debt_cfg: dict[str, Any],
    folder_balance: dict[str, Any],
    backlog_rows: list[tuple[str, str, str]],
    budget_violations: list[str],
    folder_gate_violations: list[str],
) -> dict[str, Any]:
    scoring_cfg = tech_debt_cfg.get("scoring", {}) if isinstance(tech_debt_cfg, dict) else {}
    if not isinstance(scoring_cfg, dict):
        scoring_cfg = {}

    weight_cfg = scoring_cfg.get("component_weights", {})
    if not isinstance(weight_cfg, dict):
        weight_cfg = {}

    component_weights = {
        "backlog_priority": float(weight_cfg.get("backlog_priority", 0.35)),
        "backlog_bug": float(weight_cfg.get("backlog_bug", 0.2)),
        "ci_gate": float(weight_cfg.get("ci_gate", 0.25)),
        "code_balance": float(weight_cfg.get("code_balance", 0.2)),
    }
    weight_total = sum(component_weights.values())
    if weight_total <= 0:
        raise ValueError("tech debt component weights must sum to > 0")
    component_weights = {k: v / weight_total for k, v in component_weights.items()}

    priority_weights = scoring_cfg.get("priority_weights", {})
    if not isinstance(priority_weights, dict):
        priority_weights = {}
    p_weights = {
        "P0": float(priority_weights.get("P0", priority_weights.get("P1", 5))),
        "P1": float(priority_weights.get("P1", 5)),
        "P2": float(priority_weights.get("P2", 3)),
        "P3": float(priority_weights.get("P3", 1)),
    }

    norm_cfg = scoring_cfg.get("normalization", {})
    if not isinstance(norm_cfg, dict):
        norm_cfg = {}
    priority_cap = float(norm_cfg.get("priority_points_cap", 40))
    bug_cap = float(norm_cfg.get("bug_items_cap", 6))
    ci_cap = float(norm_cfg.get("ci_issue_cap", 6))
    if priority_cap <= 0 or bug_cap <= 0 or ci_cap <= 0:
        raise ValueError("normalization caps must be > 0")

    bug_keywords = [
        token.strip().lower()
        for token in (scoring_cfg.get("bug_keywords", []) if isinstance(scoring_cfg.get("bug_keywords", []), list) else [])
        if isinstance(token, str) and token.strip()
    ]

    priority_counts: dict[str, int] = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    weighted_points = 0.0
    bug_rows: list[dict[str, str]] = []
    for priority, issue_id, title in backlog_rows:
        normalized = priority if priority in priority_counts else "P2"
        priority_counts[normalized] = priority_counts.get(normalized, 0) + 1
        weighted_points += p_weights.get(normalized, p_weights["P2"])
        lowered = title.lower()
        if any(keyword in lowered for keyword in bug_keywords):
            bug_rows.append({"priority": normalized, "id": issue_id, "title": title})

    ci_issue_count = len(budget_violations) + len(folder_gate_violations)

    summary = folder_balance.get("summary", {}) if isinstance(folder_balance, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    leaf_balance_score = float(
        summary.get(
            "gate_eligible_leaf_fuzzy_weighted_balance_score_avg",
            summary.get("leaf_fuzzy_weighted_balance_score_avg", 0.0),
        )
    )

    backlog_priority_pressure = _clamp01(weighted_points / priority_cap)
    backlog_bug_pressure = _clamp01(len(bug_rows) / bug_cap)
    ci_gate_pressure = _clamp01(ci_issue_count / ci_cap)
    code_balance_pressure = _clamp01(1.0 - leaf_balance_score)

    overall = (
        component_weights["backlog_priority"] * backlog_priority_pressure
        + component_weights["backlog_bug"] * backlog_bug_pressure
        + component_weights["ci_gate"] * ci_gate_pressure
        + component_weights["code_balance"] * code_balance_pressure
    )
    score = _round2(overall * 100)

    if score <= 25:
        status = "low"
    elif score <= 50:
        status = "moderate"
    elif score <= 75:
        status = "high"
    else:
        status = "critical"

    return {
        "score": score,
        "status": status,
        "direction": "down",
        "formula": "weighted_pressure_x100",
        "components": {
            "backlog_priority": {
                "weight": _round2(component_weights["backlog_priority"]),
                "pressure": _round2(backlog_priority_pressure),
                "weighted_open_points": _round2(weighted_points),
                "priority_weights": p_weights,
                "open_counts": priority_counts,
                "open_issue_count": len(backlog_rows),
                "normalization_cap": priority_cap,
            },
            "backlog_bug": {
                "weight": _round2(component_weights["backlog_bug"]),
                "pressure": _round2(backlog_bug_pressure),
                "bug_issue_count": len(bug_rows),
                "keywords": bug_keywords,
                "sample_issue_ids": [item["id"] for item in bug_rows[:10]],
                "normalization_cap": bug_cap,
            },
            "ci_gate": {
                "weight": _round2(component_weights["ci_gate"]),
                "pressure": _round2(ci_gate_pressure),
                "issue_count": ci_issue_count,
                "architecture_budget_overages": budget_violations,
                "folder_balance_gate_violations": folder_gate_violations,
                "normalization_cap": ci_cap,
            },
            "code_balance": {
                "weight": _round2(component_weights["code_balance"]),
                "pressure": _round2(code_balance_pressure),
                "gate_eligible_leaf_fuzzy_weighted_balance_score_avg": _round2(leaf_balance_score),
            },
        },
        "gate_context": {
            "mode": tech_debt_cfg.get("gate_mode") if isinstance(tech_debt_cfg, dict) else None,
            "score_epsilon": tech_debt_cfg.get("score_epsilon") if isinstance(tech_debt_cfg, dict) else None,
            "status_order": tech_debt_cfg.get("status_order") if isinstance(tech_debt_cfg, dict) else None,
            "baseline": tech_debt_cfg.get("baseline") if isinstance(tech_debt_cfg, dict) else None,
        },
    }
