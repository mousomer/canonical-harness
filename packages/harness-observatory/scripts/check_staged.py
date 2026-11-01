"""Review staged paths and secret signatures without printing matched content."""

import re
import subprocess

PATTERNS = [
    rb"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b",
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\r?\n",
    rb"\bAKIA[A-Z0-9]{16}\b",
]


def main():
    names = (
        subprocess.check_output(
            ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"]
        )
        .decode()
        .split("\0")
    )
    checked = 0
    for name in filter(None, names):
        parts = name.split("/")
        if any(
            p in {"private", "raw", "logs", "generated", "warehouse", "credentials"}
            or p.startswith(".env")
            for p in parts
        ):
            raise ValueError("Sensitive staged path rejected")
        if name.endswith((".db", ".duckdb", ".sqlite", ".parquet", ".pem", ".key", ".log")) or (
            name.endswith(".jsonl")
        ):
            raise ValueError("Staged evidence or database rejected")
        content = subprocess.check_output(["git", "show", ":" + name])
        if b"\0" in content or any(re.search(p, content) for p in PATTERNS):
            raise ValueError("Binary or secret signature in staged content; review locally")
        checked += 1
    print(f"Staged content scan passed: {checked} text files; serialized input files excluded.")


if __name__ == "__main__":
    main()
