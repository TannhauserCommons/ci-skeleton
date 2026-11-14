"""looks <base>: render the key screen on the base and the change, twice each, exactly."""
import difflib
import os
import subprocess
import sys

from basetree import ROOT, base_tree


def render(harness, tree):
    """Run the harness on a tree and return its output (raises if it fails)."""
    done = subprocess.run([sys.executable, str(harness), str(tree)], cwd=ROOT,
                          capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"render failed on {tree.name or tree}: {done.stderr.strip()}")
    return done.stdout


def main(argv):
    """Compare screens and return the exit code.

    The base's render.py runs on both sides so a change cannot weaken the
    check that judges it. Each side renders twice: a screen that differs from
    itself cannot be compared.
    """
    if len(argv) != 1 or not argv[0]:
        print("usage: looks.py <base>", file=sys.stderr)
        return 2
    if set(argv[0]) == {"0"}:
        # GitHub's "before" of the first push to a branch: there is no base yet.
        print("looks: no base; nothing to compare")
        return 0
    with base_tree(argv[0]) as base:
        harness = base / "fixture" / "tasks" / "render.py"
        if not harness.is_file():
            print("looks: base has no harness; nothing to compare")
            return 0
        try:
            sides = {"base": [render(harness, base) for _ in range(2)],
                     "change": [render(harness, ROOT) for _ in range(2)]}
        except RuntimeError as err:
            print(f"looks: {err}")
            return 1
    for name, (first, second) in sides.items():
        if first != second:
            print(f"looks: the {name} screen is unstable: two renders differ")
            return 1
    if sides["base"][0] == sides["change"][0]:
        print("looks: no change")
        return 0
    diff = "".join(difflib.unified_diff(sides["base"][0].splitlines(True),
                                        sides["change"][0].splitlines(True), "base", "change"))
    if os.environ.get("VISUAL_CHANGE") == "1":
        print("declared visual change:")
        print(diff, end="")
        return 0
    print("looks: the screen changed")
    print(diff, end="")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
