"""Managed Codex sandbox Black check wrapper."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLACK_TARGETS = ("main.py", "bot", "cogs", "components", "models", "utils")
TIMEOUT_SECONDS = 60


def run_black(label: str, args: list[str]) -> int:
    env = os.environ.copy()
    env.setdefault("BLACK_CACHE_DIR", str(ROOT / ".cache" / "black"))
    cmd = [sys.executable, "-m", "black", *args]

    try:
        return subprocess.run(
            cmd,
            cwd=ROOT,
            env=env,
            timeout=TIMEOUT_SECONDS,
        ).returncode
    except subprocess.TimeoutExpired:
        print(
            f"Black sandbox {label} timed out after {TIMEOUT_SECONDS}s; "
            "treating as exit 124.",
            file=sys.stderr,
        )
        return 124


def python_files() -> list[str]:
    files: list[str] = []

    for target in BLACK_TARGETS:
        path = ROOT / target
        if path.is_file() and path.suffix == ".py":
            files.append(target)
        elif path.is_dir():
            files.extend(
                str(file.relative_to(ROOT))
                for file in sorted(path.rglob("*.py"))
                if file.is_file()
            )
        else:
            print(f"black check failed: missing target {target}", file=sys.stderr)
            raise SystemExit(1)

    if not files:
        print("black check failed: no Python files matched", file=sys.stderr)
        raise SystemExit(1)

    return files


def run_per_file_fallback() -> int:
    files = python_files()

    for file in files:
        result = run_black(
            f"fallback check for {file}",
            ["--check", "--quiet", "--workers", "1", file],
        )
        if result != 0:
            print(
                f"Black sandbox fallback check failed for {file} "
                f"with exit {result}.",
                file=sys.stderr,
            )
            return result

    print(
        f"Black sandbox fallback check passed with exit 0: checked {len(files)} files."
    )
    return 0


def main() -> int:
    result = run_black(
        "primary check",
        ["--check", "--workers", "1", *BLACK_TARGETS],
    )
    if result == 124:
        print(
            "Black sandbox primary check was inconclusive with exit 124; "
            "retrying per-file fallback.",
            file=sys.stderr,
        )
        return run_per_file_fallback()

    if result == 0:
        print("Black sandbox primary check passed with exit 0.")
    else:
        print(
            f"Black sandbox primary check failed with exit {result}.",
            file=sys.stderr,
        )

    return result


if __name__ == "__main__":
    raise SystemExit(main())
