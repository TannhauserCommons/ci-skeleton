"""Tests for .github/ci/after.py. GitHub is a fake behind `ghapi.api` / `ghapi.gh`."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".github" / "ci"))

import after  # noqa: E402
import ghapi  # noqa: E402

RUN_ID = 500
HEAD = "d" * 40
BASE = "b" * 40
RUN_URL = "https://github.com/o/r/actions/runs/500"
# This attempt of the run started at STARTED; an artifact made before it is an earlier attempt's.
STARTED = "2026-01-10T10:00:00Z"
EARLIER = "2026-01-10T09:30:00Z"
DURING = "2026-01-10T10:05:00Z"


def step(number, name, conclusion="success"):
    return {"number": number, "name": name, "conclusion": conclusion}


def job(name, steps, conclusion="failure"):
    return {"name": name, "conclusion": conclusion, "steps": steps}


def real_failure(name="check / linux"):
    """A job whose own command failed."""
    return job(name, [step(1, "Set up job"), step(2, "Run actions/checkout"),
                      step(3, "mise run check", "failure")])


def infra_failure(name="check / mac"):
    """A job that died installing tools, before its command."""
    return job(name, [step(1, "Set up job"), step(2, "Run jdx/mise-action", "failure"),
                      step(3, "mise run check", "skipped")])


REPO = "o/r"


def make_run(conclusion="failure", attempt=1, path=".github/workflows/main.yml", event="push",
             branch="main", head_repo=REPO):
    return {
        "id": RUN_ID, "name": "main", "path": path, "event": event, "head_branch": branch,
        "conclusion": conclusion, "run_attempt": attempt, "head_sha": HEAD,
        "html_url": RUN_URL, "head_repository": {"full_name": head_repo} if head_repo else None,
        "run_started_at": STARTED,
        "head_commit": {"message": "Fix it\n\nbody", "author": {"name": "Ann"}},
    }


class FakeGitHub:
    """Answers the REST calls after.py and ghapi make, and records the writes."""

    def __init__(self, run, jobs=(), artifacts=(), issues=(), green=(), compare=None):
        self.run = run
        self.jobs = list(jobs)
        # A bare name is an artifact this attempt uploaded.
        self.artifacts = [
            {"id": 9000 + n, "name": a, "created_at": DURING} if isinstance(a, str) else a
            for n, a in enumerate(artifacts)
        ]
        self.issues = [dict(i) for i in issues]
        self.green = list(green)
        self.compare = compare
        self.completed = []  # finished main runs, newest first; empty: this run is the newest
        self.writes = []  # (method, path, body)
        self.requested = []

    def __call__(self, path, method="GET", body=None, paginate=False):
        self.requested.append((method, path))
        if method != "GET":
            return self.write(path, method, body)
        if path == f"actions/runs/{RUN_ID}":
            return self.run
        if "/attempts/" in path and "/jobs" in path:
            return {"jobs": self.jobs}
        if path.startswith(f"actions/runs/{RUN_ID}/artifacts"):
            # The API gives at most 100 a page.
            pages = [{"artifacts": self.artifacts[n:n + 100]}
                     for n in range(0, len(self.artifacts), 100)] or [{"artifacts": []}]
            return pages if paginate else pages[0]
        if path.startswith("issues/") and "/comments" in path:
            number = int(path.split("/")[1])
            return next((i.get("comments", []) for i in self.issues if i["number"] == number), [])
        if path.startswith("issues?state=open&labels="):
            label = path.split("labels=")[1].split("&")[0]
            return [i for i in self.issues if label in i["labels"] and i["state"] == "open"]
        if path.startswith("labels"):
            return []
        if path.startswith("actions/workflows/main.yml/runs?") and "&status=completed" in path:
            return {"workflow_runs": self.completed}
        if path.startswith("actions/workflows/main.yml/runs"):
            return {"workflow_runs": self.green}
        if path.startswith("compare/"):
            if self.compare is None:
                raise RuntimeError("gh api compare: Not Found")
            return self.compare
        raise AssertionError(f"unexpected GET {path}")

    def write(self, path, method, body):
        self.writes.append((method, path, body))
        if method == "POST" and path == "issues":
            number = 100 + len(self.issues)
            self.issues.append({"number": number, "title": body["title"],
                                "labels": body["labels"], "state": "open"})
            return {"number": number}
        return None

    def posts(self, suffix):
        return [w for w in self.writes if w[0] == "POST" and w[1].endswith(suffix)]

    def opened(self):
        return [w[2] for w in self.writes if w[0] == "POST" and w[1] == "issues"]

    def comments(self):
        return [w[2]["body"] for w in self.writes if w[1].endswith("/comments")]


def open_issue(number, title, label, created_at="2026-01-01T00:00:00Z", comments=()):
    return {"number": number, "title": title, "labels": [label], "state": "open",
            "html_url": f"https://github.com/o/r/issues/{number}", "created_at": created_at,
            "comments": list(comments)}


def write_flake_files(files, fake):
    """A stand-in for `gh api .../artifacts/<id>/zip`: writes that artifact's zip to `out`.

    `files` is {relative path: text}. A path under an artifact's name goes into
    that artifact only; any other path goes into every artifact.
    """
    def download(*args, out=None, **kwargs):
        artifact_id = int(args[1].split("/artifacts/")[1].split("/")[0])
        name = next(a["name"] for a in fake.artifacts if a["id"] == artifact_id)
        names = {a["name"] for a in fake.artifacts}
        with zipfile.ZipFile(out, "w") as archive:
            for rel, text in files.items():
                first, _, rest = rel.partition("/")
                if first == name:
                    archive.writestr(rest, text)
                elif first not in names:
                    archive.writestr(rel, text)
        return None
    return download


def junit(*cases):
    return "<testsuites><testsuite>" + "".join(cases) + "</testsuite></testsuites>"


class AfterTestCase(unittest.TestCase):
    def setUp(self):
        # The tests must not depend on the CI_BRANCH of whoever runs them.
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("CI_BRANCH", None)

    def run_after(self, fake, files=None, run_id=RUN_ID):
        """Run after.py against `fake`; returns (exit code, stdout, gh mock)."""
        gh = mock.Mock(side_effect=write_flake_files(files or {}, fake))
        out = io.StringIO()
        with mock.patch.object(ghapi, "api", fake), mock.patch.object(ghapi, "gh", gh), \
                mock.patch.object(ghapi, "repo", return_value=REPO), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = after.main([str(run_id)])
        return code, out.getvalue(), gh


class SkipTests(AfterTestCase):
    def test_cancelled_pr_run_does_nothing(self):
        fake = FakeGitHub(make_run("cancelled", path=".github/workflows/check.yml",
                                   event="pull_request"), jobs=[real_failure()])
        code, out, _ = self.run_after(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.requested, [("GET", f"actions/runs/{RUN_ID}")])

    def test_cancelled_main_run_with_a_failed_job_is_red(self):
        fake = FakeGitHub(make_run("cancelled"), jobs=[real_failure()])
        self.run_after(fake)
        self.assertEqual(len(fake.opened()), 1)

    def test_main_run_whose_job_timed_out_is_red_and_not_rerun(self):
        hung = job("build (linux)", [step(1, "Set up job"), step(2, "mise run build", "cancelled")],
                   conclusion="cancelled")
        fake = FakeGitHub(make_run("cancelled"), jobs=[hung, job("candidate", [], "skipped")])
        code, out, _ = self.run_after(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.posts("rerun-failed-jobs"), [])
        opened = fake.opened()
        self.assertEqual(len(opened), 1)
        self.assertIn("` build (linux) ` (timed out or cancelled)", opened[0]["body"])

    def test_main_run_replaced_before_it_started_does_nothing(self):
        waiting = job("build (linux)", [], conclusion="cancelled")
        fake = FakeGitHub(make_run("cancelled"), jobs=[waiting])
        self.run_after(fake)
        self.assertEqual(fake.writes, [])

    def test_cancelled_check_run_does_nothing_even_with_a_stopped_job(self):
        hung = job("test", [step(1, "Set up job"), step(2, "mise run check", "cancelled")],
                   conclusion="cancelled")
        fake = FakeGitHub(make_run("cancelled", path=".github/workflows/check.yml",
                                   event="pull_request"), jobs=[hung])
        self.run_after(fake)
        self.assertEqual(fake.writes, [])

    def test_skipped_run_does_nothing(self):
        fake = FakeGitHub(make_run("skipped"))
        self.assertEqual(self.run_after(fake)[0], 0)
        self.assertEqual(fake.writes, [])

    def test_bad_arguments_exit_nonzero(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            after.main(["not-a-number"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_api_error_is_an_error_exit(self):
        def boom(*args, **kwargs):
            raise RuntimeError("gh api: HTTP 500")
        out = io.StringIO()
        with mock.patch.object(ghapi, "api", boom), contextlib.redirect_stderr(out):
            self.assertEqual(after.main(["1"]), 1)
        self.assertIn("HTTP 500", out.getvalue())


class InfraTests(AfterTestCase):
    def test_infra_failure_on_attempt_one_reruns_and_files_nothing(self):
        fake = FakeGitHub(make_run("failure"), jobs=[infra_failure()], artifacts=["flakes-a"])
        code, out, gh = self.run_after(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes, [("POST", f"actions/runs/{RUN_ID}/rerun-failed-jobs", None)])
        self.assertIn("infra rerun: check / mac: Run jdx/mise-action", out)
        gh.assert_not_called()  # no flake download either

    def test_same_failure_on_attempt_two_is_real(self):
        fake = FakeGitHub(make_run("failure", attempt=2), jobs=[infra_failure()])
        self.run_after(fake)
        self.assertEqual(fake.posts("rerun-failed-jobs"), [])
        self.assertEqual(fake.opened()[0]["title"], "main is red: check / mac")

    def test_mix_of_infra_and_real_does_not_rerun(self):
        fake = FakeGitHub(make_run("failure"), jobs=[infra_failure(), real_failure()])
        self.run_after(fake)
        self.assertEqual(fake.posts("rerun-failed-jobs"), [])
        self.assertEqual(len(fake.opened()), 2)  # one per failed check

    def test_gate_failing_with_an_infra_job_still_reruns(self):
        gate = job("gate", [step(1, "Set up job"), step(2, "Run re-actors/alls-green", "failure")])
        fake = FakeGitHub(make_run(), jobs=[infra_failure(), gate])
        code, out, _ = self.run_after(fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.posts("rerun-failed-jobs")), 1)
        self.assertNotIn("gate", out)

    def test_called_gate_failing_with_a_real_job_does_not_rerun(self):
        gate = job("check / gate", [step(1, "Set up job"), step(2, "Run re-actors/alls-green", "failure")])
        fake = FakeGitHub(make_run(), jobs=[real_failure(), gate])
        self.run_after(fake)
        self.assertEqual(fake.posts("rerun-failed-jobs"), [])

    def test_gate_failing_alone_is_real(self):
        gate = job("gate", [step(1, "Set up job"), step(2, "Run re-actors/alls-green", "failure")])
        fake = FakeGitHub(make_run(), jobs=[gate])
        self.run_after(fake)
        self.assertEqual(fake.posts("rerun-failed-jobs"), [])

    def test_lost_runner_is_infra(self):
        lost = job("check / linux", [step(1, "Set up job", "success"), step(2, "mise run check", "cancelled")])
        fake = FakeGitHub(make_run("failure"), jobs=[lost])
        _, out, _ = self.run_after(fake)
        self.assertEqual(len(fake.posts("rerun-failed-jobs")), 1)
        self.assertIn("infra rerun: check / linux: no step failed", out)

    def test_job_without_steps_is_infra(self):
        fake = FakeGitHub(make_run("failure"), jobs=[job("check / linux", [])])
        self.run_after(fake)
        self.assertEqual(len(fake.posts("rerun-failed-jobs")), 1)

    def test_failed_mise_run_step_is_real(self):
        fake = FakeGitHub(make_run("failure"), jobs=[real_failure()])
        self.run_after(fake)
        self.assertEqual(fake.posts("rerun-failed-jobs"), [])

    def test_failure_after_the_command_is_real(self):
        late = job("check / linux", [step(1, "Set up job"), step(2, "mise run check"),
                                      step(3, "Post Run actions/checkout", "failure")])
        self.assertIsNone(after.infra_reason(late))

    def test_second_command_failing_is_real(self):
        two = job("check / linux", [step(1, "mise run build"), step(2, "mise run smoke", "failure")])
        self.assertIsNone(after.infra_reason(two))

    def test_no_command_step_only_set_up_job_is_infra(self):
        setup = job("x", [step(1, "Set up job", "failure")])
        other = job("x", [step(1, "Set up job"), step(2, "Run something", "failure")])
        self.assertIsNotNone(after.infra_reason(setup))
        self.assertIsNone(after.infra_reason(other))

    def test_called_workflow_job_is_treated_the_same(self):
        called = infra_failure("ci / build / linux")
        fake = FakeGitHub(make_run("failure"), jobs=[called])
        _, out, _ = self.run_after(fake)
        self.assertIn("infra rerun: ci / build / linux:", out)

    def test_cancelled_jobs_are_not_failed_jobs(self):
        cancelled = job("check / linux", [step(1, "Set up job"), step(2, "mise run check", "cancelled")],
                        conclusion="cancelled")
        self.assertEqual(after.failed_jobs([cancelled, real_failure()]), [real_failure()])

    def test_failure_with_only_cancelled_jobs_is_real_and_not_rerun(self):
        cancelled = job("check / linux", [step(1, "mise run check", "cancelled")], conclusion="cancelled")
        fake = FakeGitHub(make_run("failure"), jobs=[cancelled])
        self.run_after(fake)
        self.assertEqual(fake.posts("rerun-failed-jobs"), [])
        body = fake.opened()[0]["body"]
        self.assertIn("none recorded", body)

    def test_success_does_not_look_at_jobs(self):
        fake = FakeGitHub(make_run("success"))
        self.run_after(fake)
        self.assertFalse(any("/jobs" in path for _, path in fake.requested))


class FlakeParsingTests(unittest.TestCase):
    def ids(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in files.items():
                path = Path(tmp) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                return after.read_flake_dir(tmp)

    def test_junit_flaky_children(self):
        xml = junit(
            '<testcase classname="a.B" name="one"><flakyFailure message="x"/></testcase>',
            '<testcase classname="a.B" name="two"><rerunFailure/></testcase>',
            '<testcase classname="a.B" name="three"><flakyError/></testcase>',
            '<testcase classname="a.B" name="four"><rerunError/></testcase>',
            '<testcase classname="a.B" name="ok"/>',
        )
        self.assertEqual(self.ids({"flakes-x/r.xml": xml}),
                         ["a.B::one", "a.B::two", "a.B::three", "a.B::four"])

    def test_testcase_with_flaky_and_hard_failure_is_not_flaky(self):
        xml = junit(
            '<testcase classname="a" name="f"><flakyFailure/><failure/></testcase>',
            '<testcase classname="a" name="e"><rerunFailure/><error/></testcase>',
            '<testcase classname="a" name="flaky"><flakyFailure/></testcase>',
        )
        self.assertEqual(self.ids({"r.xml": xml}), ["a::flaky"])

    def test_junit_without_classname_uses_name(self):
        xml = junit('<testcase name="solo"><flakyFailure/></testcase>')
        self.assertEqual(self.ids({"r.xml": xml}), ["solo"])

    def test_junit_namespace_is_ignored(self):
        xml = '<testsuite xmlns="urn:x"><testcase classname="a" name="n"><flakyFailure/></testcase></testsuite>'
        self.assertEqual(self.ids({"r.xml": xml}), ["a::n"])

    def test_txt_one_id_per_line(self):
        self.assertEqual(self.ids({"flakes-a/f.txt": "test_a\n\n  test_b  \n"}), ["test_a", "test_b"])

    def test_duplicates_across_files_collapse(self):
        got = self.ids({"flakes-a/f.txt": "same\n", "flakes-b/f.txt": "same\nother\n"})
        self.assertEqual(got, ["same", "other"])

    def test_broken_xml_and_other_files_are_skipped(self):
        got = self.ids({"bad.xml": "<testsuite", "notes.md": "x", "good.txt": "t"})
        self.assertEqual(got, ["t"])

    def test_test_id_is_cleaned_and_capped(self):
        dirty = "evil\x1b[31m\x00name‮" + "x" * 500
        (got,) = self.ids({"f.txt": dirty + "\n"})
        self.assertEqual(len(got), 200)
        self.assertTrue(got.startswith("evil[31mname"))
        self.assertNotIn("\x1b", got)
        self.assertNotIn("‮", got)

    def test_id_of_only_control_characters_is_dropped(self):
        self.assertEqual(self.ids({"f.txt": "\x01\x02\n"}), [])


class FlakeIssueTests(AfterTestCase):
    def test_success_run_with_flakes_opens_issue_with_details(self):
        fake = FakeGitHub(make_run("success", path=".github/workflows/check.yml", event="pull_request"),
                          artifacts=["flakes-check-linux"])
        _, out, gh = self.run_after(fake, files={"flakes-check-linux/f.txt": "tests::slow\n"})
        gh.assert_called_once()
        self.assertEqual(gh.call_args.args, ("api", "repos/o/r/actions/artifacts/9000/zip"))
        (issue,) = fake.opened()
        self.assertEqual(issue["title"], "flaky: tests::slow")
        self.assertEqual(issue["labels"], ["flaky"])
        for expected in ("tests::slow", RUN_URL, "check.yml", HEAD):
            self.assertIn(expected, issue["body"])
        self.assertIn("opened #", out)

    def test_existing_issue_gets_a_one_line_comment(self):
        fake = FakeGitHub(make_run("success"), artifacts=["flakes-x"],
                          issues=[open_issue(7, "flaky: tests::slow", "flaky")])
        self.run_after(fake, files={"flakes-x/f.txt": "tests::slow\n"})
        self.assertEqual(fake.opened(), [])
        (comment,) = fake.comments()
        self.assertNotIn("\n", comment.strip())
        self.assertIn(RUN_URL, comment)
        self.assertIn(HEAD[:7], comment)

    def test_flakes_are_filed_for_a_failed_run_too(self):
        fake = FakeGitHub(make_run("failure", path=".github/workflows/check.yml", event="pull_request"),
                          jobs=[real_failure()], artifacts=["flakes-x"])
        self.run_after(fake, files={"f.txt": "tests::slow\n"})
        self.assertEqual([i["title"] for i in fake.opened()], ["flaky: tests::slow"])

    def test_no_flake_artifacts_means_no_download(self):
        fake = FakeGitHub(make_run("success"), artifacts=["dist-linux"])
        _, _, gh = self.run_after(fake)
        gh.assert_not_called()
        self.assertEqual(fake.writes, [])

    def test_cap_of_ten_new_issues_and_existing_ones_still_comment(self):
        ids = [f"t{n:02d}" for n in range(14)]
        existing = [open_issue(950, "flaky: t13", "flaky")]
        fake = FakeGitHub(make_run("success"), artifacts=["flakes-x"], issues=existing)
        _, out, _ = self.run_after(fake, files={"f.txt": "\n".join(ids) + "\n"})
        titles = [i["title"] for i in fake.opened()]
        self.assertEqual(titles, [f"flaky: t{n:02d}" for n in range(10)])
        self.assertEqual(len(fake.comments()), 1)  # t13 already had an issue
        self.assertIn("skipped 3 new issues", out)

    def test_hostile_test_id_cannot_break_the_body_markup(self):
        fake = FakeGitHub(make_run("success"), artifacts=["flakes-x"])
        self.run_after(fake, files={"f.txt": "a`b @everyone\n"})
        body = fake.opened()[0]["body"]
        self.assertIn("`` a`b @everyone ``", body)

    def test_a_rerun_files_only_the_flakes_of_its_own_attempt(self):
        # Attempt 2 reran a failed job; the job that flaked in attempt 1 was
        # carried over, and its artifact is still listed on the run.
        fake = FakeGitHub(make_run("success", attempt=2), artifacts=[
            {"id": 1, "name": "flakes-perf", "created_at": EARLIER},
            {"id": 2, "name": "flakes-ui", "created_at": DURING},
        ])
        _, _, gh = self.run_after(fake, files={"flakes-perf/f.txt": "tests::perf\n",
                                               "flakes-ui/f.txt": "tests::ui\n"})
        self.assertEqual([i["title"] for i in fake.opened()], ["flaky: tests::ui"])
        self.assertEqual(fake.comments(), [])
        self.assertEqual([c.args[1] for c in gh.call_args_list], ["repos/o/r/actions/artifacts/2/zip"])

    def test_a_rerun_whose_only_flakes_came_from_an_earlier_attempt_files_nothing(self):
        fake = FakeGitHub(make_run("success", attempt=3),
                          artifacts=[{"id": 1, "name": "flakes-perf", "created_at": EARLIER}],
                          issues=[open_issue(85, "flaky: tests::perf", "flaky")])
        _, _, gh = self.run_after(fake, files={"f.txt": "tests::perf\n"})
        gh.assert_not_called()
        self.assertEqual(fake.comments(), [])

    def test_flake_artifacts_past_the_first_page_are_read(self):
        dist = [{"id": n, "name": f"dist-{n}", "created_at": DURING} for n in range(100)]
        fake = FakeGitHub(make_run("success"),
                          artifacts=dist + [{"id": 500, "name": "flakes-x", "created_at": DURING}])
        self.run_after(fake, files={"f.txt": "tests::slow\n"})
        self.assertEqual([i["title"] for i in fake.opened()], ["flaky: tests::slow"])


def green_runs(count, after="2026-01-05T00:00:00Z"):
    """`count` green main runs made an hour apart after `after`, newest first."""
    start = datetime.fromisoformat(after.replace("Z", "+00:00"))
    runs = []
    for n in range(1, count + 1):
        made = (start + timedelta(hours=n)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        runs.append({"id": 100 + n, "head_sha": f"{n:040d}", "created_at": made,
                     "html_url": f"https://github.com/o/r/actions/runs/{100 + n}"})
    return runs[::-1]


class SteadyFlakeTests(AfterTestCase):
    """A green main run closes a `flaky:` issue once its test has gone STEADY_RUNS green runs."""

    OPENED = "2026-01-04T00:00:00Z"

    def closed(self, fake):
        return [w[1] for w in fake.writes if w[0] == "PATCH" and w[2].get("state") == "closed"]

    def test_thirty_green_runs_without_a_flake_close_the_issue_and_say_why(self):
        fake = FakeGitHub(make_run("success"), green=green_runs(30),
                          issues=[open_issue(85, "flaky: tests::slow", "flaky", self.OPENED)])
        _, out, _ = self.run_after(fake)
        self.assertEqual(self.closed(fake), ["issues/85"])
        (comment,) = fake.comments()
        for expected in ("No flake in 30 green main runs", "after 30 in a row", "new issue opens",
                         "actions/runs/101", "actions/runs/130"):
            self.assertIn(expected, comment)
        self.assertIn("closed #85", out)

    def test_twenty_nine_green_runs_keep_it_open(self):
        fake = FakeGitHub(make_run("success"), green=green_runs(29),
                          issues=[open_issue(85, "flaky: tests::slow", "flaky", self.OPENED)])
        self.run_after(fake)
        self.assertEqual(fake.writes, [])

    def test_the_count_starts_again_at_the_last_flake(self):
        runs = green_runs(40)
        # Flaked again after the 15th of 40 green runs: 25 since then.
        again = {"created_at": runs[25]["created_at"], "body": "Flaked again: x at abc1234"}
        human = {"created_at": runs[0]["created_at"], "body": "Still open: looking into it"}
        fake = FakeGitHub(make_run("success"), green=runs,
                          issues=[open_issue(85, "flaky: tests::slow", "flaky", self.OPENED,
                                             comments=[again, human])])
        self.run_after(fake)
        self.assertEqual(self.closed(fake), [])

    def test_a_test_that_flaked_in_this_run_stays_open(self):
        fake = FakeGitHub(make_run("success"), green=green_runs(30), artifacts=["flakes-x"],
                          issues=[open_issue(85, "flaky: tests::slow", "flaky", self.OPENED)])
        self.run_after(fake, files={"f.txt": "tests::slow\n"})
        self.assertEqual(self.closed(fake), [])
        (comment,) = fake.comments()
        self.assertTrue(comment.startswith("Flaked again: "))

    def test_an_issue_titled_by_hand_is_left_alone(self):
        fake = FakeGitHub(make_run("success"), green=green_runs(30),
                          issues=[open_issue(6, "Flaky test: slow [perf]", "flaky", self.OPENED)])
        self.run_after(fake)
        self.assertEqual(fake.writes, [])

    def test_only_the_newest_green_main_run_closes(self):
        issue = open_issue(85, "flaky: tests::slow", "flaky", self.OPENED)
        for run, completed in ((make_run("failure"), []),
                               (make_run("success", path=".github/workflows/check.yml",
                                         event="pull_request", branch="topic"), []),
                               (make_run("success"), [{"id": RUN_ID + 1}])):
            with self.subTest(run=run["path"], conclusion=run["conclusion"]):
                fake = FakeGitHub(run, jobs=[real_failure()], green=green_runs(30), issues=[issue],
                                  compare={"commits": []})
                fake.completed = completed
                self.run_after(fake)
                self.assertEqual(self.closed(fake), [])


class ForkRunTests(AfterTestCase):
    """A fork's run wrote its own artifacts: nothing from them becomes an issue."""

    def fork_run(self, conclusion="success", **kw):
        kw.setdefault("path", ".github/workflows/check.yml")
        kw.setdefault("event", "pull_request")
        kw.setdefault("head_repo", "evil/r")
        return make_run(conclusion, **kw)

    def test_fork_run_files_no_issues_and_downloads_nothing(self):
        fake = FakeGitHub(self.fork_run(), artifacts=["flakes-x"],
                          issues=[open_issue(7, "flaky: spam", "flaky")])
        _, out, gh = self.run_after(fake, files={"f.txt": "spam\nspam2\n"})
        gh.assert_not_called()
        self.assertEqual(fake.writes, [])
        self.assertFalse(any("artifacts" in p for _, p in fake.requested))
        self.assertIn("flakes: run 500 is from evil/r, not o/r; its artifacts are not read", out)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_failed_fork_run_files_no_issues_either(self):
        fake = FakeGitHub(self.fork_run("failure"), jobs=[real_failure()], artifacts=["flakes-x"])
        _, _, gh = self.run_after(fake, files={"f.txt": "spam\n"})
        gh.assert_not_called()
        self.assertEqual(fake.writes, [])

    def test_same_run_from_this_repository_is_unchanged(self):
        fake = FakeGitHub(self.fork_run(head_repo="o/r"), artifacts=["flakes-x"])
        self.run_after(fake, files={"f.txt": "tests::slow\n"})
        self.assertEqual([i["title"] for i in fake.opened()], ["flaky: tests::slow"])

    def test_repository_names_compare_without_case(self):
        fake = FakeGitHub(self.fork_run(head_repo="O/R"), artifacts=["flakes-x"])
        self.run_after(fake, files={"f.txt": "tests::slow\n"})
        self.assertEqual(len(fake.opened()), 1)

    def test_run_without_a_head_repository_is_foreign(self):
        fake = FakeGitHub(self.fork_run(head_repo=None), artifacts=["flakes-x"])
        _, out, gh = self.run_after(fake, files={"f.txt": "spam\n"})
        gh.assert_not_called()
        self.assertEqual(fake.writes, [])
        self.assertIn("unknown repository", out)

    def test_fork_name_in_the_log_is_cleaned(self):
        fake = FakeGitHub(self.fork_run(head_repo="evil/r\n::error::x"))
        _, out, _ = self.run_after(fake)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_fork_infra_failure_is_rerun_once(self):
        fake = FakeGitHub(self.fork_run("failure"), jobs=[infra_failure()], artifacts=["flakes-x"])
        self.run_after(fake)
        self.assertEqual(len(fake.posts("rerun-failed-jobs")), 1)
        fake = FakeGitHub(self.fork_run("failure", attempt=2), jobs=[infra_failure()])
        self.run_after(fake)
        self.assertEqual(fake.writes, [])

    def test_fork_branch_named_like_the_release_branch_is_not_a_main_run(self):
        # The fork's workflow may carry any name and its branch any name; only a
        # push to this repository makes a main run.
        for conclusion in ("failure", "success"):
            with self.subTest(conclusion=conclusion):
                run = self.fork_run(conclusion, path=".github/workflows/main.yml", branch="main")
                fake = FakeGitHub(run, jobs=[real_failure()],
                                  issues=[open_issue(5, "main is red", "main-red")])
                self.run_after(fake)
                self.assertEqual(fake.opened(), [])
                self.assertNotIn("PATCH", [w[0] for w in fake.writes])

    def test_cancelled_fork_run_reads_nothing_more(self):
        run = self.fork_run("cancelled", path=".github/workflows/main.yml", branch="main")
        fake = FakeGitHub(run, jobs=[real_failure()])
        self.run_after(fake)
        self.assertEqual(fake.requested, [("GET", f"actions/runs/{RUN_ID}")])


