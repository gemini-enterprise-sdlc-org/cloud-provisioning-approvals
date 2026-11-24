"""Unit tests for automatic Canvas, 4K Diagram, and UI-Staging GCS cleanup on TTL teardown."""

from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace
from typing import Any


def _load_teardown_module() -> Any:
    mod_path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "teardown_expired_workloads.py"
    spec = importlib.util.spec_from_file_location("teardown_expired_workloads", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cleanup_canvas_and_staging_artifacts_deletes_exact_slug_paths(monkeypatch: Any) -> None:
    mod = _load_teardown_module()
    calls: list[list[str]] = []

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/gcloud")

    def _fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(cmd)
        if "/diagrams/" in " ".join(cmd):
            return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: The following URLs matched no objects")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    cleaned = mod.cleanup_canvas_and_staging_artifacts(
        slug="keep-kanban-demo",
        project_id="test-proj",
        artifacts_bucket="test-artifacts-bucket",
    )
    assert cleaned == [
        "gs://test-artifacts-bucket/canvas/keep-kanban-demo.html",
        "gs://test-artifacts-bucket/ui-staging/keep-kanban-demo/",
    ]
    assert len(calls) == 3
    assert "--recursive" not in calls[0]
    assert "--recursive" not in calls[1]
    assert "--recursive" in calls[2]


def test_cleanup_canvas_and_staging_artifacts_rejects_unsafe_slugs(monkeypatch: Any) -> None:
    mod = _load_teardown_module()
    called = False

    def _fail_run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        nonlocal called
        called = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/gcloud")
    monkeypatch.setattr(mod.subprocess, "run", _fail_run)

    for bad_slug in ("", "*", "../other", "foo/bar", "UPPER_BAD", "-leading"):
        assert mod.cleanup_canvas_and_staging_artifacts(bad_slug, "test-proj", "bucket") == []
    assert called is False
