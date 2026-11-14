"""render <tree-root>: print that tree's key screen.

Looks runs the base's copy of this file against both trees, so a change cannot
weaken the check that judges it.
"""
import os
import subprocess
import sys
from pathlib import Path


def main(argv):
    """Run `python -m hello --screen` from a tree and echo its output."""
    if len(argv) != 1:
        print("usage: render.py <tree-root>", file=sys.stderr)
        return 2
    root = Path(argv[0]).resolve()
    env = {k: v for k, v in os.environ.items() if k != "HELLO_FLAGS"}
    env["PYTHONPATH"] = str(root / "fixture")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run([sys.executable, "-m", "hello", "--screen"], env=env, cwd=root,
                          capture_output=True, text=True)
    sys.stderr.write(done.stderr)
    sys.stdout.write(done.stdout)
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
