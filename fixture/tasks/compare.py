"""compare <base>: time the base against the change, interleaved, with a noise floor."""
import re
import statistics
import subprocess
import sys

from basetree import ROOT, base_tree

PAIRS = 10
MIN_NOISE = 0.05
NOTES = ROOT / "dist" / "NOTES.md"


def bench(harness, tree):
    """Return the seconds one run takes in a tree, from the base's bench.py."""
    done = subprocess.run([sys.executable, str(harness), str(tree)], cwd=ROOT,
                          capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"bench failed: {done.stderr.strip()}")
    return float(done.stdout)


def write_notes(section):
    """Put the Timing section in dist/NOTES.md, replacing an older one."""
    NOTES.parent.mkdir(exist_ok=True)
    old = NOTES.read_text(encoding="utf-8") if NOTES.is_file() else ""
    # An older section runs to the next heading or the end of the file.
    old = re.sub(r"(?ms)^### Timing\n.*?(?=^#|\Z)", "", old).rstrip("\n")
    text = (old + "\n\n" if old else "") + section
    NOTES.write_text(text, encoding="utf-8", newline="\n")


def main(argv):
    """Measure and return the exit code.

    Interleaving cancels machine drift. The base-against-base ratio is the
    noise floor; only a slowdown past three times it fails.
    """
    if not argv or not argv[0]:
        print("compare: no base; nothing to compare")
        return 0
    with base_tree(argv[0]) as base:
        harness = base / "fixture" / "tasks" / "bench.py"
        if not harness.is_file():
            print("compare: base has no harness; nothing to compare")
            return 0
        bench(harness, base), bench(harness, ROOT)  # warm-up: cold caches would favour the second
        pairs = [(bench(harness, base), bench(harness, ROOT)) for _ in range(PAIRS)]
        twins = [(bench(harness, base), bench(harness, base)) for _ in range(PAIRS)]
    med_base = statistics.median(b for b, _ in pairs)
    med_change = statistics.median(c for _, c in pairs)
    ratio = med_change / med_base
    noise = max(MIN_NOISE, abs(statistics.median(a for a, _ in twins)
                               / statistics.median(b for _, b in twins) - 1))
    limit = 1 + 3 * noise
    verdict = "slower than the noise allows" if ratio > limit else "within the noise"
    section = (f"### Timing\n\n| | value |\n| --- | --- |\n"
              f"| median base | {med_base:.4f} s |\n| median change | {med_change:.4f} s |\n"
              f"| ratio | {ratio:.3f} |\n| noise floor | {noise * 100:.1f}% |\n"
              f"| limit | {limit:.3f} |\n| verdict | {verdict} |\n")
    write_notes(section)
    print(section, end="")
    return 1 if ratio > limit else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