class RedMainTests(AfterTestCase):
    compare = {"commits": [
        {"sha": "1" * 40, "commit": {"author": {"name": "Ann"}, "message": "core: add x\n\nlong"}},
        {"sha": "2" * 40, "commit": {"author": {"name": "Bob"}, "message": "ui: fix y"}},
    ]}
    green = [{"id": 400, "head_sha": BASE}]

    def red_fake(self, **kw):
        return FakeGitHub(make_run("failure"), jobs=[real_failure("build / linux")],
                          green=self.green, compare=self.compare, **kw)

    def test_red_main_opens_the_issue(self):
        fake = self.red_fake()
        _, out, _ = self.run_after(fake)
        (issue,) = fake.opened()
        self.assertEqual(issue["title"], "main is red: build / linux")
        self.assertEqual(issue["labels"], ["main-red"])
        body = issue["body"]
        self.assertIn(RUN_URL, body)
        self.assertIn("build / linux", body)
        self.assertIn(f"{BASE[:7]}..{HEAD[:7]}", body)
        self.assertIn("1111111  Ann  core: add x", body)
        self.assertIn("2222222  Bob  ui: fix y", body)
        self.assertIn("Fix or revert within the window; revert by default.", body)
        self.assertIn(f"compare/{BASE}...{HEAD}", [p for _, p in fake.requested])
        self.assertIn("main-red: opened #", out)

    def test_second_red_comments_instead(self):
        fake = self.red_fake(issues=[open_issue(5, "main is red: build / linux", "main-red")])
        self.run_after(fake)
        self.assertEqual(fake.opened(), [])
        (comment,) = fake.comments()
        self.assertIn(RUN_URL, comment)
        self.assertIn("build / linux", comment)
        self.assertIn(f"{BASE[:7]}..{HEAD[:7]}", comment)

    def test_each_failed_check_gets_its_own_issue(self):
        fake = FakeGitHub(make_run("failure"), green=self.green, compare=self.compare,
                          jobs=[real_failure("build / linux"), real_failure("check / windows"),
                                job("check / gate", [step(1, "Set up job")])])
        self.run_after(fake)
        self.assertEqual([i["title"] for i in fake.opened()],
                         ["main is red: build / linux", "main is red: check / windows"])
        self.assertIn("` check / windows `", fake.opened()[1]["body"])
        self.assertNotIn("build / linux", fake.opened()[1]["body"])

    def test_a_check_that_passes_closes_its_issue_while_another_stays_red(self):
        passing = job("check / windows", [step(1, "Set up job"), step(2, "mise run windows")],
                      conclusion="success")
        fake = self.red_fake(issues=[open_issue(5, "main is red: check / windows", "main-red"),
                                     open_issue(6, "main is red: build / linux", "main-red")])
        fake.jobs.append(passing)
        self.run_after(fake)
        self.assertIn(("PATCH", "issues/5", {"state": "closed", "state_reason": "completed"}), fake.writes)
        self.assertNotIn("issues/6", [w[1] for w in fake.writes if w[0] == "PATCH"])
        self.assertEqual(fake.opened(), [])  # build / linux was commented on, not filed again

    def test_a_check_that_did_not_run_keeps_its_issue(self):
        fake = self.red_fake(issues=[open_issue(5, "main is red: check / windows", "main-red")])
        self.run_after(fake)
        self.assertNotIn("PATCH", [w[0] for w in fake.writes])

    def test_green_main_closes_every_check_issue(self):
        fake = FakeGitHub(make_run("success"),
                          issues=[open_issue(5, "main is red: check / windows", "main-red"),
                                  open_issue(6, "main is red: build / linux", "main-red")])
        self.run_after(fake)
        closed = [w[1] for w in fake.writes if w[0] == "PATCH"]
        self.assertEqual(closed, ["issues/5", "issues/6"])

    def test_a_red_run_with_no_failed_job_recorded_uses_the_bare_title(self):
        stopped_only = job("build", [step(1, "Set up job")], conclusion="cancelled")
        fake = FakeGitHub(make_run("failure"), jobs=[stopped_only], green=self.green, compare=self.compare)
        self.run_after(fake)
        (issue,) = fake.opened()
        self.assertEqual(issue["title"], "main is red")
        self.assertIn("none recorded", issue["body"])

    def test_a_hostile_job_name_is_cleaned_in_the_title(self):
        fake = FakeGitHub(make_run("failure"), green=self.green, compare=self.compare,
                          jobs=[real_failure("build\u202e / linux\n")])
        self.run_after(fake)
        self.assertEqual(fake.opened()[0]["title"], "main is red: build / linux")

    def test_green_main_closes_the_issue(self):
        fake = FakeGitHub(make_run("success"), issues=[open_issue(5, "main is red", "main-red")])
        self.run_after(fake)
        self.assertIn(("PATCH", "issues/5", {"state": "closed", "state_reason": "completed"}), fake.writes)
        (comment,) = fake.comments()
        self.assertIn(HEAD[:7], comment)
        self.assertIn(RUN_URL, comment)

    def test_green_main_without_issue_writes_nothing(self):
        fake = FakeGitHub(make_run("success"))
        self.run_after(fake)
        self.assertEqual(fake.writes, [])

    def test_check_workflow_failure_opens_no_main_red_issue(self):
        run = make_run("failure", path=".github/workflows/check.yml", event="pull_request")
        fake = FakeGitHub(run, jobs=[real_failure()])
        self.run_after(fake)
        self.assertEqual(fake.opened(), [])

    def test_check_workflow_success_leaves_main_red_open(self):
        run = make_run("success", path=".github/workflows/check.yml", event="pull_request")
        fake = FakeGitHub(run, issues=[open_issue(5, "main is red", "main-red")])
        self.run_after(fake)
        self.assertEqual(fake.writes, [])

    def test_main_workflow_on_other_event_is_not_main_red(self):
        fake = FakeGitHub(make_run("failure", event="schedule"), jobs=[real_failure()])
        self.run_after(fake)
        self.assertEqual(fake.opened(), [])

    def test_an_older_red_run_does_not_open_the_issue_after_a_newer_run_finished(self):
        fake = FakeGitHub(make_run("failure"), jobs=[real_failure()])
        fake.completed = [{"id": RUN_ID + 1, "conclusion": "success"}]
        self.run_after(fake)
        self.assertEqual(fake.opened(), [])

    def test_a_rerun_of_an_older_green_run_does_not_close_the_issue(self):
        fake = FakeGitHub(make_run("success"), issues=[open_issue(7, "main is red", "main-red")])
        fake.completed = [{"id": RUN_ID + 1, "conclusion": "failure"}]
        self.run_after(fake)
        self.assertEqual(fake.writes, [])

    def test_a_newer_cancelled_run_counts_as_newer(self):
        # It may be a timeout that is red; an old green run must not close its issue.
        fake = FakeGitHub(make_run("success"), issues=[open_issue(7, "main is red", "main-red")])
        fake.completed = [{"id": RUN_ID + 1, "conclusion": "cancelled"},
                          {"id": RUN_ID, "conclusion": "success"}]
        self.run_after(fake)
        self.assertEqual(fake.writes, [])

    def test_a_stopped_gate_job_is_still_the_gate(self):
        gate = job("check / gate", [step(1, "Set up job")], conclusion="cancelled")
        hung = job("build", [step(1, "Set up job"), step(2, "mise run build", "cancelled")],
                   conclusion="cancelled")
        fake = FakeGitHub(make_run("cancelled"), jobs=[hung, gate])
        self.run_after(fake)
        body = fake.opened()[0]["body"]
        self.assertIn("` build ` (timed out or cancelled)", body)
        self.assertNotIn("gate", body)

    def test_path_with_ref_suffix_still_identifies_the_workflow(self):
        run = make_run("failure", path=".github/workflows/main.yml@refs/heads/main")
        fake = FakeGitHub(run, jobs=[real_failure()], green=self.green, compare=self.compare)
        self.run_after(fake)
        self.assertEqual(fake.opened()[0]["title"], "main is red: check / linux")

    def test_workflow_identity_ignores_display_name(self):
        run = make_run("failure", path=".github/workflows/check.yml", event="push")
        run["name"] = "main"
        fake = FakeGitHub(run, jobs=[real_failure()])
        self.run_after(fake)
        self.assertEqual(fake.opened(), [])

    def test_workflow_named_like_main_is_not_main(self):
        self.assertEqual(after.workflow_file({"path": ".github/workflows/domain.yml"}), "domain.yml")

    def test_no_earlier_green_run_lists_the_head_commit_only(self):
        fake = FakeGitHub(make_run("failure"), jobs=[real_failure()], green=[])
        self.run_after(fake)
        body = fake.opened()[0]["body"]
        self.assertIn(f"`{HEAD[:7]}`", body)
        self.assertNotIn("..", body.split("Range:")[1].splitlines()[0])
        self.assertIn(f"{HEAD[:7]}  Ann  Fix it", body)
        self.assertFalse(any(p.startswith("compare/") for _, p in fake.requested))

    def test_base_is_the_newest_green_run_before_this_one(self):
        green = [{"id": 900, "head_sha": "9" * 40},   # newer than this run
                 {"id": 499, "head_sha": HEAD},      # same commit: not a base
                 {"id": 450, "head_sha": BASE},
                 {"id": 300, "head_sha": "3" * 40}]
        fake = FakeGitHub(make_run("failure"), green=green)
        with mock.patch.object(ghapi, "api", fake):
            self.assertEqual(after.last_green_sha(make_run("failure")), BASE)

    def test_failed_compare_falls_back_to_the_head_commit(self):
        fake = FakeGitHub(make_run("failure"), jobs=[real_failure()], green=self.green, compare=None)
        self.run_after(fake)
        body = fake.opened()[0]["body"]
        self.assertIn(f"{HEAD[:7]}  Ann  Fix it", body)

    def test_queries_use_main_when_ci_branch_is_unset_or_empty(self):
        for value in (None, "", "  "):
            with self.subTest(value=value):
                if value is not None:
                    os.environ["CI_BRANCH"] = value
                fake = self.red_fake()
                self.run_after(fake)
                paths = [p for _, p in fake.requested]
                self.assertTrue(any("runs?branch=main&status=success" in p for p in paths))
                self.assertTrue(any("runs?branch=main&status=completed" in p for p in paths))
                self.assertTrue(fake.opened()[0]["body"].startswith("main is red."))

    def test_commit_text_cannot_close_the_code_fence(self):
        compare = {"commits": [{"sha": "1" * 40, "commit": {
            "author": {"name": "Eve\x1b"}, "message": "```\n@owner x"}}]}
        fake = FakeGitHub(make_run("failure"), jobs=[real_failure()], green=self.green, compare=compare)
        self.run_after(fake)
        body = fake.opened()[0]["body"]
        fenced = body.split("```\n")[1]
        self.assertTrue(fenced.startswith("1111111  Eve"))  # subject was cut at the newline
        self.assertNotIn("\x1b", body)


