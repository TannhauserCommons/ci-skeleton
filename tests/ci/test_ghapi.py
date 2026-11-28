"""Tests for .github/ci/ghapi.py: which token `gh` runs with. No network, no gh."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[2] / ".github" / "ci"))

import ghapi  # noqa: E402


def fake_run(remote, credential, code=0):
    """Stands in for subprocess.run: answers git, records what gh was given."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(cmd, 0, remote + "\n", "")
        if cmd[:3] == ["git", "credential", "fill"]:
            return subprocess.CompletedProcess(cmd, code, credential, "")
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    return run, calls


class ProjectTokenTests(unittest.TestCase):
    def setUp(self):
        ghapi._token = False
        self.addCleanup(setattr, ghapi, "_token", False)

    def gh_env(self, remote, credential, env=None, code=0):
        """The environment `gh` was started with (None: inherited unchanged)."""
        run, calls = fake_run(remote, credential, code)
        with mock.patch.dict(os.environ, env or {}, clear=True), \
                mock.patch.object(ghapi.subprocess, "run", run):
            ghapi.gh("api", "user")
        return calls[-1][1].get("env"), calls

    def test_gh_runs_with_the_credential_git_has_for_the_remote(self):
        env, calls = self.gh_env("https://github.com/o/r.git", "username=o\npassword=secret\n")
        self.assertEqual(env["GH_TOKEN"], "secret")
        fill = next(kw for cmd, kw in calls if cmd[:3] == ["git", "credential", "fill"])
        self.assertEqual(fill["input"], "url=https://github.com/o/r.git\n\n")
        self.assertEqual(fill["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_a_token_in_the_environment_wins_and_git_is_not_asked(self):
        env, calls = self.gh_env("https://github.com/o/r.git", "password=secret\n",
                                 env={"GH_TOKEN": "from-ci"})
        self.assertIsNone(env)
        self.assertEqual([cmd[0] for cmd, _ in calls], ["gh"])

    def test_no_stored_credential_leaves_gh_its_own_login(self):
        env, _ = self.gh_env("https://github.com/o/r.git", "", code=1)
        self.assertIsNone(env)

    def test_a_remote_that_is_not_github_https_is_left_alone(self):
        env, calls = self.gh_env("git@github.com:o/r.git", "password=secret\n")
        self.assertIsNone(env)
        self.assertFalse(any(cmd[:3] == ["git", "credential", "fill"] for cmd, _ in calls))

    def test_the_lookup_happens_once(self):
        run, calls = fake_run("https://github.com/o/r.git", "password=secret\n")
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(ghapi.subprocess, "run", run):
            ghapi.gh("api", "user")
            ghapi.gh("api", "user")
        self.assertEqual(sum(cmd[:2] == ["git", "credential"] for cmd, _ in calls), 1)


class GhOutputTests(unittest.TestCase):
    def test_out_takes_stdout_as_bytes_and_errors_still_raise(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append(kwargs)
            kwargs["stdout"].write(b"PK\x03\x04\xff")
            return subprocess.CompletedProcess(cmd, 0, None, "")

        with tempfile.TemporaryFile() as handle, \
                mock.patch.dict(os.environ, {"GH_TOKEN": "t"}), \
                mock.patch.object(ghapi.subprocess, "run", run):
            self.assertIsNone(ghapi.gh("api", "zip", out=handle))
            handle.seek(0)
            self.assertEqual(handle.read(), b"PK\x03\x04\xff")
        self.assertEqual(calls[0]["stderr"], subprocess.PIPE)

        def fail(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, None, "Not Found")

        with tempfile.TemporaryFile() as handle, \
                mock.patch.dict(os.environ, {"GH_TOKEN": "t"}), \
                mock.patch.object(ghapi.subprocess, "run", fail):
            with self.assertRaisesRegex(RuntimeError, "Not Found"):
                ghapi.gh("api", "zip", out=handle)


if __name__ == "__main__":
    unittest.main()
