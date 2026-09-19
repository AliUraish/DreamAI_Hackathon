"""Runs the repository's own checks (tests, typecheck, build) in a working copy."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import settings

DEFAULT_COMMANDS = {
    "package.json": [{"name": "tests", "cmd": "npm test --silent"}, {"name": "typecheck", "cmd": "npx tsc --noEmit"}],
    "pyproject.toml": [{"name": "tests", "cmd": "python -m pytest -q"}],
}


def detect_commands(repo_root: Path) -> list[dict[str, str]]:
    package_json = repo_root / "package.json"
    if package_json.exists():
        import json
        scripts = json.loads(package_json.read_text()).get("scripts", {})
        commands = []
        if "test" in scripts:
            verbose = " -- --reporter=verbose" if "vitest" in scripts["test"] else ""
            commands.append({"name": "tests", "cmd": f"npm test --silent{verbose}"})
        if "typecheck" in scripts:
            commands.append({"name": "typecheck", "cmd": "npm run typecheck --silent"})
        elif (repo_root / "tsconfig.json").exists():
            commands.append({"name": "typecheck", "cmd": "npx tsc --noEmit"})
        if "build" in scripts:
            commands.append({"name": "build", "cmd": "npm run build --silent"})
        return commands
    for marker, commands in DEFAULT_COMMANDS.items():
        if (repo_root / marker).exists():
            return commands
    return []


def _summarize(name: str, output: str) -> str | None:
    if name == "tests":
        vitest = re.search(r"^\s*Tests\s+(.*)$", output, re.M)
        if vitest:
            failed = re.search(r"(\d+) failed", vitest.group(1))
            passed = re.search(r"(\d+) passed", vitest.group(1))
            failed_n, passed_n = int(failed.group(1)) if failed else 0, int(passed.group(1)) if passed else 0
            return f"{passed_n}/{passed_n + failed_n} tests passed"
        pytest = re.search(r"(\d+) passed", output)
        if pytest:
            failed = re.search(r"(\d+) failed", output)
            total = int(pytest.group(1)) + int(failed.group(1) if failed else 0)
            return f"{pytest.group(1)}/{total} tests passed"
    return None


_TEST_LINE = re.compile(r"^\s*([✓×✗])\s+(\S+)\s+>\s+(.*?)(?:\s+\d+\s*ms)?\s*$")


def parse_tests(output: str) -> list[dict[str, Any]]:
    """Per-test results from vitest's verbose reporter: [{file, name, passed, detail}]."""
    tests: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = _TEST_LINE.match(line)
        if match:
            tests.append({"file": match.group(2), "name": match.group(3).split(" > ")[-1], "passed": match.group(1) == "✓", "detail": None})
        elif tests and line.strip().startswith("→") and tests[-1]["detail"] is None:
            tests[-1]["detail"] = line.strip()[1:].strip()
    return tests


def run_validation(workdir: Path, commands: list[dict[str, str]], extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    env = {**os.environ, "CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0", **(extra_env or {})}
    checks = []
    for command in commands:
        started = time.monotonic()
        try:
            proc = subprocess.run(command["cmd"], shell=True, cwd=workdir, env=env, capture_output=True, text=True,
                                  timeout=settings.validation_timeout_seconds)
            output, passed = (proc.stdout + proc.stderr), proc.returncode == 0
        except subprocess.TimeoutExpired as exc:
            output, passed = f"timed out after {exc.timeout}s", False
        checks.append({"name": command["name"], "cmd": command["cmd"], "passed": passed,
                       "summary": _summarize(command["name"], output) or ("passed" if passed else "failed"),
                       "duration_s": round(time.monotonic() - started, 1), "output": output[-8000:],
                       "tests": parse_tests(output) if command["name"] == "tests" else []})
    return {"passed": bool(checks) and all(c["passed"] for c in checks), "checks": checks}
