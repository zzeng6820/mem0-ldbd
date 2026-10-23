"""Check Git index (and optionally publishable working files), without printing secrets.

This supplements .gitignore and GitHub secret scanning. It cannot recognize every
possible secret. Install as a pre-commit hook for this local clone as documented.
"""
from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def env_values(content: str):
    for line in content.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match:
            name, value = match.groups()
            yield name, value.strip("\"'")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Also inspect non-ignored working files")
    args = parser.parse_args()
    root = Path(git("rev-parse", "--show-toplevel").decode().strip())
    known_values: set[bytes] = set()
    for path in root.glob(".env*"):
        if path.is_file() and path.name != ".env.example":
            for name, value in env_values(path.read_text(encoding="utf-8", errors="replace")):
                if re.search(r"key|secret|password|token", name, re.I) and len(value) >= 8:
                    known_values.add(value.encode())
    # Credentials embedded in remotes must never enter README, logs or exports.
    for remote in git("remote").decode().splitlines():
        result = subprocess.run(["git", "remote", "get-url", "--all", remote],
                                capture_output=True, check=False)
        for url in result.stdout.decode(errors="replace").splitlines():
            if url.startswith(("http://", "https://")):
                parsed = urlsplit(url)
                for value in (parsed.username, parsed.password):
                    if value and len(value) >= 16:
                        known_values.add(unquote(value).encode())

    patterns = [
        re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
        re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{20,}"),
        re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}"),
        re.compile(rb"\bm0-[A-Za-z0-9_-]{20,}"),
        re.compile(rb"https?://[^\s/@]+:[^\s/@]+@github\.com"),
    ]
    issues: set[tuple[str, str]] = set()
    checked = 0

    def inspect(name: str, data: bytes, source: str):
        nonlocal checked
        checked += 1
        path = PurePosixPath(name)
        base = path.name
        prohibited = (
            (base.startswith(".env") and base != ".env.example")
            or any(part in {".git", ".venv", "venv", "data", "logs", ".private"} for part in path.parts)
            or base.endswith((".pem", ".key", ".db", ".sqlite", ".sqlite3", ".tar", ".tar.gz", ".tgz", ".zip", ".bak", ".backup", ".private.md"))
            or base in {"id_rsa", "id_ed25519", "SHA256SUMS", "ldbd-mem0.service"}
        )
        if prohibited:
            issues.add((name, source + ": private/runtime file is selected for publication"))
        if len(data) > 5 * 1024 * 1024:
            issues.add((name, source + ": large file requires manual review"))
        if any(secret in data for secret in known_values):
            issues.add((name, source + ": matches a local configured credential"))
        if any(pattern.search(data) for pattern in patterns):
            issues.add((name, source + ": credential pattern found"))
        if base == ".env.example":
            for key, value in env_values(data.decode("utf-8", errors="replace")):
                if re.search(r"key|secret|password|token", key, re.I) and value:
                    issues.add((name, source + ": credential example must be empty"))

    # Inspect the actual staged blobs, even when the working copy was cleaned.
    for record in git("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        info, raw_name = record.split(b"\t", 1)
        mode, oid, stage = info.split()
        name = raw_name.decode("utf-8", errors="replace")
        if mode != b"160000":  # No submodule content is part of this index.
            inspect(name, git("cat-file", "blob", oid.decode()), "index")
    if args.all:
        for raw_name in set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")):
            if not raw_name:
                continue
            name = raw_name.decode("utf-8", errors="replace")
            path = root / name
            if path.is_symlink():
                issues.add((name, "working tree: symlink requires manual review"))
            elif path.is_file():
                inspect(name, path.read_bytes(), "working tree")
    for name, issue in sorted(issues):
        print(f"BLOCKED: {name}: {issue}", file=sys.stderr)
    if issues:
        print("Commit blocked. Remove sensitive files/values from the index and run again.", file=sys.stderr)
        return 1
    print(f"PASS: inspected {checked} file versions; no known credentials or prohibited files found.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (subprocess.CalledProcessError, OSError, ValueError):
        print("Secret check could not complete; commit blocked. Run from the repository root.", file=sys.stderr)
        raise SystemExit(2)
