import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Appended, not inserted first: .github/ci/select.py must not shadow the stdlib `select`.
sys.path.append(str(Path(__file__).resolve().parents[2] / ".github" / "ci"))

import candidate  # noqa: E402
import ghapi  # noqa: E402

SHA = "a" * 40
TAG_OBJECT = "e" * 40
MARK = candidate.MARKER


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeGitHub:
    """Stands in for ghapi.api / ghapi.gh and records what was done, in order."""

    def __init__(self, releases=None, assets=None, tags=(), downloads=None, annotated=()):
        self.releases = releases if releases is not None else []
        self.assets = assets if assets is not None else []
        self.tags = list(tags)
        self.annotated = set(annotated)  # tags that are tag objects, not lightweight
        self.downloads = downloads or {}
        self.events = []

    def api(self, path, method="GET", body=None, paginate=False):
        self.paths = getattr(self, "paths", []) + [path]
        if method == "GET" and path.startswith("releases?"):
            return self.releases
        if method == "GET" and path.startswith("git/matching-refs/tags/"):
            tag = path.split("/tags/", 1)[1].split("?")[0]
            return [
                {
                    "ref": f"refs/tags/{t}",
                    "object": {
                        "sha": TAG_OBJECT if t in self.annotated else SHA,
                        "type": "tag" if t in self.annotated else "commit",
                    },
                }
                for t in self.tags
                if t.startswith(tag)
            ]
        if method == "GET" and path == f"git/tags/{TAG_OBJECT}":
            return {"object": {"sha": SHA, "type": "commit"}}
        if method == "POST" and path == "actions/workflows/main.yml/dispatches":
            self.events.append(("dispatch", body))
            return None
        if method == "GET" and path.startswith("releases/") and "/assets" in path:
            return self.assets
        if method == "GET" and path.startswith("actions/workflows/main.yml/runs"):
            return {"workflow_runs": self.runs} if hasattr(self, "runs") else {"workflow_runs": []}
        if method == "DELETE" and path.startswith("releases/assets/"):
            asset_id = int(path.rsplit("/", 1)[1])
            name = next(a["name"] for a in self.assets if a["id"] == asset_id)
            self.events.append(("delete", name))
            return None
        if method == "POST" and path == "releases":
            self.events.append(("create", body))
            return {"id": 99, **body}
        if method == "PATCH" and path.startswith("releases/"):
            self.events.append(("patch", body))
            return {"id": int(path.split("/")[1]), **body}
        raise AssertionError(f"unexpected api call {method} {path}")

    def gh(self, *args, stdin=None, check=True):
        if args[:2] == ("release", "upload"):
            names = [Path(a).name for a in args[3:] if a != "--clobber"]
            self.events.append(("upload", names))
        elif args[:2] == ("release", "download"):
            directory = Path(args[args.index("--dir") + 1])
            for name, data in self.downloads.items():
                (directory / name).write_bytes(data)
            self.events.append(("download", args[2]))
        else:
            raise AssertionError(f"unexpected gh call {args}")
        return ""

    def kinds(self):
        return [e[0] for e in self.events]


