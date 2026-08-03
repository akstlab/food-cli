"""`food update`.

This command pulls code and then runs it, so the tests that matter are the ones
where it must refuse: a diverged branch, a failed merge, and a stash that will
not come back cleanly. Nothing here touches a real repository - git is faked.
"""

from __future__ import annotations

import subprocess

import pytest

from food_cli.cli import app
from food_cli.commands import update as U
from tests.conftest import parse_out

OLD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NEW = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def done(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class FakeGit:
    """Answers git by first argument, recording what was asked."""

    def __init__(self, **overrides):
        self.calls: list[tuple[str, ...]] = []
        self.head = OLD
        self.dirty = False
        self.overrides = overrides

    def __call__(self, *args, cwd=None):
        self.calls.append(args)
        key = " ".join(args)
        for pattern, value in self.overrides.items():
            # Match on a word boundary, so overriding "merge" does not also
            # capture "merge-base" and silently send the test down another path.
            if key == pattern or key.startswith(pattern + " "):
                return value() if callable(value) else value

        if args[:2] == ("rev-parse", "--git-dir"):
            return done(".git")
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return done("main")
        if args[:2] == ("remote", "get-url"):
            return done("https://github.com/akstlab/food-cli.git")
        if args == ("rev-parse", "HEAD"):
            return done(self.head)
        if args == ("rev-parse", "origin/main"):
            return done(NEW)
        if args[0] == "fetch":
            return done()
        if args[0] == "merge-base":
            return done()                      # ancestor: fast-forward is fine
        if args[0] == "log":
            return done("bbbbbbb tweak something\n")
        if args[:2] == ("diff", "--name-only") and "--diff-filter=U" not in args:
            return done("food_cli/cli.py\nSKILL.md\n")
        if args[:2] == ("diff", "--name-only"):
            return done("")
        if args[0] == "status":
            return done("M food_cli/cli.py\n" if self.dirty else "")
        if args[0] == "merge":
            return done()
        if args[:2] == ("stash", "push"):
            return done()
        if args[:2] == ("stash", "pop"):
            return done()
        return done()

    def ran(self, *prefix) -> bool:
        return any(c[:len(prefix)] == prefix for c in self.calls)


@pytest.fixture()
def git(monkeypatch):
    fake = FakeGit()
    monkeypatch.setattr(U, "_git", fake)
    monkeypatch.setattr(U, "is_git_checkout", lambda root=None: True)
    # Never shell out to uv from a test.
    monkeypatch.setattr(U.subprocess, "run", lambda *a, **k: done())
    return fake


def test_refuses_when_not_a_git_checkout(runner, monkeypatch):
    monkeypatch.setattr(U, "is_git_checkout", lambda root=None: False)
    r = runner.invoke(app, ["update"])
    assert r.exit_code == 2
    assert parse_out(r)["status"] == "not_a_checkout"
    assert "Reinstall instead" in r.stderr


def test_up_to_date_does_nothing(runner, git):
    git.head = NEW
    data = parse_out(runner.invoke(app, ["update"]))
    assert data["status"] == "up_to_date"
    assert not git.ran("merge")
    assert not git.ran("stash", "push")


def test_updates_and_reports_the_range(runner, git):
    data = parse_out(runner.invoke(app, ["update"]))
    assert data["status"] == "updated"
    assert data["from"] == OLD[:12] and data["to"] == NEW[:12]
    assert data["commits"] == ["bbbbbbb tweak something"]
    assert git.ran("merge", "--ff-only", "origin/main")


def test_a_skill_change_is_called_out(runner, git):
    """The agent's contract changed; it should be told to re-read it."""
    r = runner.invoke(app, ["update"])
    assert parse_out(r)["skill_changed"] is True
    assert "re-read" in r.stderr


def test_only_ever_fast_forwards(runner, git, monkeypatch):
    """Local commits are the user's. Merging or rebasing is not our call."""
    monkeypatch.setattr(U, "_git", FakeGit(**{"merge-base": done(code=1)}))
    r = runner.invoke(app, ["update"])
    assert r.exit_code == 3
    assert parse_out(r)["status"] == "diverged"
    assert "resolve it yourself" in r.stderr


def test_local_edits_are_stashed_and_restored(runner, git):
    git.dirty = True
    data = parse_out(runner.invoke(app, ["update"]))
    assert data["stash_restored"] is True
    assert git.ran("stash", "push")
    assert git.ran("stash", "pop")


def test_a_clean_tree_is_not_stashed(runner, git):
    runner.invoke(app, ["update"])
    assert not git.ran("stash", "push")


def test_conflicts_stop_and_name_the_files(runner, monkeypatch):
    """The one path where work could be lost. It must not resolve anything."""
    fake = FakeGit(**{
        "stash pop": done(code=1, stderr="CONFLICT"),
        "diff --name-only --diff-filter=U": done("food_cli/cli.py\n"),
    })
    fake.dirty = True
    monkeypatch.setattr(U, "_git", fake)
    monkeypatch.setattr(U, "is_git_checkout", lambda root=None: True)
    monkeypatch.setattr(U.subprocess, "run", lambda *a, **k: done())

    r = runner.invoke(app, ["update"])
    assert r.exit_code == 4
    data = parse_out(r)
    assert data["status"] == "conflicts"
    assert data["conflicts"] == ["food_cli/cli.py"]
    assert "Nothing has been lost" in r.stderr
    assert "git stash drop" in data["hint"]


def test_a_failed_merge_puts_local_changes_back(runner, monkeypatch):
    fake = FakeGit(**{"merge": done(code=1, stderr="cannot fast-forward")})
    fake.dirty = True
    monkeypatch.setattr(U, "_git", fake)
    monkeypatch.setattr(U, "is_git_checkout", lambda root=None: True)
    monkeypatch.setattr(U.subprocess, "run", lambda *a, **k: done())

    r = runner.invoke(app, ["update"])
    assert r.exit_code == 3
    assert parse_out(r)["stash_restored"] is True
    assert fake.ran("stash", "pop")


def test_an_unreachable_remote_is_reported_not_swallowed(runner, monkeypatch):
    fake = FakeGit(**{"fetch": done(code=1, stderr="Could not resolve host")})
    monkeypatch.setattr(U, "_git", fake)
    monkeypatch.setattr(U, "is_git_checkout", lambda root=None: True)
    r = runner.invoke(app, ["update"])
    assert r.exit_code == 1
    assert parse_out(r)["status"] == "fetch_failed"
    assert not fake.ran("merge")


def test_dry_run_changes_nothing(runner, git):
    data = parse_out(runner.invoke(app, ["update", "--dry-run"]))
    assert data["status"] == "dry_run"
    assert data["changed_files"] == ["food_cli/cli.py", "SKILL.md"]
    assert not git.ran("merge")
    assert not git.ran("stash", "push")


def test_sync_can_be_skipped(runner, git, monkeypatch):
    seen = []
    monkeypatch.setattr(U.subprocess, "run",
                        lambda a, **k: (seen.append(a), done())[1])
    runner.invoke(app, ["update", "--no-sync"])
    assert not any("sync" in " ".join(a) for a in seen)


def test_sync_runs_by_default(runner, git, monkeypatch):
    seen = []
    monkeypatch.setattr(U.subprocess, "run",
                        lambda a, **k: (seen.append(a), done())[1])
    runner.invoke(app, ["update"])
    assert any(a[:2] == ["uv", "sync"] for a in seen)


def test_check_runs_the_suite_and_reports_failure(runner, git, monkeypatch):
    def fake_run(a, **k):
        if "pytest" in a:
            return done(stdout="1 failed", code=1)
        return done()
    monkeypatch.setattr(U.subprocess, "run", fake_run)
    r = runner.invoke(app, ["update", "--check"])
    assert parse_out(r)["tests_passed"] is False
    assert "Tests failed" in r.stderr
