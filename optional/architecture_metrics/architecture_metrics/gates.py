from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_TECH_DEBT_STATUS_ORDER = {
    "low": 0,
    "moderate": 1,
    "high": 2,
    "critical": 3,
}

DEFAULT_FOLDER_STATUS_ORDER = {
    "balanced": 0,
    "watch": 1,
    "skewed": 2,
    "rebalance_signal": 3,
}


def evaluate_metric_budget_overages(metrics: dict[str, Any], budgets: dict[str, int] | None) -> list[str]:
    if not budgets:
        return []

    def _metric_int(metric_path: str) -> int:
        node: Any = metrics
        for part in metric_path.split("."):
            if not isinstance(node, dict):
                raise TypeError(f"{metric_path} does not resolve to a dict node")
            node = node.get(part)
        if not isinstance(node, int):
            raise TypeError(f"{metric_path} is not an int: {node!r}")
        return node

    violations: list[str] = []
    for path, budget in budgets.items():
        value = _metric_int(path)
        if value > int(budget):
            violations.append(f"{path}: {value} > budget {budget}")
    return violations


def _require_dict(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return value


def _require_list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be a JSON array")
    return value


def _folder_rows(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    folder_balance = _require_dict(metrics.get("folder_balance"), label="folder_balance")
    rows = _require_list(folder_balance.get("folders"), label="folder_balance.folders")
    return [_require_dict(row, label="folder_balance.folders[*]") for row in rows]


def folder_row_index(metrics: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("path")): row for row in _folder_rows(metrics) if isinstance(row.get("path"), str)}


def _tracked_leaf_folders(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    tracked = _require_list(cfg.get("tracked_leaf_folders", []), label="tracked_leaf_folders")
    return [_require_dict(item, label="tracked_leaf_folders[*]") for item in tracked]


def _status_order(cfg: dict[str, Any], *, default: dict[str, int]) -> dict[str, int]:
    raw = cfg.get("status_order", default)
    order = _require_dict(raw, label="status_order")
    out: dict[str, int] = {}
    for key, value in order.items():
        if isinstance(key, str) and isinstance(value, int):
            out[key] = value
    if not out:
        return dict(default)
    return out


def _status_regression_reason(*, current_status: Any, baseline_status: Any, order: dict[str, int]) -> str | None:
    if not isinstance(current_status, str):
        return "missing current fuzzy_weighted_status"
    if not isinstance(baseline_status, str):
        return "missing baseline_status"
    if current_status not in order:
        return f"unknown current status {current_status!r}"
    if baseline_status not in order:
        return f"unknown baseline status {baseline_status!r}"
    if order[current_status] > order[baseline_status]:
        return f"status worsened ({current_status} > {baseline_status})"
    return None


def _score_regression_reason(*, current_score_raw: Any, baseline_score_raw: Any, epsilon: float) -> str | None:
    try:
        current_score = float(current_score_raw)
    except (TypeError, ValueError):
        return "missing current fuzzy_weighted_score"
    try:
        baseline_score = float(baseline_score_raw)
    except (TypeError, ValueError):
        return "missing baseline_score"
    if current_score < (baseline_score - epsilon):
        return f"score regressed ({current_score:.2f} < {baseline_score:.2f} - epsilon {epsilon:.2f})"
    return None


def evaluate_folder_balance_gate(metrics: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    index = folder_row_index(metrics)
    epsilon = float(cfg.get("score_epsilon", 0.0))
    order = _status_order(cfg, default=DEFAULT_FOLDER_STATUS_ORDER)

    for tracked in _tracked_leaf_folders(cfg):
        path = tracked.get("path")
        if not isinstance(path, str) or not path:
            violations.append("tracked_leaf_folders entry has invalid path")
            continue

        row = index.get(path)
        if row is None:
            violations.append(f"folder_balance_gate[{path}] folder missing from folder_balance.folders")
            continue

        reasons: list[str] = []
        if not bool(row.get("leaf_folder")):
            reasons.append("folder is not a leaf_folder")

        expected_profile = tracked.get("profile")
        actual_profile = row.get("folder_profile")
        if expected_profile != actual_profile:
            reasons.append(f"profile mismatch ({actual_profile!r} != {expected_profile!r})")

        allow_non_code = bool(tracked.get("allow_non_code", False))
        gate_eligible = bool(row.get("gate_eligible", True))
        if not gate_eligible and not allow_non_code:
            reasons.append("folder is not gate_eligible (allow_non_code=false)")

        balancer = row.get("balancer")
        if not isinstance(balancer, dict):
            reasons.append("missing balancer object")
        else:
            status_reason = _status_regression_reason(
                current_status=balancer.get("fuzzy_weighted_status"),
                baseline_status=tracked.get("baseline_status"),
                order=order,
            )
            if status_reason:
                reasons.append(status_reason)

            tracked_epsilon = float(tracked.get("score_epsilon", epsilon))
            score_reason = _score_regression_reason(
                current_score_raw=balancer.get("fuzzy_weighted_score"),
                baseline_score_raw=tracked.get("baseline_score"),
                epsilon=tracked_epsilon,
            )
            if score_reason:
                reasons.append(score_reason)

        if reasons:
            violations.append(
                f"folder_balance_gate[{path}] baseline(score={tracked.get('baseline_score')}, "
                f"status={tracked.get('baseline_status')}) current(score={row.get('balancer', {}).get('fuzzy_weighted_score')}, "
                f"status={row.get('balancer', {}).get('fuzzy_weighted_status')}): " + "; ".join(reasons)
            )

    return violations


def refresh_folder_balance_budgets(metrics: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(cfg)
    index = folder_row_index(metrics)
    tracked = _tracked_leaf_folders(updated)
    for item in tracked:
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("tracked_leaf_folders entry has invalid path")
        row = index.get(path)
        if row is None:
            raise ValueError(f"tracked folder not found: {path}")
        if not bool(row.get("leaf_folder")):
            raise ValueError(f"tracked folder is not a leaf folder: {path}")
        allow_non_code = bool(item.get("allow_non_code", False))
        if not bool(row.get("gate_eligible", True)) and not allow_non_code:
            raise ValueError(f"tracked folder not gate_eligible: {path}")

        expected_profile = item.get("profile")
        actual_profile = row.get("folder_profile")
        if expected_profile != actual_profile:
            raise ValueError(f"tracked folder profile mismatch for {path}: {actual_profile!r} != {expected_profile!r}")

        balancer = _require_dict(row.get("balancer"), label=f"{path}.balancer")
        item["baseline_score"] = float(balancer["fuzzy_weighted_score"])
        item["baseline_status"] = str(balancer["fuzzy_weighted_status"])

    updated["tracked_leaf_folders"] = tracked
    return updated


def _current_tech_debt(metrics: dict[str, Any]) -> tuple[int | None, float | None, str | None]:
    stage_raw = metrics.get("arch_stage")
    stage = stage_raw if isinstance(stage_raw, int) else None
    tech_debt = metrics.get("tech_debt")
    if not isinstance(tech_debt, dict):
        return stage, None, None
    score_raw = tech_debt.get("score")
    status_raw = tech_debt.get("status")
    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        score = None
    status = status_raw if isinstance(status_raw, str) else None
    return stage, score, status


def _baseline_tech_debt(cfg: dict[str, Any]) -> tuple[int, float, str]:
    baseline = _require_dict(cfg.get("baseline"), label="baseline")
    stage_raw = baseline.get("arch_stage")
    score_raw = baseline.get("score")
    status = baseline.get("status")
    if not isinstance(stage_raw, int):
        raise TypeError("baseline.arch_stage must be an int")
    try:
        score = float(score_raw)
    except (TypeError, ValueError) as exc:
        raise TypeError("baseline.score must be numeric") from exc
    if not isinstance(status, str):
        raise TypeError("baseline.status must be a string")
    return stage_raw, score, status


def evaluate_tech_debt_gate(metrics: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    baseline_stage, baseline_score, baseline_status = _baseline_tech_debt(cfg)
    stage, score, status = _current_tech_debt(metrics)
    epsilon = float(cfg.get("score_epsilon", 0.0))
    order = _status_order(cfg, default=DEFAULT_TECH_DEBT_STATUS_ORDER)

    violations: list[str] = []
    if stage is None:
        violations.append("missing arch_stage")
    if score is None:
        violations.append("missing tech_debt.score")
    if status is None:
        violations.append("missing tech_debt.status")
    if violations:
        return violations

    assert stage is not None
    assert score is not None
    assert status is not None

    if stage < baseline_stage:
        return [f"arch_stage regressed ({stage} < baseline {baseline_stage})"]

    if status not in order:
        return [f"unknown current status {status!r}"]
    if baseline_status not in order:
        return [f"unknown baseline status {baseline_status!r}"]
    if order[status] > order[baseline_status]:
        return [f"status worsened ({status} > {baseline_status})"]

    if stage > baseline_stage:
        threshold = baseline_score - epsilon
        if score >= threshold:
            return [
                "score must strictly decrease for stage advance "
                f"({score:.2f} >= {baseline_score:.2f} - epsilon {epsilon:.2f})"
            ]
        return []

    ceiling = baseline_score + epsilon
    if score > ceiling:
        return [
            "score regressed within baseline stage "
            f"({score:.2f} > {baseline_score:.2f} + epsilon {epsilon:.2f})"
        ]
    return []


def refresh_tech_debt_budgets(metrics: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    stage, score, status = _current_tech_debt(metrics)
    if stage is None:
        raise ValueError("metrics missing arch_stage")
    if score is None or status is None:
        raise ValueError("metrics missing tech_debt score/status")

    updated = deepcopy(cfg)
    baseline = _require_dict(updated.get("baseline", {}), label="baseline")
    baseline["arch_stage"] = stage
    baseline["score"] = round(float(score), 2)
    baseline["status"] = status
    updated["baseline"] = baseline
    return updated