def run(func, *args, **kwargs):
    """Run a command function capturing stdout and stderr; returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = candidate.main(*args, **kwargs) if func is None else func(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


def draft(tag="v0.2.0", sha_=SHA, body=None, id_=7, draft_=True):
    return {
        "id": id_,
        "draft": draft_,
        "tag_name": tag,
        "target_commitish": sha_,
        "body": body if body is not None else f"## {tag}\n\nCandidate: {sha_}\n{MARK}\n",
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "candidate" / "dist"
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        # The caller's settings must not change what the tests see.
        for key in ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_OUTPUT",
                    "CI_BRANCH", "CI_REPO", "RELEASE_NOTES"):
            os.environ.pop(key, None)
        os.environ["BUMP_KEY_SET"] = "true"  # the workflow sets it for the whole release job
        self.is_ancestor = True
        self.tree = Path(self.tmp.name) / "candidate"
        self.tracked_dist = False  # a worktree that already holds a non-empty dist/
        self.scratch_dirs = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(*a, **k):
            made = real_mkdtemp(*a, **k)
            self.scratch_dirs.append(Path(made))
            return made

        patcher = mock.patch.object(tempfile, "mkdtemp", mkdtemp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, fake, version="0.2.0", switch_notes="", git=None):
        """Patch every outside call; returns the git calls list."""
        self.git_calls = []
        subjects = getattr(self, "subjects", [])

        def fake_git(*args):
            self.git_calls.append(args)
            if args[0] == "rev-parse":
                return SHA + "\n"
            if args[:2] == ("worktree", "add"):
                # The real call checks the commit out; the tests only need the directory.
                tree = Path(args[3])
                tree.mkdir(parents=True)
                if self.tracked_dist:
                    (tree / "dist").mkdir()
                    (tree / "dist" / "keep.txt").write_text("tracked")
                return ""
            if args[:2] == ("worktree", "remove"):
                shutil.rmtree(args[-1])
                return ""
            if args[0] == "describe":
                raise RuntimeError("no tags")
            if args[0] == "log":
                return "\n".join(subjects) + "\n"
            return ""

        for target, value in (
            (ghapi, {"api": fake.api, "gh": fake.gh}),
            (
                candidate,
                {
                    "git": git or fake_git,
                    "git_ok": lambda *a: self.git_calls.append(a) or self.is_ancestor,
                    "mise_version": lambda: version,
                },
            ),
        ):
            for name, func in value.items():
                patcher = mock.patch.object(target, name, func)
                patcher.start()
                self.addCleanup(patcher.stop)
        sw = mock.patch.object(
            candidate,
            "switches",
            lambda *a: (self.switch_calls.append(a), switch_notes if a == ("notes",) else "")[1],
        )
        self.switch_calls = []
        sw.start()
        self.addCleanup(sw.stop)

    def write(self, name, data=b"x"):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / name).write_bytes(data)


class NotesTest(unittest.TestCase):
    def test_groups_by_area_in_first_appearance_order_other_last(self):
        notes = candidate.build_notes(
            "0.2.0",
            SHA,
            "v0.1.0",
            [
                "cli: add flag",
                "fix the thing",
                "core: speed up",
                "cli: rename flag",
                "this prefix is way too long to count as an area at all: x",
                "a b c d: four words",
            ],
        )
        self.assertEqual(
            notes,
            "## v0.2.0\n\n"
            "### cli\n- add flag\n- rename flag\n\n"
            "### core\n- speed up\n\n"
            "### Other\n- fix the thing\n"
            "- this prefix is way too long to count as an area at all: x\n"
            "- a b c d: four words\n\n"
            f"Candidate: {SHA}\n{MARK}\n",
        )

    def test_other_comes_last_even_when_it_appears_first(self):
        groups = candidate.group_subjects(["plain", "cli: a"])
        self.assertEqual([g[0] for g in groups], ["cli", "Other"])

    def test_no_commits(self):
        notes = candidate.build_notes("0.2.0", SHA, "v0.1.0", [])
        self.assertIn("No changes since v0.1.0.", notes)
        self.assertNotIn("###", notes)

    def test_no_changes_line_is_left_out_when_other_notes_exist(self):
        for kwargs in ({"switch_notes": "### Switches\n- shout"}, {"extra_notes": "perf table"}):
            notes = candidate.build_notes("0.2.0", SHA, "v0.1.0", [], **kwargs)
            self.assertNotIn("No changes", notes)
            self.assertTrue(notes.startswith("## v0.2.0\n\n"))
        blank = candidate.build_notes("0.2.0", SHA, "v0.1.0", [], "  \n", "\n")
        self.assertIn("No changes since v0.1.0.", blank)

    def test_switches_and_notes_md_are_appended_and_marker_is_last(self):
        notes = candidate.build_notes(
            "0.2.0", SHA, None, ["cli: a"], "### Switches\n- shout\n", "perf table\n"
        )
        self.assertLess(notes.index("### cli"), notes.index("### Switches"))
        self.assertLess(notes.index("### Switches"), notes.index("perf table"))
        self.assertLess(notes.index("perf table"), notes.index("Candidate:"))
        lines = notes.rstrip().splitlines()
        self.assertEqual(lines[-1], MARK)
        self.assertEqual(lines[-2], f"Candidate: {SHA}")

    def test_strip_candidate_lines_roundtrip(self):
        notes = candidate.build_notes("0.2.0", SHA, None, ["cli: a"], "", "extra")
        stripped = candidate.strip_candidate_lines(notes)
        self.assertNotIn("Candidate:", stripped)
        self.assertNotIn(MARK, stripped)
        self.assertTrue(stripped.rstrip().endswith("extra"))

    def test_next_patch(self):
        self.assertEqual(candidate.next_patch("0.2.9"), "0.2.10")
        with self.assertRaises(candidate.CandidateError):
            candidate.next_patch("0.2")
        with self.assertRaises(candidate.CandidateError):
            candidate.next_patch("1.0.0-rc1")


class SubjectsTest(unittest.TestCase):
    def subjects(self, log, version):
        with mock.patch.object(candidate, "git", lambda *args: log):
            return candidate.commit_subjects(SHA, "v0.1.0", version)

    def test_subjects_that_only_name_the_version_are_left_out(self):
        log = (
            "cli: add flag\nStart v0.2.0\nKiku v0.2.0\nRelease 0.2.0\n"
            "Start the engine sooner\n"
        )
        self.assertEqual(
            self.subjects(log, "0.2.0"), ["cli: add flag", "Start the engine sooner"]
        )

    def test_another_version_or_more_words_are_listed(self):
        log = "Start v0.1.1\nPrepare for the 0.2.0\nStart v0.2.01\nStart v0.2.0 properly\n"
        self.assertEqual(self.subjects(log, "0.2.0"), log.splitlines())

    def test_the_dot_in_the_version_is_literal(self):
        self.assertEqual(self.subjects("Start v0x2x0\n", "0.2.0"), ["Start v0x2x0"])


class DraftTest(Base):
    def test_two_marked_drafts_is_an_error_naming_them(self):
        fake = FakeGitHub(releases=[draft("v0.2.0", id_=1), draft("v0.3.0", id_=2)])
        self.install(fake)
        code, _, err = run(None, ["fetch", "--tree", str(self.tree)])
        self.assertEqual(code, 1)
        self.assertIn("v0.2.0", err)
        self.assertIn("v0.3.0", err)

    def test_unmarked_and_published_releases_are_ignored(self):
        fake = FakeGitHub(
            releases=[
                draft("v0.1.0", body="no marker", id_=1),
                draft("v0.0.9", draft_=False, id_=2),
                draft("v0.2.0", id_=3),
            ]
        )
        self.install(fake)
        self.assertEqual(candidate.standing_draft()["id"], 3)

    def test_base_asks_for_the_release_branch(self):
        for value, expected in ((None, "main"), ("", "main"), ("  ", "main"), (" next ", "next")):
            with self.subTest(value=value):
                os.environ.pop("CI_BRANCH", None)
                if value is not None:
                    os.environ["CI_BRANCH"] = value
                fake = FakeGitHub()
                self.install(fake)
                run(candidate.cmd_base, None)
                self.assertEqual(
                    fake.paths,
                    [f"actions/workflows/main.yml/runs?branch={expected}&status=success&per_page=1"],
                )

    def test_base_outputs_head_sha_or_nothing(self):
        gh_out = Path(self.tmp.name) / "out.txt"
        os.environ["GITHUB_OUTPUT"] = str(gh_out)
        fake = FakeGitHub()
        self.install(fake)
        self.assertEqual(run(candidate.cmd_base, None), (0, "", ""))
        self.assertFalse(gh_out.exists())
        fake.runs = [{"head_sha": SHA}]
        self.assertEqual(run(candidate.cmd_base, None), (0, f"base={SHA}\n", ""))
        self.assertEqual(gh_out.read_text(), f"base={SHA}\n")


class RecordTest(Base):
    def args(self):
        return ["record", "--dir", str(self.dir)]

    def test_released_version_is_no_candidate_and_no_failure(self):
        self.write("app.zip")
        # A draft is there too: it must stay untouched.
        fake = FakeGitHub(tags=["v0.2.0"], releases=[draft("v0.3.0")])
        self.install(fake)
        code, out, _ = run(None, self.args())
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                "candidate: v0.2.0 is already released; this commit is not a candidate "
                "(the release moves main to the next version)",
                "::notice title=No candidate::v0.2.0 is already released",
            ],
        )
        self.assertEqual(fake.events, [])
        self.assertFalse((self.dir / "candidate.json").exists())

    def test_prefix_tag_does_not_count(self):
        self.write("app.zip")
        fake = FakeGitHub(tags=["v0.2.0-rc1"])
        self.install(fake)
        self.assertEqual(run(None, self.args())[0], 0)

    def test_refuses_empty_dir_or_only_notes_and_manifest(self):
        self.write("NOTES.md")
        self.write("candidate.json", b"{}")
        fake = FakeGitHub()
        self.install(fake)
        code, _, err = run(None, self.args())
        self.assertEqual(code, 1)
        self.assertIn("no release files", err)
        self.assertEqual(fake.events, [])

    def test_refuses_subdirectory(self):
        self.write("app.zip")
        (self.dir / "sub").mkdir()
        self.install(FakeGitHub())
        code, _, err = run(None, self.args())
        self.assertEqual(code, 1)
        self.assertIn("sub", err)

    def test_refuses_names_github_would_rewrite(self):
        self.write("my app.zip")
        self.install(FakeGitHub())
        code, _, err = run(None, self.args())
        self.assertEqual(code, 1)
        self.assertIn("my app.zip", err)

    def test_creates_draft_and_orders_assets(self):
        self.write("b.zip", b"bbb")
        self.write("a.zip", b"aaa")
        self.write("NOTES.md", b"perf table")
        os.environ.update(GITHUB_SHA=SHA, GITHUB_RUN_ID="12", GITHUB_RUN_ATTEMPT="2")
        fake = FakeGitHub()
        self.subjects = ["cli: add flag", "fix"]
        self.install(fake, switch_notes="### Switches\n- shout")
        code, out, _ = run(None, self.args())
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"candidate: v0.2.0 {SHA[:7]} (2 files)")
        self.assertEqual(fake.kinds(), ["create", "upload", "upload"])
        kind, fields = fake.events[0]
        self.assertEqual(fields["tag_name"], "v0.2.0")
        self.assertEqual(fields["name"], "v0.2.0")
        self.assertEqual(fields["target_commitish"], SHA)
        self.assertIs(fields["draft"], True)
        self.assertIn("### cli\n- add flag", fields["body"])
        self.assertIn("### Switches", fields["body"])
        self.assertIn("perf table", fields["body"])
        self.assertTrue(fields["body"].rstrip().endswith(MARK))
        self.assertEqual(fake.events[1], ("upload", ["a.zip", "b.zip"]))
        self.assertEqual(fake.events[2], ("upload", ["candidate.json"]))
        manifest = json.loads((self.dir / "candidate.json").read_text())
        self.assertEqual(list(manifest), ["sha", "version", "run_id", "run_attempt", "files"])
        self.assertEqual(manifest["run_id"], 12)
        self.assertEqual(manifest["run_attempt"], 2)
        self.assertEqual(manifest["files"], {"a.zip": sha(b"aaa"), "b.zip": sha(b"bbb")})

    def test_no_env_uses_git_head_and_zero_run_ids(self):
        self.write("a.zip")
        fake = FakeGitHub()
        self.install(fake)
        run(None, self.args())
        manifest = json.loads((self.dir / "candidate.json").read_text())
        self.assertEqual(manifest["sha"], SHA)
        self.assertEqual((manifest["run_id"], manifest["run_attempt"]), (0, 0))

    def test_updates_existing_draft_and_deletes_manifest_first(self):
        self.write("a.zip")
        old = draft("v0.2.0", sha_="b" * 40)
        fake = FakeGitHub(
            releases=[old],
            assets=[
                {"id": 1, "name": "a.zip"},
                {"id": 2, "name": "gone.zip"},
                {"id": 3, "name": "candidate.json"},
            ],
        )
        self.install(fake)
        code, _, _ = run(None, self.args())
        self.assertEqual(code, 0)
        self.assertEqual(
            fake.events[1:],
            [
                ("delete", "candidate.json"),
                ("delete", "a.zip"),
                ("delete", "gone.zip"),
                ("upload", ["a.zip"]),
                ("upload", ["candidate.json"]),
            ],
        )
        self.assertEqual(fake.events[0][0], "patch")
        self.assertEqual(fake.events[0][1]["target_commitish"], SHA)
        self.assertNotIn("create", fake.kinds())

    def test_stale_signed_manifest_asset_is_removed_after_candidate_json(self):
        self.write("a.zip")
        self.write("signed.json", b"{}")  # a leftover in the dir is not a release file
        fake = FakeGitHub(
            releases=[draft("v0.2.0")],
            assets=[
                {"id": 1, "name": "a.zip"},
                {"id": 2, "name": "signed.json"},
                {"id": 3, "name": "candidate.json"},
            ],
        )
        self.install(fake)
        code, _, _ = run(None, self.args())
        self.assertEqual(code, 0)
        deletes = [e[1] for e in fake.events if e[0] == "delete"]
        self.assertEqual(deletes[0], "candidate.json")
        self.assertIn("signed.json", deletes)
        self.assertEqual(fake.events[-2:], [("upload", ["a.zip"]), ("upload", ["candidate.json"])])

    def test_last_tag_bounds_the_log(self):
        self.write("a.zip")

        def fake_git(*args):
            if args[0] == "describe":
                return "v0.1.0\n"
            if args[0] == "log":
                self.log_args = args
                return "cli: x\n"
            return SHA + "\n"

        self.install(FakeGitHub(), git=fake_git)
        run(None, self.args())
        self.assertEqual(self.log_args[-1], f"v0.1.0..{SHA}")
        self.assertIn("--no-merges", self.log_args)


class FetchTest(Base):
    def files(self, **over):
        data = {"a.zip": b"aaa", "b.zip": b"bbb"}
        manifest = {
            "sha": SHA,
            "version": "0.2.0",
            "run_id": 1,
            "run_attempt": 1,
            "files": {n: sha(d) for n, d in data.items()},
        }
        downloads = {**data, "candidate.json": json.dumps(manifest).encode()}
        for key, value in over.items():
            if value is None:
                downloads.pop(key)
            else:
                downloads[key] = value
        return downloads

    def fetch(self, downloads, release=None):
        fake = FakeGitHub(releases=[release or draft()], downloads=downloads)
        self.install(fake)
        return run(None, ["fetch", "--tree", str(self.tree)])

    def test_passes_and_outputs(self):
        gh_out = Path(self.tmp.name) / "out.txt"
        os.environ["GITHUB_OUTPUT"] = str(gh_out)
        code, out, _ = self.fetch(self.files())
        self.assertEqual(code, 0)
        self.assertEqual(
            gh_out.read_text().splitlines(),
            ["published=false", f"sha={SHA}", "version=0.2.0", "tag=v0.2.0"],
        )
        self.assertIn("tag=v0.2.0", out)

    def test_worktree_at_the_candidate_with_files_and_manifest_in_dist(self):
        signed = {"a.zip": b"A-signed"}
        downloads = self.files(**{"signed.json": self.signed(signed)})
        code, _, err = self.fetch(downloads)
        self.assertEqual(code, 0, err)
        self.assertIn(("worktree", "add", "--detach", str(self.tree), SHA), self.git_calls)
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()),
            ["a.zip", "b.zip", "candidate.json", "signed.json"],
        )
        self.assertEqual((self.dir / "a.zip").read_bytes(), b"aaa")
        self.assertTrue(all(not d.exists() for d in self.scratch_dirs))
        self.assertEqual(len(self.scratch_dirs), 1)

    def test_failed_verification_leaves_no_worktree_and_no_scratch_dir(self):
        code, _, _ = self.fetch(self.files(**{"a.zip": b"tampered"}))
        self.assertEqual(code, 1)
        self.assertFalse(self.tree.exists())
        self.assertFalse([c for c in self.git_calls if c[0] == "worktree"])
        self.assertTrue(self.scratch_dirs)
        self.assertTrue(all(not d.exists() for d in self.scratch_dirs))

    def test_download_failure_removes_the_scratch_dir(self):
        fake = FakeGitHub(releases=[draft()])
        fake.gh = mock.Mock(side_effect=RuntimeError("network"))
        self.install(fake)
        code, _, _ = run(None, ["fetch", "--tree", str(self.tree)])
        self.assertEqual(code, 1)
        self.assertTrue(all(not d.exists() for d in self.scratch_dirs))

    def test_existing_tree_is_refused(self):
        self.tree.mkdir()
        code, _, err = self.fetch(self.files())
        self.assertEqual(code, 1)
        self.assertIn(f"{self.tree} already exists; remove it and run fetch again", err)
        self.assertFalse([c for c in self.git_calls if c[0] == "worktree"])
        self.assertTrue(all(not d.exists() for d in self.scratch_dirs))

    def test_tracked_nonempty_dist_is_refused_and_the_worktree_removed(self):
        self.tracked_dist = True
        code, _, err = self.fetch(self.files())
        self.assertEqual(code, 1)
        self.assertIn("commits a dist/ folder", err)
        self.assertFalse(self.tree.exists())

    def test_no_candidate(self):
        self.install(FakeGitHub())
        code, _, err = run(None, ["fetch", "--tree", str(self.tree)])
        self.assertEqual(code, 1)
        self.assertIn("no candidate: main has not been green since the last release", err)

    def test_missing_manifest(self):
        code, _, err = self.fetch(self.files(**{"candidate.json": None}))
        self.assertEqual(code, 1)
        self.assertIn("candidate.json is missing", err)

    def test_missing_file(self):
        code, _, err = self.fetch(self.files(**{"b.zip": None}))
        self.assertEqual(code, 1)
        self.assertIn("missing file: b.zip", err)

    def test_wrong_hash(self):
        code, _, err = self.fetch(self.files(**{"a.zip": b"tampered"}))
        self.assertEqual(code, 1)
        self.assertIn("hash differs: a.zip", err)

    def test_extra_file(self):
        code, _, err = self.fetch(self.files(**{"evil.exe": b"x"}))
        self.assertEqual(code, 1)
        self.assertIn("unlisted file: evil.exe", err)

    def test_sha_mismatch_with_draft(self):
        code, _, err = self.fetch(self.files(), release=draft(sha_="c" * 40))
        self.assertEqual(code, 1)
        self.assertIn("differs from the draft", err)

    def signed(self, files, sha_=SHA):
        return json.dumps({"sha": sha_, "files": {n: sha(d) for n, d in files.items()}}).encode()

    def test_all_files_signed(self):
        signed = {"a.zip": b"A-signed", "b.zip": b"B-signed", "a.zip.sha256": b"sum"}
        downloads = self.files(**signed, **{"signed.json": self.signed(signed)})
        code, _, err = self.fetch(downloads)
        self.assertEqual(code, 0, err)
        self.assertTrue((self.dir / "signed.json").exists())

    def test_partly_signed_with_a_signed_file_missing_from_the_draft(self):
        # a.zip got replaced, b.zip not yet; the signature file never made it up.
        signed = {"a.zip": b"A-signed", "b.zip": b"B-signed", "a.zip.sha256": b"sum"}
        downloads = self.files(**{"a.zip": b"A-signed", "signed.json": self.signed(signed)})
        code, _, err = self.fetch(downloads)
        self.assertEqual(code, 0, err)
        self.assertTrue((self.dir / "signed.json").exists())
        self.assertEqual((self.dir / "a.zip").read_bytes(), b"A-signed")
        self.assertEqual((self.dir / "b.zip").read_bytes(), b"bbb")

    def signed_lists(self, files, sha_=SHA):
        """A signed.json in the list shape: `files` maps a name to the contents of every attempt."""
        return json.dumps(
            {"sha": sha_, "files": {n: [sha(d) for d in datas] for n, datas in files.items()}}
        ).encode()

    def test_hash_from_any_attempt_in_the_list_passes(self):
        lists = {"a.zip": [b"A-try1", b"A-try2"], "b.zip": [b"B-try1"], "x.sha256": [b"s1", b"s2"]}
        downloads = self.files(
            **{"a.zip": b"A-try1", "x.sha256": b"s2", "signed.json": self.signed_lists(lists)}
        )
        code, _, err = self.fetch(downloads)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.dir / "signed.json").read_bytes(), downloads["signed.json"])

    def test_hash_in_neither_list_nor_candidate_fails(self):
        lists = {"a.zip": [b"A-try1", b"A-try2"]}
        downloads = self.files(**{"a.zip": b"A-try3", "signed.json": self.signed_lists(lists)})
        code, _, err = self.fetch(downloads)
        self.assertEqual(code, 1)
        self.assertIn("hash differs: a.zip", err)

    def test_extra_file_must_match_a_hash_in_its_list(self):
        lists = {"x.sha256": [b"s1", b"s2"]}
        downloads = self.files(**{"x.sha256": b"s3", "signed.json": self.signed_lists(lists)})
        code, _, err = self.fetch(downloads)
        self.assertEqual(code, 1)
        self.assertIn("hash differs: x.sha256", err)

    def test_malformed_hash_list_fails(self):
        bad = json.dumps({"sha": SHA, "files": {"a.zip": [1, 2]}}).encode()
        code, _, err = self.fetch(self.files(**{"signed.json": bad}))
        self.assertEqual(code, 1)
        self.assertIn("signed.json is malformed", err)

    def test_old_string_shape_still_passes(self):
        downloads = self.files(**{"a.zip": b"A-signed", "signed.json": self.signed({"a.zip": b"A-signed"})})
        code, _, err = self.fetch(downloads)
        self.assertEqual(code, 0, err)

    def test_file_matching_neither_hash_fails(self):
        signed = {"a.zip": b"A-signed"}
        downloads = self.files(**{"a.zip": b"other", "signed.json": self.signed(signed)})
        code, _, err = self.fetch(downloads)
        self.assertEqual(code, 1)
        self.assertIn("hash differs: a.zip", err)

    def test_listed_file_missing_still_fails_with_signed_manifest(self):
        downloads = self.files(**{"b.zip": None, "signed.json": self.signed({"b.zip": b"B-signed"})})
        code, _, err = self.fetch(downloads)
        self.assertEqual(code, 1)
        self.assertIn("missing file: b.zip", err)

    def test_extra_file_not_in_signed_manifest_fails(self):
        downloads = self.files(**{"evil.exe": b"x", "signed.json": self.signed({"a.zip": b"A-signed"})})
        code, _, err = self.fetch(downloads)
        self.assertEqual(code, 1)
        self.assertIn("unlisted file: evil.exe", err)

    def test_extra_file_with_the_wrong_signed_hash_fails(self):
        downloads = self.files(**{"x.sha256": b"changed", "signed.json": self.signed({"x.sha256": b"sum"})})
        code, _, err = self.fetch(downloads)
        self.assertEqual(code, 1)
        self.assertIn("hash differs: x.sha256", err)

    def test_signed_manifest_for_another_commit_fails(self):
        downloads = self.files(**{"signed.json": self.signed({}, sha_="c" * 40)})
        code, _, err = self.fetch(downloads)
        self.assertEqual(code, 1)
        self.assertIn("signed.json sha", err)

    def test_release_branch_is_named_by_ci_branch(self):
        os.environ["CI_BRANCH"] = "next"
        code, _, err = self.fetch(self.files())
        self.assertEqual(code, 0, err)
        self.assertIn(("fetch", "origin", "next"), self.git_calls)
        self.assertIn(("merge-base", "--is-ancestor", SHA, "origin/next"), self.git_calls)

    def test_candidate_not_on_the_named_branch_fails(self):
        os.environ["CI_BRANCH"] = "next"
        self.is_ancestor = False
        code, _, err = self.fetch(self.files())
        self.assertEqual(code, 1)
        self.assertIn(f"the candidate {SHA[:7]} is not on next", err)

    def test_bump_key_variable_absent_false_and_true(self):
        for value, ok in ((None, True), ("true", True), ("false", False), ("", False)):
            with self.subTest(value=value):
                os.environ.pop("BUMP_KEY_SET", None)
                if value is not None:
                    os.environ["BUMP_KEY_SET"] = value
                shutil.rmtree(self.tree, ignore_errors=True)
                code, _, err = self.fetch(self.files())
                self.assertEqual(code == 0, ok, err)
                if not ok:
                    self.assertIn("BUMP_KEY is not set", err)

    def test_candidate_not_on_main_fails(self):
        self.is_ancestor = False
        code, _, err = self.fetch(self.files())
        self.assertEqual(code, 1)
        self.assertIn(f"the candidate {SHA[:7]} is not on main", err)
        self.assertIn(("fetch", "origin", "main"), self.git_calls)
        self.assertIn(("merge-base", "--is-ancestor", SHA, "origin/main"), self.git_calls)


class BumpKeyGateTest(Base):
    """A missing deploy key stops the release at fetch, before any GitHub call."""

    def test_fetch_refuses_without_the_key_on_both_paths(self):
        for releases, tags in (([draft()], []), ([draft("v0.2.0", draft_=False)], ["v0.2.0"])):
            with self.subTest(releases=len(releases), draft=releases[0]["draft"]):
                fake = FakeGitHub(releases=releases, tags=tags)
                self.install(fake)
                api = mock.Mock(side_effect=AssertionError("GitHub was called"))
                for name in ("api", "gh"):
                    patcher = mock.patch.object(ghapi, name, api)
                    patcher.start()
                    self.addCleanup(patcher.stop)
                for value in ("false", ""):
                    os.environ["BUMP_KEY_SET"] = value
                    code, _, err = run(None, ["fetch", "--tree", str(self.tree)])
                    self.assertEqual(code, 1)
                    self.assertIn("BUMP_KEY is not set", err)
                    self.assertFalse(self.tree.exists())
                api.assert_not_called()
                mock.patch.stopall()

    def test_dry_runs_do_not_need_the_key(self):
        os.environ.pop("BUMP_KEY_SET")
        self.write("a.zip")
        self.write(
            "candidate.json", json.dumps({"sha": SHA, "version": "0.2.0", "files": {}}).encode()
        )
        fake = FakeGitHub(releases=[draft()])
        self.install(fake, version="0.2.0")
        self.assertEqual(run(None, ["publish", "--dir", str(self.dir), "--dry-run"])[0], 0)
        self.assertEqual(run(None, ["bump", "--released", "0.2.0", "--dry-run"])[0], 0)


class FetchResumeTest(Base):
    """No draft: a release that published but did not finish is resumed, not refetched."""

    def resume(self, fake, version="0.2.0"):
        self.install(fake, version=version)
        gh_out = Path(self.tmp.name) / "out.txt"
        os.environ["GITHUB_OUTPUT"] = str(gh_out)
        result = run(None, ["fetch", "--tree", str(self.tree)])
        return result, gh_out

    def published(self, tag="v0.2.0"):
        return draft(tag, draft_=False, body="## notes\n")

    def test_published_release_resumes_without_download(self):
        fake = FakeGitHub(releases=[self.published()], tags=["v0.2.0"])
        (code, out, _), gh_out = self.resume(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.events, [])
        self.assertFalse(self.tree.exists())
        self.assertEqual(self.scratch_dirs, [])
        self.assertIn("release: v0.2.0 is already published; only the bump is left", out)
        self.assertEqual(
            gh_out.read_text().splitlines(),
            ["published=true", f"sha={SHA}", "version=0.2.0", "tag=v0.2.0"],
        )

    def test_resume_removes_leftover_manifest_assets(self):
        fake = FakeGitHub(
            releases=[self.published()],
            tags=["v0.2.0"],
            assets=[
                {"id": 1, "name": "candidate.json"},
                {"id": 2, "name": "a.zip"},
                {"id": 3, "name": "signed.json"},
            ],
        )
        (code, _, _), _ = self.resume(fake)
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted(e[1] for e in fake.events if e[0] == "delete"),
            ["candidate.json", "signed.json"],
        )

    def test_resume_cleanup_failure_only_warns(self):
        class Stubborn(FakeGitHub):
            def api(self, path, method="GET", body=None, paginate=False):
                if method == "DELETE":
                    raise RuntimeError("HTTP 500")
                return super().api(path, method, body, paginate)

        fake = Stubborn(
            releases=[self.published()], tags=["v0.2.0"], assets=[{"id": 1, "name": "candidate.json"}]
        )
        (code, out, _), gh_out = self.resume(fake)
        self.assertEqual(code, 0)
        self.assertIn("::warning", out)
        self.assertIn("published=true", gh_out.read_text())

    def test_a_published_release_for_the_trees_version_wins_over_any_draft(self):
        unrelated = draft("v0.3.0", sha_="d" * 40)
        fake = FakeGitHub(releases=[unrelated, self.published()], tags=["v0.2.0"])
        (code, out, _), gh_out = self.resume(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.events, [])  # nothing downloaded, the draft untouched
        self.assertFalse(self.tree.exists())
        self.assertIn("published=true", gh_out.read_text())
        self.assertIn("release: v0.2.0 is already published", out)

    def test_an_unreleased_tree_version_takes_the_normal_path(self):
        downloads = FetchTest.files(self)
        fake = FakeGitHub(
            releases=[self.published("v0.1.0"), draft("v0.2.0")],
            tags=["v0.1.0"],
            downloads=downloads,
        )
        (code, _, err), gh_out = self.resume(fake)
        self.assertEqual(code, 0, err)
        self.assertIn("published=false", gh_out.read_text())
        self.assertEqual(fake.events, [("download", "v0.2.0")])

    def test_the_tree_version_is_read_before_any_release_is_looked_at(self):
        order = []
        fake = FakeGitHub(releases=[self.published()], tags=["v0.2.0"])
        real_api = fake.api
        fake.api = lambda path, *a, **k: (
            order.append(path.split("?")[0]) if path.startswith("releases?") else None,
            real_api(path, *a, **k),
        )[1]
        self.install(fake)
        mock.patch.object(candidate, "mise_version", lambda: order.append("version") or "0.2.0").start()
        self.addCleanup(mock.patch.stopall)
        run(None, ["fetch", "--tree", str(self.tree)])
        self.assertEqual(order[:2], ["version", "releases"])

    def test_dry_run_removes_no_manifests_on_the_resume_path(self):
        assets = [{"id": 1, "name": "candidate.json"}, {"id": 3, "name": "signed.json"}]
        fake = FakeGitHub(releases=[self.published()], tags=["v0.2.0"], assets=assets)
        with mock.patch.dict(os.environ, {"DRY_RUN": "1"}):
            (code, out, _), _ = self.resume(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.events, [])
        self.assertIn("dry run: would remove the manifests from v0.2.0", out.splitlines())

    def test_dry_run_is_quiet_when_no_manifests_are_left(self):
        fake = FakeGitHub(releases=[self.published()], tags=["v0.2.0"])
        with mock.patch.dict(os.environ, {"DRY_RUN": "1"}):
            (code, out, _), _ = self.resume(fake)
        self.assertEqual(code, 0)
        self.assertNotIn("would remove", out)

    def test_annotated_tag_resolves_to_its_commit(self):
        fake = FakeGitHub(
            releases=[self.published()], tags=["v0.2.0"], annotated=["v0.2.0"]
        )
        (code, _, _), gh_out = self.resume(fake)
        self.assertEqual(code, 0)
        self.assertIn(f"sha={SHA}", gh_out.read_text().splitlines())

    def test_current_version_not_released_still_fails(self):
        fake = FakeGitHub(releases=[self.published("v0.1.0")], tags=["v0.1.0"])
        (code, _, err), gh_out = self.resume(fake, version="0.2.0")
        self.assertEqual(code, 1)
        self.assertIn("no candidate: main has not been green since the last release", err)
        self.assertFalse(gh_out.exists())

    def test_tag_without_a_published_release_still_fails(self):
        fake = FakeGitHub(releases=[], tags=["v0.2.0"])
        (code, _, err), _ = self.resume(fake)
        self.assertEqual(code, 1)
        self.assertIn("no candidate", err)

    def test_bump_after_resume_uses_the_resumed_version(self):
        fake = FakeGitHub(releases=[self.published()], tags=["v0.2.0"])
        (code, _, _), gh_out = self.resume(fake)
        self.assertEqual(code, 0)
        released = dict(l.split("=", 1) for l in gh_out.read_text().splitlines())["version"]
        # main still carries 0.2.0 (the bump never landed): the bump behaves as usual.
        os.environ["BUMP_KEY_SET"] = "true"
        calls = []
        for name, func in (
            ("git", lambda *a: calls.append(a) or ""),
            ("mise_bump", lambda v: calls.append(("bump", v))),
        ):
            patcher = mock.patch.object(candidate, name, func)
            patcher.start()
            self.addCleanup(patcher.stop)
        code, out, _ = run(None, ["bump", "--released", released])
        self.assertEqual(code, 0)
        self.assertIn(("bump", "0.2.1"), calls)
        self.assertEqual(fake.events, [])


class PublishTest(Base):
    def setUp(self):
        super().setUp()
        self.write("a.zip", b"signed")
        self.write("candidate.json", json.dumps({"sha": SHA, "version": "0.2.0", "files": {}}).encode())
        self.write("NOTES.md")
        body = f"## v0.2.0\n\n- thing\n\nCandidate: {SHA}\n{MARK}\n"
        self.release = draft(body=body)

    def test_dry_run_changes_nothing(self):
        fake = FakeGitHub(releases=[self.release])
        self.install(fake)
        code, out, _ = run(None, ["publish", "--dir", str(self.dir), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.events, [])
        self.assertEqual(self.switch_calls, [])
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(line.startswith("dry run: would ") for line in lines))
        self.assertFalse((self.dir / "signed.json").exists())

    MANIFEST_ASSETS = [
        {"id": 1, "name": "candidate.json"},
        {"id": 2, "name": "a.zip"},
        {"id": 3, "name": "signed.json"},
    ]

    def test_publishes_last_and_strips_trailing_lines(self):
        fake = FakeGitHub(releases=[self.release], assets=self.MANIFEST_ASSETS)
        self.install(fake)
        code, _, _ = run(None, ["publish", "--dir", str(self.dir)])
        self.assertEqual(code, 0)
        self.assertEqual(fake.events[0], ("upload", ["signed.json"]))
        self.assertEqual(fake.events[1], ("upload", ["a.zip"]))
        kind, body = fake.events[2]
        self.assertEqual(kind, "patch")
        self.assertIs(body["draft"], False)
        self.assertEqual(body["body"], "## v0.2.0\n\n- thing\n")
        self.assertEqual(fake.kinds(), ["upload", "upload", "patch", "delete", "delete"])
        self.assertEqual(
            sorted(e[1] for e in fake.events[3:]), ["candidate.json", "signed.json"]
        )

    def publish_body(self, **env):
        fake = FakeGitHub(releases=[self.release])
        self.install(fake)
        with mock.patch.dict(os.environ, env):
            code, _, err = run(None, ["publish", "--dir", str(self.dir)])
        self.assertEqual(code, 0, err)
        return next(e[1]["body"] for e in fake.events if e[0] == "patch")

    def test_release_note_is_the_paragraph_after_the_heading(self):
        body = self.publish_body(RELEASE_NOTES="  Big *change*: read me.\nSecond line.\n\n")
        self.assertEqual(
            body, "## v0.2.0\n\nBig *change*: read me.\nSecond line.\n\n- thing\n"
        )

    def test_no_release_note_leaves_the_body_alone(self):
        plain = "## v0.2.0\n\n- thing\n"
        self.assertEqual(self.publish_body(), plain)
        self.assertEqual(self.publish_body(RELEASE_NOTES=""), plain)
        self.assertEqual(self.publish_body(RELEASE_NOTES=" \n\t\n"), plain)

    def test_dry_run_reports_the_release_note(self):
        fake = FakeGitHub(releases=[self.release])
        self.install(fake)
        with mock.patch.dict(os.environ, {"RELEASE_NOTES": "  hello there \n"}):
            code, out, _ = run(None, ["publish", "--dir", str(self.dir), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("dry run: would add the release note (11 characters)", out.splitlines())
        self.assertEqual(fake.events, [])

    def test_signed_manifest_records_the_files_as_they_are_now(self):
        fake = FakeGitHub(releases=[self.release])
        self.install(fake)
        run(None, ["publish", "--dir", str(self.dir)])
        signed = json.loads((self.dir / "signed.json").read_text())
        self.assertEqual(signed, {"sha": SHA, "files": {"a.zip": [sha(b"signed")]}})

    def test_existing_signed_manifest_is_unioned(self):
        self.write("b.zip", b"b-now")
        self.write(
            "signed.json",
            json.dumps(
                {
                    "sha": SHA,
                    "files": {
                        "a.zip": [sha(b"a-try1")],  # earlier attempt, different bytes
                        "b.zip": sha(b"b-now"),  # old string shape, same bytes as now
                        "gone.sha256": [sha(b"g")],  # only in the old file: kept
                    },
                }
            ).encode(),
        )
        fake = FakeGitHub(releases=[self.release])
        self.install(fake)
        code, _, err = run(None, ["publish", "--dir", str(self.dir)])
        self.assertEqual(code, 0, err)
        signed = json.loads((self.dir / "signed.json").read_text())
        self.assertEqual(
            signed["files"],
            {
                "a.zip": [sha(b"a-try1"), sha(b"signed")],
                "b.zip": [sha(b"b-now")],
                "gone.sha256": [sha(b"g")],
            },
        )
        self.assertEqual(fake.events[0], ("upload", ["signed.json"]))

    def test_rerunning_publish_does_not_duplicate_hashes(self):
        fake = FakeGitHub(releases=[self.release])
        self.install(fake)
        run(None, ["publish", "--dir", str(self.dir)])
        run(None, ["publish", "--dir", str(self.dir)])
        signed = json.loads((self.dir / "signed.json").read_text())
        self.assertEqual(signed["files"], {"a.zip": [sha(b"signed")]})

    def test_signed_manifest_of_another_commit_is_refused(self):
        self.write("signed.json", json.dumps({"sha": "c" * 40, "files": {}}).encode())
        fake = FakeGitHub(releases=[self.release])
        self.install(fake)
        code, _, err = run(None, ["publish", "--dir", str(self.dir)])
        self.assertEqual(code, 1)
        self.assertIn("signed.json sha", err)
        self.assertEqual(fake.events, [])

    def test_failed_manifest_cleanup_only_warns(self):
        class Stubborn(FakeGitHub):
            def api(self, path, method="GET", body=None, paginate=False):
                if method == "DELETE":
                    raise RuntimeError("HTTP 500")
                return super().api(path, method, body, paginate)

        fake = Stubborn(releases=[self.release], assets=self.MANIFEST_ASSETS)
        self.install(fake)
        code, out, _ = run(None, ["publish", "--dir", str(self.dir)])
        self.assertEqual(code, 0)
        self.assertIn("::warning", out)
        self.assertIn("published: v0.2.0", out)
        self.assertIn("patch", fake.kinds())

    def test_a_failure_before_the_patch_leaves_nothing_deleted(self):
        fake = FakeGitHub(releases=[self.release], assets=self.MANIFEST_ASSETS)
        self.install(fake)
        mock.patch.object(candidate, "switches", mock.Mock(side_effect=RuntimeError("boom"))).start()
        self.addCleanup(mock.patch.stopall)
        code, _, err = run(None, ["publish", "--dir", str(self.dir)])
        self.assertEqual(code, 1)
        self.assertNotIn("patch", fake.kinds())
        self.assertNotIn("delete", fake.kinds())

    def test_shipped_runs_between_upload_and_publish(self):
        seen = []
        fake = FakeGitHub(releases=[self.release])
        self.install(fake)
        patcher = mock.patch.object(
            candidate, "switches", lambda *a: seen.append(list(fake.kinds())) or ""
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        run(None, ["publish", "--dir", str(self.dir)])
        self.assertEqual(seen, [["upload", "upload"]])

    def test_rerun_with_tag_and_no_draft_exits_zero(self):
        fake = FakeGitHub(releases=[], tags=["v0.2.0"])
        self.install(fake)
        code, out, _ = run(None, ["publish", "--dir", str(self.dir)])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "already published: v0.2.0")
        self.assertEqual(fake.events, [])

    def test_no_draft_and_no_tag_fails(self):
        fake = FakeGitHub()
        self.install(fake)
        code, _, err = run(None, ["publish", "--dir", str(self.dir)])
        self.assertEqual(code, 1)
        self.assertIn("nothing to publish", err)

    def test_replaced_candidate_is_refused(self):
        fake = FakeGitHub(releases=[draft(sha_="d" * 40)])
        self.install(fake)
        code, _, err = run(None, ["publish", "--dir", str(self.dir)])
        self.assertEqual(code, 1)
        self.assertIn("rerun the release", err)
        self.assertEqual(fake.events, [])


class BumpTest(Base):
    def setUp(self):
        super().setUp()
        os.environ["BUMP_KEY_SET"] = "true"

    def install_git(self, version, push_results=()):
        """Patch git and the version; `push_results` lists errors (or None) per push."""
        self.calls = []
        self.fake = FakeGitHub()
        pushes = list(push_results)
        state = {"version": version}

        def fake_git(*args):
            self.calls.append(args)
            if args[0] == "push":
                result = pushes.pop(0) if pushes else None
                if result:
                    raise RuntimeError(result)
            return ""

        for target, name, func in (
            (candidate, "git", fake_git),
            (candidate, "mise_version", lambda: state["version"]),
            (candidate, "mise_bump", lambda v: self.calls.append(("bump", v))),
            (ghapi, "api", self.fake.api),
        ):
            patcher = mock.patch.object(target, name, func)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_skips_when_main_moved_to_another_version(self):
        self.install_git("0.3.0")
        code, out, _ = run(None, ["bump", "--released", "0.2.0"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "main already carries 0.3.0; nothing to bump")
        self.assertFalse([c for c in self.calls if c[0] in ("commit", "push", "bump")])
        self.assertEqual(self.fake.events, [])

    def test_bumps_next_patch(self):
        self.install_git("0.2.9")
        code, out, _ = run(None, ["bump", "--released", "0.2.9"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], "bumped main to 0.2.10")
        self.assertEqual(self.fake.events, [])  # the deploy key's push starts main itself
        self.assertEqual(
            self.calls,
            [
                ("fetch", "origin", "main"),
                ("checkout", "--detach", "origin/main"),
                ("bump", "0.2.10"),
                ("commit", "-am", "Start v0.2.10"),
                ("push", "origin", "HEAD:main"),
            ],
        )

    def test_retries_a_rejected_push_from_a_fresh_checkout(self):
        self.install_git("0.2.0", push_results=["rejected", None])
        code, _, _ = run(None, ["bump", "--released", "0.2.0"])
        self.assertEqual(code, 0)
        self.assertEqual(sum(c[0] == "push" for c in self.calls), 2)
        self.assertEqual(sum(c[0] == "fetch" for c in self.calls), 2)
        self.assertEqual(self.fake.events, [])

    def test_refuses_without_the_deploy_key(self):
        for value in (None, "", "false", "True"):
            with self.subTest(value=value):
                self.install_git("0.2.0")
                os.environ.pop("BUMP_KEY_SET", None)
                if value is not None:
                    os.environ["BUMP_KEY_SET"] = value
                code, _, err = run(None, ["bump", "--released", "0.2.0"])
                self.assertEqual(code, 1)
                self.assertIn("BUMP_KEY is not set", err)
                self.assertEqual(self.calls, [])
                self.assertEqual(self.fake.events, [])

    def test_release_branch_is_named_by_ci_branch(self):
        os.environ["CI_BRANCH"] = "next"
        self.install_git("0.2.0")
        code, out, _ = run(None, ["bump", "--released", "0.2.0"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "bumped next to 0.2.1")
        self.assertIn(("fetch", "origin", "next"), self.calls)
        self.assertIn(("checkout", "--detach", "origin/next"), self.calls)
        self.assertIn(("push", "origin", "HEAD:next"), self.calls)
        self.assertFalse([c for c in self.calls if "main" in c or "origin/main" in c])

    def test_branch_name_in_the_skip_and_dry_run_messages(self):
        os.environ["CI_BRANCH"] = "next"
        self.install_git("0.3.0")
        self.assertEqual(
            run(None, ["bump", "--released", "0.2.0"])[1].strip(),
            "next already carries 0.3.0; nothing to bump",
        )
        self.install_git("0.2.0")
        self.assertEqual(
            run(None, ["bump", "--released", "0.2.0", "--dry-run"])[1].strip(),
            "dry run: would bump next to 0.2.1",
        )

    def test_empty_ci_branch_means_main(self):
        os.environ["CI_BRANCH"] = ""
        self.install_git("0.2.0")
        run(None, ["bump", "--released", "0.2.0"])
        self.assertIn(("push", "origin", "HEAD:main"), self.calls)

    def test_dry_run_needs_no_key(self):
        self.install_git("0.2.0")
        os.environ.pop("BUMP_KEY_SET")
        code, out, _ = run(None, ["bump", "--released", "0.2.0", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("dry run: would bump main to 0.2.1", out)

    def test_gives_up_after_three_attempts(self):
        self.install_git("0.2.0", push_results=["rejected"] * 5)
        code, _, err = run(None, ["bump", "--released", "0.2.0"])
        self.assertEqual(code, 1)
        self.assertEqual(sum(c[0] == "push" for c in self.calls), 3)
        self.assertIn("3 times", err)
        self.assertEqual(self.fake.events, [])

    def test_retry_that_finds_main_already_bumped_stops(self):
        self.install_git("0.2.0", push_results=["rejected"])
        versions = iter(["0.2.0", "0.2.1"])
        patcher = mock.patch.object(candidate, "mise_version", lambda: next(versions))
        patcher.start()
        self.addCleanup(patcher.stop)
        code, out, _ = run(None, ["bump", "--released", "0.2.0"])
        self.assertEqual(code, 0)
        self.assertIn("nothing to bump", out)

    def test_dry_run_touches_nothing(self):
        self.install_git("0.2.0")
        code, out, _ = run(None, ["bump", "--released", "0.2.0", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "dry run: would bump main to 0.2.1")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.fake.events, [])

    def test_non_semver_is_an_error(self):
        self.install_git("1.0")
        code, _, err = run(None, ["bump", "--released", "1.0"])
        self.assertEqual(code, 1)
        self.assertIn("x.y.z", err)


if __name__ == "__main__":
    unittest.main()
