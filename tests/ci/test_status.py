"""Tests for .github/ci/status.py. GitHub is a fake behind `ghapi.api`."""

import contextlib
import io
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".github" / "ci"))

import ghapi  # noqa: E402
import status  # noqa: E402

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
MARKER = "<!-- ci:candidate -->"


def stamp(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pr(number, title="Change", sha=None, pushed=10, draft=False):
    return {"number": number, "title": title, "head": {"sha": sha or f"{number:x}" * 40},
            "updated_at": stamp(pushed), "draft": draft}


def check(conclusion, started=5, name="gate", state="completed"):
    return {"name": name, "status": state, "conclusion": conclusion, "started_at": stamp(started)}


class FakeGitHub:
    def __init__(self, prs=(), checks=None, releases=(), red=None, flaky=0, runs=()):
        self.prs = list(prs)
        self.checks = checks or {}  # sha -> list of check runs
        self.releases = list(releases)
        self.red = red
        self.flaky = flaky
        self.runs = [{"conclusion": c, "html_url": f"https://x/runs/{i}"} for i, c in enumerate(runs)]
        self.requested = []

    def __call__(self, path, method="GET", body=None, paginate=False):
        self.requested.append(path)
        if path.startswith("pulls?state=open"):
            return self.prs
        if path.startswith("commits/") and "/check-runs" in path:
            sha = path.split("/")[1]
            return {"check_runs": self.checks.get(sha, [])}
        if path.startswith("actions/workflows/main.yml/runs?") and "&status=completed" in path:
            return {"workflow_runs": self.runs}
        if path.startswith("releases"):
            return self.releases
        if path.startswith("issues?state=open&labels=main-red"):
            return getattr(self, "red_list", None) or ([self.red] if self.red else [])
        if path.startswith("issues?state=open&labels=flaky"):
            return [{"number": n, "title": f"flaky: {n}"} for n in range(self.flaky)] + [
                {"number": 99, "title": "a pr", "pull_request": {}}]
        raise AssertionError(f"unexpected GET {path}")


def run_status(fake):
    out = io.StringIO()
    with mock.patch.object(ghapi, "api", fake), contextlib.redirect_stdout(out):
        code = status.main([])
    return code, out.getvalue().splitlines()


class StatusTests(unittest.TestCase):
    def setUp(self):
        # The tests must not depend on the CI_BRANCH of whoever runs them.
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("CI_BRANCH", None)

    def lines(self, fake):
        with mock.patch.object(status, "datetime") as clock:
            clock.now.return_value = NOW
            clock.fromisoformat = datetime.fromisoformat
            code, lines = run_status(fake)
        self.assertEqual(code, 0)
        return lines

    def test_quiet_repo(self):
        self.assertEqual(self.lines(FakeGitHub()), ["main: green", "candidate: none"])

    def test_main_red_with_issue_link(self):
        red = {"number": 3, "title": "main is red", "html_url": "https://github.com/o/r/issues/3"}
        self.assertEqual(self.lines(FakeGitHub(red=red))[0], "main: red  https://github.com/o/r/issues/3")

    def test_main_red_lists_every_check_issue(self):
        fake = FakeGitHub()
        fake.red_list = [{"number": 3, "html_url": "https://x/3"}, {"number": 4, "html_url": "https://x/4"}]
        self.assertEqual(self.lines(fake)[0], "main: red  https://x/3  https://x/4")

    def test_newest_main_run_failed_at_startup_is_red_without_an_issue(self):
        lines = self.lines(FakeGitHub(runs=["startup_failure", "success"]))
        self.assertEqual(lines[0], "main: red  startup_failure  https://x/runs/0")

    def test_cancelled_main_run_leaves_the_run_before_it_standing(self):
        self.assertEqual(self.lines(FakeGitHub(runs=["cancelled", "success", "failure"]))[0],
                         "main: green")
        self.assertEqual(self.lines(FakeGitHub(runs=["cancelled", "failure"]))[0],
                         "main: red  failure  https://x/runs/1")

    def test_main_query_uses_main_when_ci_branch_is_unset_or_empty(self):
        for value in (None, "", "  "):
            with self.subTest(value=value):
                if value is not None:
                    os.environ["CI_BRANCH"] = value
                fake = FakeGitHub()
                self.assertEqual(self.lines(fake)[0], "main: green")
                self.assertIn("actions/workflows/main.yml/runs?branch=main&status=completed&per_page=10",
                              fake.requested)

    def test_release_branch_names_the_line_and_the_query(self):
        os.environ["CI_BRANCH"] = "next"
        fake = FakeGitHub()
        self.assertEqual(self.lines(fake)[0], "next: green")
        self.assertIn("actions/workflows/main.yml/runs?branch=next&status=completed&per_page=10",
                      fake.requested)
        self.assertFalse(any("branch=main" in p for p in fake.requested))

    def test_release_branch_red_lines(self):
        os.environ["CI_BRANCH"] = "next"
        red = {"number": 3, "html_url": "https://x/3"}
        self.assertEqual(self.lines(FakeGitHub(red=red))[0], "next: red  https://x/3")
        self.assertEqual(self.lines(FakeGitHub(runs=["failure"]))[0], "next: red  failure  https://x/runs/0")

    def test_pr_with_failed_gate(self):
        p = pr(12, "Break things")
        fake = FakeGitHub([p], {p["head"]["sha"]: [check("failure")]})
        self.assertIn("pr #12: gate failed  Break things", self.lines(fake))

    def test_failed_gate_is_not_also_reported_stuck(self):
        p = pr(12, pushed=300)
        fake = FakeGitHub([p], {p["head"]["sha"]: [check("failure")]})
        lines = self.lines(fake)
        self.assertEqual([l for l in lines if l.startswith("pr #")], ["pr #12: gate failed  Change"])

    def test_pending_gate_over_an_hour_is_stuck(self):
        p = pr(13, "Slow", pushed=125)
        fake = FakeGitHub([p], {p["head"]["sha"]: [check(None, state="in_progress")]})
        self.assertIn("pr #13: stuck  gate in_progress for 2h 5m  Slow", self.lines(fake))

    def test_missing_gate_over_an_hour_is_stuck(self):
        p = pr(14, pushed=61)
        lines = self.lines(FakeGitHub([p]))
        self.assertIn("pr #14: stuck  gate missing for 1h 1m  Change", lines)

    def test_pending_gate_within_the_hour_is_not_reported(self):
        p = pr(15, pushed=59)
        fake = FakeGitHub([p], {p["head"]["sha"]: [check(None, state="queued")]})
        self.assertEqual(self.lines(fake), ["main: green", "candidate: none"])

    def test_green_pr_waits_for_the_owner_however_old(self):
        for pushed in (10, 5000):
            with self.subTest(pushed=pushed):
                p = pr(16, "Ready", pushed=pushed)
                fake = FakeGitHub([p], {p["head"]["sha"]: [check("success")]})
                self.assertEqual(self.lines(fake), [
                    "main: green", "pr #16: green, waiting for the owner  Ready", "candidate: none"])

    def test_green_draft_pr_prints_nothing(self):
        p = pr(18, draft=True)
        fake = FakeGitHub([p], {p["head"]["sha"]: [check("success")]})
        self.assertEqual(self.lines(fake), ["main: green", "candidate: none"])

    def test_red_draft_pr_is_still_reported(self):
        p = pr(19, draft=True)
        fake = FakeGitHub([p], {p["head"]["sha"]: [check("failure")]})
        self.assertIn("pr #19: gate failed  Change", self.lines(fake))

    def test_pr_lines_order_failed_then_stuck_then_green(self):
        green, stuck, red = pr(1, "G"), pr(2, "S", pushed=200), pr(3, "R")
        fake = FakeGitHub([green, stuck, red], {green["head"]["sha"]: [check("success")],
                                                red["head"]["sha"]: [check("failure")]})
        prs = [l for l in self.lines(fake) if l.startswith("pr #")]
        self.assertEqual(prs, [
            "pr #3: gate failed  R",
            "pr #2: stuck  gate missing for 3h 20m  S",
            "pr #1: green, waiting for the owner  G",
        ])

    def test_newest_gate_run_wins_and_other_checks_are_ignored(self):
        p = pr(17, pushed=500)
        runs = [check("failure", started=200), check("success", started=20),
                check("failure", started=1, name="lint")]
        fake = FakeGitHub([p], {p["head"]["sha"]: runs})
        self.assertEqual([l for l in self.lines(fake) if l.startswith("pr #")],
                         ["pr #17: green, waiting for the owner  Change"])

    def test_failed_prs_come_before_stuck_prs(self):
        stuck, red = pr(1, pushed=200), pr(2)
        fake = FakeGitHub([stuck, red], {red["head"]["sha"]: [check("failure")]})
        prs = [l for l in self.lines(fake) if l.startswith("pr #")]
        self.assertEqual([l.split(":")[0] for l in prs], ["pr #2", "pr #1"])

    def test_candidate_from_the_draft_with_the_marker(self):
        releases = [
            {"draft": True, "body": "other draft", "tag_name": "v9.9.9", "target_commitish": "f" * 40},
            {"draft": False, "body": "x\n" + MARKER, "tag_name": "v0.1.0", "target_commitish": "e" * 40},
            {"draft": True, "body": "notes\n" + MARKER + "\n", "tag_name": "v0.2.0",
             "target_commitish": "abcdef1234567890"},
        ]
        self.assertIn("candidate: v0.2.0 abcdef1", self.lines(FakeGitHub(releases=releases)))

    def test_flaky_count_only_when_there_are_any(self):
        self.assertFalse(any(l.startswith("flaky") for l in self.lines(FakeGitHub())))
        self.assertEqual(self.lines(FakeGitHub(flaky=3))[-1], "flaky: 3 open")

    def test_full_order(self):
        red = {"number": 3, "html_url": "https://x/3"}
        p = pr(8, pushed=90)
        releases = [{"draft": True, "body": MARKER, "tag_name": "v1.0.0", "target_commitish": "a" * 40}]
        fake = FakeGitHub([p], releases=releases, red=red, flaky=1)
        self.assertEqual(self.lines(fake), [
            "main: red  https://x/3",
            "pr #8: stuck  gate missing for 1h 30m  Change",
            "candidate: v1.0.0 aaaaaaa",
            "flaky: 1 open",
        ])

    def test_title_from_github_is_cleaned(self):
        p = pr(5, "bad\x1b[0m title " + "x" * 200)
        fake = FakeGitHub([p], {p["head"]["sha"]: [check("failure")]})
        line = [l for l in self.lines(fake) if l.startswith("pr #5")][0]
        self.assertNotIn("\x1b", line)
        self.assertLess(len(line), 120)

    def test_github_unreachable_prints_one_line_and_exits_zero(self):
        def boom(*args, **kwargs):
            raise RuntimeError("gh repo view: not a git repository\nmore detail")
        out = io.StringIO()
        with mock.patch.object(ghapi, "api", boom), mock.patch.object(ghapi, "find_issue", boom), \
                contextlib.redirect_stdout(out):
            code = status.main([])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "status: gh repo view: not a git repository\n")

    def test_gh_not_installed_prints_one_line_and_exits_zero(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory")
        out = io.StringIO()
        with mock.patch.object(ghapi, "api", missing), contextlib.redirect_stdout(out):
            self.assertEqual(status.main([]), 0)
        self.assertTrue(out.getvalue().startswith("status: "))
        self.assertEqual(len(out.getvalue().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
