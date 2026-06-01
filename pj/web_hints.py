from __future__ import annotations

"""Static hints for projects that look like web apps."""

import json
import re
from pathlib import Path
from typing import Any

SCRIPT_NAMES = {"dev", "start", "serve", "preview"}
SCRIPT_MARKERS = (
    "vite",
    "next",
    "astro",
    "svelte-kit",
    "sveltekit",
    "webpack",
    "parcel",
    "nuxt",
    "react-scripts",
    "http-server",
    "live-server",
)
PACKAGE_MARKERS = {
    "@sveltejs/kit",
    "astro",
    "fastify",
    "next",
    "nuxt",
    "react-scripts",
    "vite",
    "webpack",
}
CONFIG_GLOBS = (
    "vite.config.*",
    "next.config.*",
    "astro.config.*",
    "svelte.config.*",
)
PYTHON_WEB_PACKAGES = ("flask", "fastapi")
PYTHON_ENTRYPOINTS = ("app.py", "main.py")


def _read_text(path: Path, *, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def _package_json_evidence(path: Path) -> list[str]:
    package_path = path / "package.json"
    try:
        data = json.loads(_read_text(package_path))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []

    evidence: list[str] = []
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        for name, command in sorted(scripts.items()):
            if name not in SCRIPT_NAMES or not isinstance(command, str):
                continue
            lowered = command.lower()
            if any(marker in lowered for marker in SCRIPT_MARKERS):
                evidence.append(f"package.json:scripts.{name}={command}")

    packages: set[str] = set()
    for key in ("dependencies", "devDependencies"):
        deps = data.get(key)
        if isinstance(deps, dict):
            packages.update(str(name) for name in deps)
    for marker in sorted(PACKAGE_MARKERS & packages):
        evidence.append(f"package.json:dependency:{marker}")

    return evidence


def _config_evidence(path: Path) -> list[str]:
    evidence: list[str] = []
    for pattern in CONFIG_GLOBS:
        for match in sorted(path.glob(pattern)):
            if match.is_file():
                evidence.append(match.name)
    return evidence


def _python_import_evidence(path: Path) -> list[str]:
    evidence: list[str] = []
    for filename in PYTHON_ENTRYPOINTS:
        source_path = path / filename
        if not source_path.is_file():
            continue
        source = _read_text(source_path)
        for package in PYTHON_WEB_PACKAGES:
            import_re = re.compile(rf"^\s*(?:from\s+{package}\b|import\s+{package}\b)", re.MULTILINE)
            if import_re.search(source):
                evidence.append(f"{filename}:imports:{package}")
    return evidence


def _python_dependency_evidence(path: Path) -> list[str]:
    evidence: list[str] = []

    requirements = path / "requirements.txt"
    if requirements.is_file():
        lowered = _read_text(requirements).lower()
        for package in PYTHON_WEB_PACKAGES:
            if re.search(rf"(^|\n)\s*{package}\b", lowered):
                evidence.append(f"requirements.txt:{package}")

    pyproject = path / "pyproject.toml"
    if pyproject.is_file():
        lowered = _read_text(pyproject).lower()
        for package in PYTHON_WEB_PACKAGES:
            if re.search(rf"['\"]?{package}['\"]?\s*(?:[<>=~!]|[,\"\n])", lowered):
                evidence.append(f"pyproject.toml:{package}")

    return evidence


def _static_html_evidence(path: Path) -> list[str]:
    index = path / "index.html"
    return ["index.html"] if index.is_file() else []


def detect(path: str) -> dict[str, Any] | None:
    """Return a web-app hint when static project files provide evidence."""
    root = Path(path)
    if not root.is_dir():
        return None

    evidence = (
        _package_json_evidence(root)
        + _config_evidence(root)
        + _python_import_evidence(root)
        + _python_dependency_evidence(root)
        + _static_html_evidence(root)
    )
    if not evidence:
        return None

    confidence = "low" if evidence == ["index.html"] else "medium"
    return {
        "type": "web_app",
        "confidence": confidence,
        "evidence": evidence,
    }
