"""smoke: run every dist/*.pyz and check its version and greeting."""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NAME = re.compile(r"^hello-(\d+\.\d+\.\d+)-\w+\.pyz$")


def run(archive, *args):
    """Run an archive with the current Python and return its stripped stdout.

    The environment is clean so neither HELLO_FLAGS nor PYTHONPATH can make
    the answer differ from what a user gets.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("HELLO_FLAGS", "PYTHONPATH")}
    done = subprocess.run([sys.executable, str(archive), *args], env=env, cwd=ROOT,
                          capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"exit {done.returncode}: {done.stderr.strip()}")
    return done.stdout.strip()


def main():
    """Check each archive in dist/ and return the exit code.

    Archives run on any OS, so every file present is tested, not just this
    runner's own target.
    """
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / "fixture"))
    import hello
    want = hello.greeting({})  # the build freezes the same defaults
    archives = sorted((ROOT / "dist").glob("*.pyz"))
    if not archives:
        print("smoke: no .pyz in dist/")
        return 1
    bad = 0
    for archive in archives:
        match = NAME.match(archive.name)
        try:
            version, greeting = run(archive, "--version"), run(archive)
        except RuntimeError as err:
            print(f"smoke: {archive.name}: {err}")
            bad += 1
            continue
        if not match or version != match.group(1) or greeting != want:
            print(f"smoke: {archive.name}: got version {version!r}, greeting {greeting!r}")
            bad += 1
        else:
            print(f"smoke: {archive.name} ok")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
