import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[2] / ".github" / "ci"))

import switches  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def switch(name="shout", **overrides):
    fields = {
        "name": name,
        "what": "Greets in capitals",
        "owner": "owner",
        "area": "cli",
        "default": "off",
        "ready": False,
        "expires": "0.3.0",
    }
    fields.update(overrides)
    return {k: v for k, v in fields.items() if v is not None}


def toml_of(*items, cap=2, tombstones=()):
    lines = [f"cap_per_area = {cap}" if cap is not None else "", f"tombstones = {list(tombstones)!r}".replace("'", '"')]
    for item in items:
        lines.append("\n[[switch]]")
        for key, value in item.items():
            if isinstance(value, bool):
                value = str(value).lower()
            elif isinstance(value, str):
                value = f'"{value}"'
            lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"


class FileCase(unittest.TestCase):
    """Runs `switches.main` against a temp file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "switches.toml"

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")

    def run_main(self, *args, text=None):
        if text is not None:
            self.write(text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = switches.main([args[0], "--file", str(self.path), *args[1:]])
        return code, out.getvalue().splitlines()

    def check(self, *items, args=("--version", "0.2.0", "--today", "2026-01-01"), **kwargs):
        return self.run_main("check", *args, text=toml_of(*items, **kwargs))

    def findings(self, *items, **kwargs):
        code, lines = self.check(*items, **kwargs)
        self.assertEqual(code, 1, lines)
        return lines


class CheckTest(FileCase):
    def test_valid_file_is_ok(self):
        code, lines = self.check(switch("a"), switch("b", area="web"), switch("c", ready=True))
        self.assertEqual((code, lines), (0, ["switches: ok (3 switches)"]))

    def test_finding_format_names_file_and_switch(self):
        lines = self.findings(switch(owner=None))
        self.assertEqual(lines, [f"{self.path}: shout: missing owner"])

    def test_empty_file_with_cap_is_ok(self):
        code, lines = self.check()
        self.assertEqual((code, lines), (0, ["switches: ok (0 switches)"]))

    def test_missing_file_is_ok_with_zero(self):
        code, lines = self.run_main("check", "--version", "0.2.0")
        self.assertEqual((code, lines), (0, ["switches: ok (0 switches)"]))

    def test_invalid_toml_is_a_finding(self):
        code, lines = self.run_main("check", "--version", "0.2.0", text="cap_per_area = [")
        self.assertEqual(code, 1)
        self.assertIn("invalid TOML", lines[0])

    def test_each_required_key(self):
        for key in switches.REQUIRED:
            with self.subTest(key=key):
                lines = self.findings(switch(**{key: None}))
                self.assertTrue(any(f"missing {key}" in line for line in lines), lines)

    def test_unknown_switch_key(self):
        lines = self.findings(switch(color="red"))
        self.assertIn("unknown key `color`", lines[0])

    def test_unknown_top_level_key(self):
        code, lines = self.run_main("check", "--version", "0.2.0", text="cap_per_area = 2\nextra = 1\n")
        self.assertEqual(code, 1)
        self.assertIn("unknown key `extra`", lines[0])

    def test_bad_names(self):
        for name in ("Shout", "sh out", "shout_it", ""):
            with self.subTest(name=name):
                self.assertTrue(any("[a-z0-9-]+" in line for line in self.findings(switch(name))))

    def test_good_names(self):
        for name in ("a", "shout-2", "0"):
            with self.subTest(name=name):
                self.assertEqual(self.check(switch(name))[0], 0)

    def test_duplicate_names(self):
        lines = self.findings(switch("a"), switch("a", area="web"))
        self.assertEqual(lines, [f"{self.path}: a: duplicate name"])

    def test_tombstoned_name(self):
        lines = self.findings(switch("old"), tombstones=["old"])
        self.assertIn("tombstone", lines[0])

    def test_other_name_with_tombstones_is_fine(self):
        self.assertEqual(self.check(switch("new"), tombstones=["old"])[0], 0)

    def test_default_must_be_off_or_on(self):
        self.assertIn('default must be "off" or "on"', self.findings(switch(default="maybe"))[0])
        self.assertEqual(self.check(switch(default="on", expires="0.9.0"))[0], 0)

    def test_ready_must_be_bool(self):
        self.assertIn("ready must be", self.findings(switch(ready="yes"))[0])

    def test_ready_with_default_on_is_a_finding(self):
        lines = self.findings(switch(ready=True, default="on"))
        self.assertIn("already on", lines[0])

    def test_ready_with_default_off_is_fine(self):
        self.assertEqual(self.check(switch(ready=True))[0], 0)

    def test_expires_formats(self):
        for good in ("1.2.3", "2027-12-31"):
            with self.subTest(good=good):
                self.assertEqual(self.check(switch(expires=good))[0], 0)
        for bad in ("1.2", "v1.2.3", "2027-13-01", "2027-1-1", "soon", "1.2.3-rc1"):
            with self.subTest(bad=bad):
                self.assertIn("expires must be", self.findings(switch(expires=bad))[0])

    def test_cap_counts_every_switch_in_an_area(self):
        lines = self.findings(switch("a"), switch("b", default="on", expires="0.9.0"), switch("c", ready=True), cap=2)
        self.assertEqual(lines, [f"{self.path}: file: area cli has 3 switches, the cap is 2"])

    def test_cap_is_per_area(self):
        self.assertEqual(self.check(switch("a"), switch("b"), switch("c", area="web"), cap=2)[0], 0)

    def test_cap_is_required_and_positive(self):
        self.assertIn("missing cap_per_area", self.findings(switch(), cap=None)[0])
        for bad in (0, -1):
            with self.subTest(bad=bad):
                self.assertIn("positive integer", self.findings(switch(), cap=bad)[0])
        self.assertIn("positive integer", self.findings(switch(), cap='"two"')[0])

    def test_tombstones_must_be_names(self):
        code, lines = self.run_main("check", "--version", "0.2.0", text="cap_per_area = 1\ntombstones = [1]\n")
        self.assertEqual(code, 1)
        self.assertIn("tombstones", lines[0])


class ExpiryTest(FileCase):
    def test_version_before_expiry_is_fine(self):
        self.assertEqual(self.check(switch(expires="0.3.0"), args=("--version", "0.2.9"))[0], 0)

    def test_version_at_expiry_is_a_finding(self):
        lines = self.findings(switch(expires="0.3.0"), args=("--version", "0.3.0"))
        self.assertIn("shout: expired", lines[0])

    def test_version_after_expiry_is_a_finding(self):
        self.findings(switch(expires="0.3.0"), args=("--version", "1.0.0"))

    def test_versions_compare_as_integers(self):
        self.assertEqual(self.check(switch(expires="0.10.0"), args=("--version", "0.9.0"))[0], 0)
        self.findings(switch(expires="0.9.0"), args=("--version", "0.10.0"))

    def test_date_on_the_expiry_day_is_fine(self):
        self.assertEqual(self.check(switch(expires="2026-06-01"), args=("--today", "2026-06-01"))[0], 0)

    def test_date_after_expiry_is_a_finding(self):
        lines = self.findings(switch(expires="2026-06-01"), args=("--today", "2026-06-02"))
        self.assertIn("expired", lines[0])

    def test_version_comes_from_mise_when_not_given(self):
        done = mock.Mock(returncode=0, stdout="mise noise\n0.3.0\n\n", stderr="")
        with mock.patch.object(switches.shutil, "which", return_value="mise"), \
                mock.patch.object(switches.subprocess, "run", return_value=done) as run:
            code, lines = self.check(switch(expires="0.3.0"), args=())
        self.assertEqual(run.call_args.args[0], ["mise", "run", "version"])
        self.assertEqual(code, 1)
        self.assertIn("expired", lines[0])

    def test_failing_mise_is_a_finding(self):
        done = mock.Mock(returncode=1, stdout="", stderr="boom")
        with mock.patch.object(switches.shutil, "which", return_value="mise"), \
                mock.patch.object(switches.subprocess, "run", return_value=done):
            lines = self.findings(switch(), args=())
        self.assertIn("boom", lines[0])

    def test_date_comes_from_the_head_commit_when_not_given(self):
        done = mock.Mock(returncode=0, stdout="2026-07-01\n", stderr="")
        with mock.patch.object(switches.subprocess, "run", return_value=done) as run:
            lines = self.findings(switch(expires="2026-06-01"), args=())
        self.assertEqual(run.call_args.args[0], ["git", "log", "-1", "--format=%cs"])
        self.assertIn("expired", lines[0])

    def test_nothing_runs_when_not_needed(self):
        with mock.patch.object(switches.subprocess, "run") as run:
            self.assertEqual(self.check(switch(expires="2026-06-01"), args=("--today", "2026-01-01"))[0], 0)
            self.assertEqual(self.check(switch(expires="0.3.0"), args=("--version", "0.1.0"))[0], 0)
        run.assert_not_called()


class ListNotesTest(FileCase):
    ITEMS = (
        switch("a"),
        switch("b", ready=True, what="Is finished"),
        switch("c", default="on", what="Is live"),
    )

    def setUp(self):
        super().setUp()
        self.write(toml_of(*self.ITEMS))

    def test_list_all_and_filters(self):
        self.assertEqual(self.run_main("list")[1], ["a", "b", "c"])
        self.assertEqual(self.run_main("list", "--off")[1], ["a", "b"])
        self.assertEqual(self.run_main("list", "--on")[1], ["c"])
        self.assertEqual(self.run_main("list", "--ready")[1], ["b"])

    def test_list_of_missing_file_is_empty(self):
        self.path.unlink()
        self.assertEqual(self.run_main("list"), (0, []))

    def test_notes_format(self):
        code, lines = self.run_main("notes")
        self.assertEqual(code, 0)
        self.assertEqual(
            lines,
            ["### Switches", "", "On in this release:", "- `c`: Is live", "", "Ready to turn on:", "- `b`: Is finished"],
        )

    def test_notes_omit_an_empty_sub_list(self):
        self.write(toml_of(switch("c", default="on", what="Is live")))
        self.assertEqual(self.run_main("notes")[1], ["### Switches", "", "On in this release:", "- `c`: Is live"])
        self.write(toml_of(switch("b", ready=True, what="Is finished")))
        self.assertEqual(self.run_main("notes")[1], ["### Switches", "", "Ready to turn on:", "- `b`: Is finished"])

    def test_notes_are_empty_when_nothing_to_say(self):
        self.write(toml_of(switch("a")))
        self.assertEqual(self.run_main("notes"), (0, []))
        self.path.unlink()
        self.assertEqual(self.run_main("notes"), (0, []))


class ShippedTest(FileCase):
    def run_shipped(self, text, upsert):
        fake = mock.Mock()
        fake.upsert_issue.side_effect = upsert
        with mock.patch.object(switches, "ghapi", fake):
            result = self.run_main("shipped", text=text)
        return result, fake

    def test_one_issue_per_on_switch(self):
        text = toml_of(switch("a"), switch("b", default="on"), switch("c", default="on", what="Is live"))
        (code, lines), fake = self.run_shipped(text, lambda **kw: (7, True))
        self.assertEqual(code, 0)
        self.assertEqual(fake.upsert_issue.call_count, 2)
        first, second = (call.kwargs for call in fake.upsert_issue.call_args_list)
        self.assertEqual((first["label"], first["title"]), ("switch-removal", "Remove switch b"))
        self.assertEqual(second["title"], "Remove switch c")
        self.assertIn("Is live", second["body"])
        self.assertEqual(lines, ["b: opened #7", "c: opened #7"])

    def test_existing_issue_is_commented_on(self):
        (_, lines), _ = self.run_shipped(toml_of(switch("b", default="on")), lambda **kw: (3, False))
        self.assertEqual(lines, ["b: commented on #3"])

    def test_no_on_switches_no_calls(self):
        (code, lines), fake = self.run_shipped(toml_of(switch("a")), lambda **kw: (1, True))
        self.assertEqual((code, lines), (0, []))
        fake.upsert_issue.assert_not_called()


class OwnFileTest(unittest.TestCase):
    """The repo's own switches.toml, with the version it really carries."""

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = switches.main(["check", "--file", str(REPO / "switches.toml"), *args])
        return code, out.getvalue()

    def test_ok_before_expiry(self):
        self.assertEqual(self.run_main("--version", "0.2.0"), (0, "switches: ok (1 switches)\n"))

    def test_names_the_switch_at_expiry(self):
        code, out = self.run_main("--version", "0.3.0")
        self.assertEqual(code, 1)
        self.assertIn("shout", out)


if __name__ == "__main__":
    unittest.main()
