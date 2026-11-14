"""budgets: compare exact metrics with the maxima in fixture/budgets.toml."""
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def source_bytes():
    """Total bytes of fixture/hello/*.py with LF line endings.

    Normalised so Windows and Linux checkouts give the same number.
    """
    paths = (ROOT / "fixture" / "hello").glob("*.py")
    return sum(len(p.read_bytes().replace(b"\r\n", b"\n")) for p in paths)


METRICS = {"source-bytes": source_bytes}


def main():
    """Check every budget and return the exit code (1 when any is over)."""
    data = tomllib.loads((ROOT / "fixture" / "budgets.toml").read_text(encoding="utf-8"))
    bad = 0
    for budget in data["budget"]:
        name, limit = budget["name"], budget["max"]
        if name not in METRICS:
            print(f"budgets: {name}: no such metric")
            bad += 1
            continue
        value = METRICS[name]()
        if value > limit:
            print(f"budgets: {name} grew to {value}, over the max {limit} by {value - limit}")
            print(f"budgets: why it is capped: {budget['why']}")
            print("budgets: growth the app needs goes to the owner with an impact note")
            bad += 1
        else:
            print(f"budgets: {name} {value} / {limit}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
