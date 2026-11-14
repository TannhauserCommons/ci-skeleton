"""basetree: check a base commit out into .base/ for looks and compare."""
import contextlib
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / ".base"


def git(*args, check=True):
    """Run git in the repo root and return the completed process."""
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def remove():
    """Drop the .base worktree, including a stale one left by a crashed run."""
    git("worktree", "remove", "--force", ".base", check=False)
    shutil.rmtree(BASE, ignore_errors=True)
    git("worktree", "prune", check=False)


@contextlib.contextmanager
def base_tree(rev):
    """Yield the path of a detached checkout of `rev`, removed again on exit.

    Why a worktree: the base's own harness must run, so its files must exist
    on disk, and the change's tree stays untouched.
    """
    remove()
    git("worktree", "add", "--detach", ".base", rev)
    try:
        yield BASE
    finally:
        remove()
