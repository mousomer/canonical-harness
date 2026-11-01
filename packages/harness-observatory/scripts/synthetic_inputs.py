"""Construct invented provider inputs at runtime; no stored transcript fixtures."""

import json
from pathlib import Path


def synthetic_bytes(provider):
    def stamp(second):
        return f"2026-01-01T10:00:{second:02d}Z"

    if provider == "codex":
        records = [
            dict(
                type="session_meta",
                timestamp=stamp(0),
                payload=dict(id="synthetic-codex", cwd="synthetic-repo"),
            ),
            dict(type="turn_context", timestamp=stamp(0), payload=dict(model="synthetic-model")),
            dict(
                type="response_item",
                timestamp=stamp(1),
                payload=dict(
                    type="message",
                    role="user",
                    content=[dict(type="input_text", text="Check the synthetic build")],
                ),
            ),
            dict(
                type="response_item",
                timestamp=stamp(2),
                payload=dict(
                    type="function_call",
                    name="exec_command",
                    call_id="call-1",
                    arguments=json.dumps(dict(cmd="gh run view 123"), separators=(",", ":")),
                ),
            ),
            dict(
                type="response_item",
                timestamp=stamp(3),
                payload=dict(
                    type="function_call_output", call_id="call-1", output="synthetic success"
                ),
            ),
            dict(
                type="response_item",
                timestamp=stamp(4),
                payload=dict(
                    type="message",
                    role="assistant",
                    content=[dict(type="output_text", text="Synthetic check finished")],
                ),
            ),
        ]
    elif provider == "claude":
        records = [
            dict(
                type="user",
                sessionId="synthetic-claude",
                cwd="synthetic-repo",
                timestamp=stamp(1),
                message=dict(role="user", content="Check the synthetic build"),
            ),
            dict(
                type="assistant",
                sessionId="synthetic-claude",
                timestamp=stamp(2),
                message=dict(
                    model="synthetic-model",
                    content=[
                        dict(
                            type="tool_use",
                            id="call-1",
                            name="Bash",
                            input=dict(command="gh run view 123"),
                        )
                    ],
                ),
            ),
            dict(
                type="user",
                sessionId="synthetic-claude",
                timestamp=stamp(3),
                message=dict(
                    role="user",
                    content=[
                        dict(type="tool_result", tool_use_id="call-1", content="synthetic success")
                    ],
                ),
            ),
            dict(
                type="assistant",
                sessionId="synthetic-claude",
                timestamp=stamp(4),
                message=dict(
                    model="synthetic-model",
                    content=[dict(type="text", text="Synthetic check finished")],
                ),
            ),
        ]
    else:
        raise ValueError("Unsupported synthetic provider")
    return (
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n"
    ).encode()


def write_synthetic_inputs(directory):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for provider in ("codex", "claude"):
        target = root / f"{provider}.jsonl"
        with target.open("xb") as stream:
            stream.write(synthetic_bytes(provider))
        target.chmod(0o600)
    return root
