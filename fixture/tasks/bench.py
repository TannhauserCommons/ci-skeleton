"""bench <tree-root>: print the seconds one `python -m hello` run takes in that tree.

One whole process on purpose: it is what a user waits for, and it is honest
about start-up cost. Compare runs the base's copy against both trees.
"""
import os
import subprocess
import sys
import time
from pathlib import Path


def main(argv):
    """Time one run of the greeting and print the seconds."""
    if len(argv) != 1:
        print("usage: bench.py <tree-root>", file=sys.stderr)
        return 2
    root = Path(argv[0]).resolve()
    env = {k: v for k, v in os.environ.items() if k != "HELLO_FLAGS"}
    env["PYTHONPATH"] = str(root / "fixture")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    start = time.perf_counter()
    done = subprocess.run([sys.executable, "-m", "hello"], env=env, cwd=root,
                          capture_output=True, text=True)
    elapsed = time.perf_counter() - start
    if done.returncode:
        sys.stderr.write(done.stderr)
        return done.returncode
    print(f"{elapsed:.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
