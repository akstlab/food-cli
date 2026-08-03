"""Update the installed CLI from its git checkout.

Deliberately conservative, because this command pulls code and then runs it:

* It only ever fast-forwards. A diverged branch means someone has local commits,
  and silently merging or rebasing them is not this command's decision to make.
* Local edits are stashed and put back. If restoring them conflicts, the
  conflict is left in the tree for a human and the command exits non-zero -
  it never resolves or discards anyone's work.
* Nothing is fetched from anywhere but the checkout's existing remote.

Exit codes: 0 up to date or updated, 2 not a git checkout, 3 refused (diverged
or dirty in a way that cannot be handled), 4 the stash came back with conflicts.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import typer

from .common import err, out

#: The repo root is two levels up from this file: food_cli/commands/update.py.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

STASH_PREFIX = "food-cli-update"


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run one git command. Fixed argv, never a shell."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


def _ok(proc: subprocess.CompletedProcess) -> bool:
    return proc.returncode == 0


def _line(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.strip()


def is_git_checkout(root: Path | None = None) -> bool:
    root = root or REPO_ROOT
    return (root / ".git").exists() and _ok(_git("rev-parse", "--git-dir", cwd=root))


def conflicted_files() -> list[str]:
    """Paths git reports as unmerged."""
    proc = _git("diff", "--name-only", "--diff-filter=U")
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def update(
    check: bool = typer.Option(
        False, "--check", help="Run the test suite after updating."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would change without touching anything."
    ),
    sync: bool = typer.Option(
        True, "--sync/--no-sync", help="Run `uv sync` afterwards to pick up dependencies."
    ),
):
    """Update this CLI from its git remote, keeping any local edits.

    Fetches, fast-forwards, and restores anything you had uncommitted. Stops and
    hands back to you if the branch has diverged or the restore conflicts -
    it will not merge, rebase or discard your work.
    """
    if not is_git_checkout():
        err(
            f"{REPO_ROOT} is not a git checkout, so there is nothing to pull.\n"
            "Reinstall instead: "
            "git clone https://github.com/akstlab/food-cli.git && cd food-cli && uv sync"
        )
        out({"status": "not_a_checkout", "path": str(REPO_ROOT)})
        raise typer.Exit(2)

    branch = _line(_git("rev-parse", "--abbrev-ref", "HEAD"))
    remote = _line(_git("remote", "get-url", "origin"))
    before = _line(_git("rev-parse", "HEAD"))

    fetched = _git("fetch", "origin", branch)
    if not _ok(fetched):
        err(f"Could not reach {remote}:\n{fetched.stderr.strip()[:300]}")
        out({"status": "fetch_failed", "remote": remote})
        raise typer.Exit(1)

    upstream = f"origin/{branch}"
    target = _line(_git("rev-parse", upstream))

    if target == before:
        err("Already up to date.")
        out({"status": "up_to_date", "branch": branch, "commit": before[:12]})
        return

    # Refuse anything that is not a clean fast-forward: local commits are the
    # user's, and this command does not get to decide how they are integrated.
    if not _ok(_git("merge-base", "--is-ancestor", before, target)):
        err(
            f"Your {branch} has commits that {upstream} does not.\n"
            "Not merging or rebasing that for you - resolve it yourself:\n"
            f"    cd {REPO_ROOT} && git log --oneline {upstream}..HEAD"
        )
        out({"status": "diverged", "branch": branch,
             "local": before[:12], "remote": target[:12]})
        raise typer.Exit(3)

    log = _git("log", "--oneline", f"{before}..{target}")
    incoming = [ln for ln in log.stdout.splitlines() if ln.strip()]
    changed = _git("diff", "--name-only", before, target).stdout.split()

    if dry_run:
        err(f"{len(incoming)} commit(s) would be pulled from {remote}.")
        out({
            "status": "dry_run",
            "branch": branch,
            "from": before[:12],
            "to": target[:12],
            "commits": incoming,
            "changed_files": changed,
        })
        return

    dirty = bool(_line(_git("status", "--porcelain")))
    stash_ref = None
    if dirty:
        label = f"{STASH_PREFIX}-{int(time.time())}"
        stashed = _git("stash", "push", "--include-untracked", "-m", label)
        if not _ok(stashed):
            err(f"Could not stash your local changes:\n{stashed.stderr.strip()[:300]}")
            out({"status": "stash_failed"})
            raise typer.Exit(3)
        stash_ref = label
        err(f"Stashed your local changes as {label}.")

    merged = _git("merge", "--ff-only", upstream)
    if not _ok(merged):
        err(f"Update failed:\n{merged.stderr.strip()[:300]}")
        if stash_ref:
            _git("stash", "pop")
            err("Your local changes have been put back.")
        out({"status": "update_failed", "stash_restored": bool(stash_ref)})
        raise typer.Exit(3)

    result = {
        "status": "updated",
        "branch": branch,
        "from": before[:12],
        "to": target[:12],
        "commits": incoming,
        "changed_files": changed,
        "skill_changed": any(f in ("SKILL.md", "REFERENCE.md") for f in changed),
    }

    if stash_ref:
        popped = _git("stash", "pop")
        if not _ok(popped):
            files = conflicted_files()
            err(
                "\n⚠️  Updated, but restoring your local changes hit conflicts.\n"
                "Nothing has been lost - your work is in the tree and in the "
                "stash. Resolve these, then `git stash drop`:\n"
                + "".join(f"    {f}\n" for f in files)
            )
            out({**result, "status": "conflicts",
                 "conflicts": files, "stash": stash_ref,
                 "hint": "Resolve the conflicts, then run `git stash drop`."})
            raise typer.Exit(4)
        err("Restored your local changes.")
        result["stash_restored"] = True

    if sync:
        synced = subprocess.run(
            ["uv", "sync"], cwd=str(REPO_ROOT),
            capture_output=True, text=True, check=False,
        )
        result["dependencies_synced"] = synced.returncode == 0
        if synced.returncode != 0:
            err(f"`uv sync` failed:\n{synced.stderr.strip()[:300]}\nRun it yourself.")

    if check:
        tested = subprocess.run(
            ["uv", "run", "pytest", "-q"], cwd=str(REPO_ROOT),
            capture_output=True, text=True, check=False,
        )
        result["tests_passed"] = tested.returncode == 0
        if tested.returncode != 0:
            err("Tests failed after updating:\n" + tested.stdout.strip()[-600:])

    err(f"\n✅ Updated {before[:8]} → {target[:8]} ({len(incoming)} commit(s)).")
    if result["skill_changed"]:
        err("SKILL.md or REFERENCE.md changed — re-read it before ordering again.")
    out(result)
