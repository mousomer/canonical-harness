"""Pure shared execution lifecycle; no observation producer or interception."""

from __future__ import annotations

from dataclasses import dataclass

MODEL_ID = "execution-lifecycle-v2"
CAUSES = {"exit", "cancellation", "deadline"}


@dataclass
class ExecutionLifecycle:
    """Track one original execution independently of client connectivity."""

    mode: str
    sha: str | None = None
    connection: str = "connected"
    reconciliation_required: bool = False
    watching: bool = False
    lifecycle: str = "running"
    pending_cause: str | None = None
    exit_code: int | None = None
    cause: str | None = None

    @property
    def terminal(self) -> bool:
        return self.exit_code is not None

    @property
    def outcome(self) -> str | None:
        if not self.terminal:
            return None
        return {"succeeded": "success", "failed": "failure"}.get(
            self.lifecycle, self.lifecycle
        )

    def _connection(self, kind: str) -> str | None:
        if kind == "disconnect":
            self.connection = "disconnected"
            self.reconciliation_required = True
        elif kind == "reconnect":
            self.connection = "connected"
            self.reconciliation_required = True
        elif self.connection != "connected":
            return "reconciliation_while_disconnected"
        else:
            self.reconciliation_required = False
        return None

    def _finish(self, event: dict) -> str | None:
        confirmation = event.get("termination_confirmed", event["event"] == "finish")
        if confirmation is not True:
            return "unconfirmed_termination"
        if self.mode == "remote" and not self.watching:
            return "missing_completion_mechanism"
        code = event.get("exit_code")
        cause = event.get("cause", "exit" if event["event"] == "finish" else None)
        if type(code) is not int:
            return "missing_exit_status"
        if not isinstance(cause, str) or cause not in CAUSES:
            return "invalid_termination_cause"
        if self.terminal:
            if (self.exit_code, self.cause) != (code, cause):
                return "conflicting_terminal_result"
            return None
        self.exit_code, self.cause = code, cause
        self.pending_cause = None
        self.lifecycle = {
            "cancellation": "cancelled",
            "deadline": "deadline",
            "exit": "succeeded" if code == 0 else "failed",
        }[cause]
        return None

    def _client_operation(self, kind: str) -> str | None:
        if self.connection != "connected":
            return "client_disconnected"
        if self.reconciliation_required:
            return "reconciliation_required"
        if kind == "retrieve":
            return None if self.terminal else "result_not_terminal"
        if kind == "watch":
            if self.watching:
                return "duplicate_watcher"
            self.watching = True
        return None

    def _request(self, kind: str) -> str | None:
        if self.terminal:
            return "operation_already_terminal"
        self.pending_cause = {
            "timeout": "wait_timeout",
            "cancel": "cancellation",
            "cancel_requested": "cancellation",
            "deadline": "deadline",
            "deadline_elapsed": "deadline",
            "unsupported": "unsupported",
        }[kind]
        return None

    def apply(self, event: dict) -> str | None:
        """Return a fixed violation code, preserving state on invalid completion."""
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            return "invalid_event"
        if event.get("sha", self.sha) != self.sha:
            return "remote_identity_changed"
        kind = event.get("event")
        if kind in {"status_query", "log_tail", "redispatch"}:
            return "agent_polling_or_redispatch"
        if kind in {"disconnect", "reconnect", "reconcile"}:
            return self._connection(kind)
        if kind in {"finish", "terminated"}:
            return self._finish(event)
        if kind in {"wait", "watch", "retrieve"}:
            return self._client_operation(kind)
        if kind in {
            "timeout",
            "cancel",
            "cancel_requested",
            "deadline",
            "deadline_elapsed",
            "unsupported",
        }:
            return self._request(kind)
        if kind == "notification":
            self.watching = True
            return None
        return None if kind == "yield" else "unsupported_event"

    def as_dict(self) -> dict:
        return {
            "lifecycle": self.lifecycle,
            "connection": self.connection,
            "reconciliation_required": self.reconciliation_required,
            "pending_cause": self.pending_cause,
            "exit_code": self.exit_code,
            "cause": self.cause,
        }
