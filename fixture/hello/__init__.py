"""hello: the fixture app, the smallest thing that fills in the task contract."""
# Layer A case 4: this comment stands for a change that grows the app a little; on its own it fits the source-bytes budget, but with the matching change in the other file of this package the total goes over the max.
import functools
import os
from pathlib import Path

# The key screen: the text `looks` compares between a base and a change.
SCREEN = """\
+------------------+
|   hello, world   |
+------------------+"""


def source_config(root):
    """Read (version, switch defaults) from a source tree.

    Shared by the build, which freezes the answer into the archive, and by
    a run from source, so both agree on what the switch list is.
    """
    import tomllib
    version = (Path(root) / "fixture" / "VERSION").read_text(encoding="utf-8").strip()
    data = tomllib.loads((Path(root) / "switches.toml").read_text(encoding="utf-8"))
    return version, {s["name"]: s["default"] == "on" for s in data.get("switch", [])}


@functools.cache
def _config():
    """Return (version, switch defaults): frozen at build time, else from source.

    An archive has no repo around it, so the build writes _build.py into it.
    """
    try:
        from . import _build
    except ImportError:
        return source_config(Path(__file__).resolve().parents[2])
    return _build.VERSION, dict(_build.SWITCHES)


def version():
    """Return the version string."""
    return _config()[0]


def switches():
    """Return each switch's default, True for on."""
    return dict(_config()[1])


def enabled(name, env=None):
    """Say whether a declared switch is on: its default, or HELLO_FLAGS at runtime.

    HELLO_FLAGS stands for the owner-only runtime setting. A name that is not
    declared stays off, so an off switch behaves as if it did not exist.
    """
    env = os.environ if env is None else env
    runtime = {f.strip() for f in env.get("HELLO_FLAGS", "").split(",")}
    known = switches()
    return name in known and (known[name] or name in runtime)


def greeting(env=None):
    """Return the greeting, in capitals when the shout switch is on."""
    text = "hello, world"
    return text.upper() if enabled("shout", env) else text
