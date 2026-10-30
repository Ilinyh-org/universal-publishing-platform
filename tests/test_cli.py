"""CLI tests. All runs use `--demo` (seeded in-memory DB) or env-isolated
databases — no live infrastructure."""

from __future__ import annotations

import pytest

from publisher.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "publisher" in capsys.readouterr().out


def test_list_tasks_demo_shows_pending(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["list-tasks", "--demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "pending" in out
    assert "english_channel" in out
    assert "At the coffee shop" in out
    assert "showing 1 task(s)" in out


def test_list_pending_alias(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["list-pending", "--demo"])
    out = capsys.readouterr().out
    assert code == 0
    assert "At the coffee shop" in out


def test_status_filter_without_matches(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["list-tasks", "--demo", "--status", "published"])
    out = capsys.readouterr().out
    assert code == 0
    assert "showing 0 task(s)" in out


def test_status_all_shows_every_demo_task(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["list-tasks", "--demo", "--status", "all"])
    out = capsys.readouterr().out
    assert code == 0
    assert "retry_wait" in out
    assert "scheduled" in out
    assert "showing 3 task(s)" in out


def test_limit_one(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["list-tasks", "--demo", "--status", "all", "--limit", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "showing 1 task(s)" in out


def test_limit_must_be_positive() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["list-tasks", "--demo", "--limit", "0"])
    assert excinfo.value.code == 2


def test_uninitialized_database_reports_clean_error(
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/fresh.db")
    code = main(["list-tasks"])
    captured = capsys.readouterr()
    assert code == 2
    assert "not initialized" in captured.err
