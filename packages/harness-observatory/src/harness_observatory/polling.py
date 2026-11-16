"""NO_AUTONOMOUS_POLLING classification from the canonical kit's command policy.

The observatory keeps no second definition of polling. It loads the kit's
stdlib-only ``tools/workspace_governance/command_policy.py`` by path (or the
file named by ``HARNESS_COMMAND_POLICY``). When that file is unavailable the
classifier is reported missing, and callers record the coverage gap instead of
treating commands as compliant.
"""

import importlib.util
import os
import sys
from functools import lru_cache
from pathlib import Path

DEFAULT = Path(__file__).resolve().parents[4] / "tools/workspace_governance/command_policy.py"
MARKER = "NO_AUTONOMOUS_POLLING"


@lru_cache(maxsize=1)
def policy():
    path = Path(os.environ.get("HARNESS_COMMAND_POLICY") or DEFAULT)
    if not path.is_file():
        return None
    specification = importlib.util.spec_from_file_location("_kit_command_policy", path)
    if specification is None or specification.loader is None:
        return None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def classify(tool, command):
    """Shared decision for one shell command, or None when unclassifiable."""
    module = policy()
    if module is None or not isinstance(command, str):
        return None
    name = "Monitor" if tool == "Monitor" else "Bash"
    return module.evaluate({"tool": name, "command": command}).as_dict()


def blocked(body):
    """Whether a tool result reports a pre-execution no-polling denial."""
    if isinstance(body, list):
        body = " ".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in body
        )
    return isinstance(body, str) and MARKER in body