class ReleaseBranchTests(AfterTestCase):
    """The release branch is `next`; the workflow file and the issue names stay `main`."""

    def setUp(self):
        super().setUp()
        os.environ["CI_BRANCH"] = "next"

    def next_fake(self, conclusion="failure", branch="next", **kw):
        return FakeGitHub(make_run(conclusion, branch=branch), jobs=[real_failure("build / linux")],
                          green=[{"id": 400, "head_sha": BASE}], compare=RedMainTests.compare, **kw)

    def test_queries_carry_the_release_branch(self):
        fake = self.next_fake()
        self.run_after(fake)
        paths = [p for _, p in fake.requested]
        self.assertTrue(any("runs?branch=next&status=success" in p for p in paths))
        self.assertTrue(any("runs?branch=next&status=completed" in p for p in paths))
        self.assertFalse(any("branch=main" in p for p in paths))

    def test_push_run_on_the_release_branch_is_handled(self):
        fake = self.next_fake()
        self.run_after(fake)
        (issue,) = fake.opened()
        self.assertEqual(issue["title"], "main is red: build / linux")  # identifiers do not change
        self.assertEqual(issue["labels"], ["main-red"])
        self.assertTrue(issue["body"].startswith("next is red."))
        self.assertNotIn("main is red", issue["body"])

    def test_push_run_on_another_branch_is_not_handled(self):
        fake = self.next_fake(branch="main")
        self.run_after(fake)
        self.assertEqual(fake.opened(), [])

    def test_green_run_on_the_release_branch_closes_naming_it(self):
        fake = FakeGitHub(make_run("success", branch="next"),
                          issues=[open_issue(5, "main is red", "main-red")])
        self.run_after(fake)
        (comment,) = fake.comments()
        self.assertTrue(comment.startswith(f"next is green again at {HEAD[:7]}"))
        self.assertIn(RUN_URL, comment)

    def test_green_run_on_another_branch_does_not_close(self):
        fake = FakeGitHub(make_run("success", branch="main"),
                          issues=[open_issue(5, "main is red", "main-red")])
        self.run_after(fake)
        self.assertEqual(fake.writes, [])

    def test_default_branch_run_is_not_the_release_run_without_the_variable(self):
        os.environ.pop("CI_BRANCH")
        fake = self.next_fake(branch="next")
        self.run_after(fake)
        self.assertEqual(fake.opened(), [])

    def test_branch_function(self):
        self.assertEqual(after.branch(), "next")
        os.environ["CI_BRANCH"] = " "
        self.assertEqual(after.branch(), "main")
        del os.environ["CI_BRANCH"]
        self.assertEqual(after.branch(), "main")


if __name__ == "__main__":
    unittest.main()
