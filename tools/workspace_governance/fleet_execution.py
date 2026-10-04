"""Bounded execution evidence without retaining or printing consumer output."""

from __future__ import annotations

import hashlib
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from tools.workspace_governance.fleet_support import MAKE_ENVIRONMENT_VARIABLES

COLLECTOR_ID = "workspace-governance-fleet-verify-v1"


def safe_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in MAKE_ENVIRONMENT_VARIABLES:
        environment.pop(name, None)
    return environment


def make_environment(environment: dict[str, str]) -> dict[str, str]:
    return {
        name: environment[name]
        for name in MAKE_ENVIRONMENT_VARIABLES
        if name in environment
    }


def run_digest(
    argv: list[str], root: Path, environment: dict[str, str], timeout: float
) -> dict[str, Any]:
    """Drain both pipes in bounded chunks; kill only this process group on timeout."""
    started = time.monotonic()
    deadline = started + timeout
    streams = {
        name: {"hash": hashlib.sha256(), "bytes": 0} for name in ("stdout", "stderr")
    }
    timed_out = False
    process = subprocess.Popen(
        argv,
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    with selectors.DefaultSelector() as selector:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            for key, _ in selector.select(min(remaining, 0.25)):
                data = os.read(key.fileobj.fileno(), 64 * 1024)
                if data:
                    streams[key.data]["hash"].update(data)
                    streams[key.data]["bytes"] += len(data)
                else:
                    selector.unregister(key.fileobj)
    process.wait()
    process.stdout.close()
    process.stderr.close()
    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 6),
        **{f"{name}_bytes": info["bytes"] for name, info in streams.items()},
        **{
            f"{name}_sha256": info["hash"].hexdigest() for name, info in streams.items()
        },
    }
