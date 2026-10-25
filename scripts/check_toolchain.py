"""Fail before dependency installation when the selected toolchain is unsupported."""

import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    sys.exit("Unsupported Python. Run `mise install` and `mise run setup` with mise's Python.")

ROOT = Path(__file__).resolve().parents[1]


def check(root: Path = ROOT) -> list[str]:
    """Read the pin source and report all tool/version mismatches."""
    config = tomllib.loads((root / "mise.toml").read_text())
    expected: dict[str, str] = config["tools"]
    errors = []
    for tool in ("python", "node", "uv", "pnpm"):
        command = [sys.executable, "--version"] if tool == "python" else [tool, "--version"]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, check=True, timeout=15, cwd=root
            )
        except (OSError, subprocess.SubprocessError):
            errors.append(f"{tool}: cannot run --version; required {expected[tool]}")
            continue
        match = re.fullmatch(
            {
                "python": r"Python (\d+\.\d+\.\d+)",
                "node": r"v(\d+\.\d+\.\d+)",
                "uv": r"uv (\d+\.\d+\.\d+)(?: .*)?",
                "pnpm": r"(\d+\.\d+\.\d+)",
            }[tool],
            result.stdout.strip(),
        )
        actual = match[1] if match else "unrecognized version output"
        if actual != expected[tool]:
            errors.append(f"{tool}: required {expected[tool]}, found {actual}")

    package = json.loads((root / "package.json").read_text())
    if package.get("packageManager") != f"pnpm@{expected['pnpm']}":
        errors.append("package.json packageManager must match mise.toml pnpm")
    for tool in ("node", "pnpm"):
        if package.get("engines", {}).get(tool) != expected[tool]:
            errors.append(f"package.json engines.{tool} must match mise.toml")
    # This scalar is deliberately restricted to the exact version syntax, not a YAML parser.
    workspace = (root / "pnpm-workspace.yaml").read_text()
    node_version = re.search(r"^nodeVersion: (\d+\.\d+\.\d+)\s*$", workspace, re.MULTILINE)
    if node_version is None or node_version[1] != expected["node"]:
        errors.append("pnpm-workspace.yaml nodeVersion must match mise.toml node")
    return errors


def main() -> int:
    errors = check()
    if errors:
        print("Unsupported toolchain/configuration:\n- " + "\n- ".join(errors), file=sys.stderr)
        print("Run `mise install`, then `mise run setup`. Pins live in mise.toml.", file=sys.stderr)
        return 1
    print("Toolchain matches mise.toml (Python, Node.js, uv, pnpm).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
