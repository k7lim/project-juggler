from __future__ import annotations

import json

from pj import web_hints


def test_detects_package_json_script_and_config(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite --host 0.0.0.0"}, "devDependencies": {"vite": "^5"}}),
        encoding="utf-8",
    )
    (tmp_path / "vite.config.ts").write_text("export default {}", encoding="utf-8")

    hint = web_hints.detect(str(tmp_path))

    assert hint is not None
    assert hint["type"] == "web_app"
    assert hint["confidence"] == "medium"
    assert "package.json:scripts.dev=vite --host 0.0.0.0" in hint["evidence"]
    assert "package.json:dependency:vite" in hint["evidence"]
    assert "vite.config.ts" in hint["evidence"]


def test_detects_framework_config_without_package_json(tmp_path):
    (tmp_path / "next.config.mjs").write_text("export default {}", encoding="utf-8")

    hint = web_hints.detect(str(tmp_path))

    assert hint is not None
    assert hint["evidence"] == ["next.config.mjs"]


def test_detects_flask_and_fastapi_python_markers(tmp_path):
    (tmp_path / "app.py").write_text("from flask import Flask\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi==0.110.0\n", encoding="utf-8")

    hint = web_hints.detect(str(tmp_path))

    assert hint is not None
    assert "app.py:imports:flask" in hint["evidence"]
    assert "main.py:imports:fastapi" in hint["evidence"]
    assert "requirements.txt:fastapi" in hint["evidence"]


def test_detects_static_html_as_low_confidence_hint(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")

    hint = web_hints.detect(str(tmp_path))

    assert hint == {
        "type": "web_app",
        "confidence": "low",
        "evidence": ["index.html"],
    }


def test_ignores_projects_without_web_evidence(tmp_path):
    (tmp_path / "README.md").write_text("# notes", encoding="utf-8")

    assert web_hints.detect(str(tmp_path)) is None
    assert web_hints.detect(str(tmp_path / "missing")) is None
