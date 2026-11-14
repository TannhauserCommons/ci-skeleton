import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[2] / ".github" / "ci"))

import paths  # noqa: E402

PATHS = """\
# One key per path-gated job id in check.yml.
rust:
  - 'crates/**'
  - 'Cargo.toml'

web: # the web app
  - 'web/**'

"docs":
  - 'docs/**'
"""


class SelectTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = Path(tmp.name) / "paths.yml"
        self.paths.write_text(PATHS, encoding="utf-8")
        self.out = Path(tmp.name) / "output"

    def run_main(self, *args, changes=None):
        env = {"GITHUB_OUTPUT": str(self.out)}
        if changes is not None:
            env["CHANGES"] = changes
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = paths.main(["--paths", str(self.paths), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def outputs(self):
        lines = self.out.read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines)

    def test_keys_are_top_level_only(self):
        self.assertEqual(paths.read_keys(self.paths), ["rust", "web", "docs"])

    def test_changed_keys_run_in_file_order(self):
        code, _, _ = self.run_main(changes='["web", "rust"]')
        self.assertEqual(code, 0)
        out = self.outputs()
        self.assertEqual(json.loads(out["run"]), ["rust", "web"])
        self.assertEqual(out["skippable"], "docs")

    def test_stdout_shows_the_outputs(self):
        _, stdout, _ = self.run_main(changes='["docs"]')
        self.assertIn('run=["docs"]', stdout)
        self.assertIn("skippable=rust,web", stdout)

    def test_unset_changes_means_none(self):
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        out = self.outputs()
        self.assertEqual(json.loads(out["run"]), [])
        self.assertEqual(out["skippable"], "rust,web,docs")

    def test_empty_and_blank_changes_mean_none(self):
        for raw in ("", "  ", "[]"):
            with self.subTest(raw=raw):
                self.out.write_text("", encoding="utf-8")
                self.run_main(changes=raw)
                self.assertEqual(json.loads(self.outputs()["run"]), [])

    def test_all_runs_everything(self):
        self.run_main("--all", changes='["web"]')
        out = self.outputs()
        self.assertEqual(json.loads(out["run"]), ["rust", "web", "docs"])
        self.assertEqual(out["skippable"], "")

    def test_unknown_key_fails(self):
        code, _, err = self.run_main(changes='["rust", "nope"]')
        self.assertEqual(code, 1)
        self.assertIn("nope", err)

    def test_bad_json_fails(self):
        code, _, err = self.run_main(changes="rust")
        self.assertEqual(code, 1)
        self.assertIn("paths:", err)

    def test_non_array_fails(self):
        code, _, _ = self.run_main(changes='{"rust": true}')
        self.assertEqual(code, 1)

    def test_missing_paths_file_fails(self):
        self.paths.unlink()
        code, _, err = self.run_main(changes="[]")
        self.assertEqual(code, 1)
        self.assertIn("paths:", err)


if __name__ == "__main__":
    unittest.main()
