"""version: print fixture/VERSION, or write a new x.y.z into it."""
import re
import sys
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"


def main(argv):
    """Print the version, or set it when one valid argument is given."""
    if not argv:
        print(VERSION_FILE.read_text(encoding="utf-8").strip())
        return 0
    if len(argv) != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", argv[0]):
        print("usage: version.py [x.y.z]")
        return 2
    VERSION_FILE.write_text(argv[0] + "\n", encoding="utf-8", newline="\n")
    print(f"version set to {argv[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
