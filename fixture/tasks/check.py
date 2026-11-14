"""check: run the fixture's unit tests; re-run failures once to classify flakes."""
import io
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FLAKES = ROOT / "flakes" / "fixture.txt"


def run(suite):
    """Run a suite quietly and return (tests run, failed ids, output).

    Quiet because a flaky first run is not news; only a real failure prints.
    """
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    failed = [t.id() for t, _ in result.failures + result.errors]
    return result.testsRun, list(dict.fromkeys(failed)), stream.getvalue()


def main():
    """Run, retry the failures once, report flakes, and return the exit code.

    A test that passes on retry is flaky: the task stays green and the id goes
    to flakes/fixture.txt. A test that fails twice is a real failure.
    """
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "fixture" / "tests"), top_level_dir=str(ROOT))
    count, failed, _ = run(suite)
    flaky, broken, output = [], [], ""
    if failed:
        os.environ["FIXTURE_RETRY"] = "1"
        _, broken, output = run(loader.loadTestsFromNames(failed))
        flaky = [i for i in failed if i not in broken]
    # Why: the upload step needs the folder even when nothing was flaky.
    FLAKES.parent.mkdir(exist_ok=True)
    FLAKES.write_text("".join(i + "\n" for i in flaky), encoding="utf-8", newline="\n")
    for test_id in flaky:
        print(f"check: flaky (passed on retry): {test_id}")
    if broken:
        print(output)
        for test_id in broken:
            print(f"check: failed twice: {test_id}")
        return 1
    print(f"check: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
