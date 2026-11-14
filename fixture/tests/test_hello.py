"""Unit tests for hello, plus two switchable tests that prove check.py's retry."""
import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import hello

ROOT = Path(__file__).resolve().parents[2]


def run_cli(*args, flags=None):
    """Run `python -m hello` in a clean environment and return its output."""
    env = {k: v for k, v in os.environ.items() if k != "HELLO_FLAGS"}
    env["PYTHONPATH"] = str(ROOT / "fixture")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if flags:
        env["HELLO_FLAGS"] = flags
    done = subprocess.run([sys.executable, "-m", "hello", *args], env=env, cwd=ROOT,
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


class Defaults(unittest.TestCase):
    def test_greeting_follows_switches_toml(self):
        data = tomllib.loads((ROOT / "switches.toml").read_text(encoding="utf-8"))
        shout = next(s for s in data["switch"] if s["name"] == "shout")
        want = "HELLO, WORLD" if shout["default"] == "on" else "hello, world"
        self.assertEqual(hello.greeting({}), want)

    def test_off_is_plain(self):
        with mock.patch.object(hello, "switches", return_value={"shout": False}):
            self.assertEqual(hello.greeting({}), "hello, world")

    def test_undeclared_switch_stays_off(self):
        with mock.patch.object(hello, "switches", return_value={}):
            self.assertEqual(hello.greeting({"HELLO_FLAGS": "shout"}), "hello, world")

    def test_version(self):
        want = (ROOT / "fixture" / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(hello.version(), want)
        self.assertEqual(run_cli("--version"), want)

    def test_screen(self):
        self.assertEqual(run_cli("--screen"), hello.SCREEN)


class ShoutOn(unittest.TestCase):
    """The shout switch's own tests, with it on."""

    def test_greeting(self):
        with mock.patch.object(hello, "switches", return_value={"shout": False}):
            self.assertEqual(hello.greeting({"HELLO_FLAGS": "shout"}), "HELLO, WORLD")

    def test_cli(self):
        self.assertEqual(run_cli(flags="shout"), "HELLO, WORLD")


@unittest.skipUnless(os.environ.get("FIXTURE_FLAKE") == "1", "set FIXTURE_FLAKE=1")
class Flaky(unittest.TestCase):
    def test_passes_on_retry(self):
        # Why: check.py sets FIXTURE_RETRY=1 for its one retry run, so the first
        # run fails and the second passes with no clock or randomness.
        self.assertEqual(os.environ.get("FIXTURE_RETRY"), "1")


@unittest.skipUnless(os.environ.get("FIXTURE_BREAK") == "1", "set FIXTURE_BREAK=1")
class Broken(unittest.TestCase):
    def test_always_fails(self):
        self.fail("broken on purpose")
