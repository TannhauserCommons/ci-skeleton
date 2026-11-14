"""publish: sign the files in dist/ with a SHA-256 file each, then verify them."""
import hashlib
import os
import sys
from pathlib import Path

DIST = Path(__file__).resolve().parents[2] / "dist"
SKIP = {"candidate.json", "NOTES.md"}


def digest(path):
    """Return the hex SHA-256 of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def targets():
    """Return the files to sign: everything in dist/ but the notes, the candidate record and signatures."""
    return sorted(p for p in DIST.iterdir()
                  if p.is_file() and p.name not in SKIP and p.suffix != ".sha256")


def main():
    """Sign, verify and report; return the exit code.

    Signing rewrites the same bytes each time, so a second run is harmless.
    """
    files = targets() if DIST.is_dir() else []
    if not files:
        print("publish: nothing to sign in dist/")
        return 1
    for path in files:
        sig = path.with_name(path.name + ".sha256")
        sig.write_text(f"{digest(path)} {path.name}\n", encoding="utf-8", newline="\n")
    for path in files:
        sig = path.with_name(path.name + ".sha256")
        if sig.read_text(encoding="utf-8").strip() != f"{digest(path)} {path.name}":
            print(f"publish: bad signature for {path.name}")
            return 1
        print(f"publish: signed and verified {path.name}")
    if os.environ.get("DRY_RUN") == "1":
        print("dry run: nothing deployed")
    else:
        print("deployed: nothing to deploy (fixture)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
