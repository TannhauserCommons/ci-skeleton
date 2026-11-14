"""build: write dist/hello-<version>-<target>.pyz, the same bytes every time."""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
MAIN = b"from hello.__main__ import main\nraise SystemExit(main())\n"


def target():
    """Return this runner's release target: linux, windows or other."""
    if sys.platform.startswith("linux"):
        return "linux"
    return "windows" if sys.platform == "win32" else "other"


def entries(version, switches):
    """Return {archive name: bytes}: the app, its build file and the entry point.

    Source is read as bytes with CRLF folded to LF so a Windows checkout and a
    Linux one give the same archive.
    """
    files = {"__main__.py": MAIN}
    for path in sorted((ROOT / "fixture" / "hello").glob("*.py")):
        files[f"hello/{path.name}"] = path.read_bytes().replace(b"\r\n", b"\n")
    flags = ", ".join(f'"{k}": {v}' for k, v in sorted(switches.items()))
    build = f'VERSION = "{version}-oops"\nSWITCHES = {{{flags}}}\n'
    files["hello/_build.py"] = build.encode("utf-8")
    return files


def write_archive(path, files):
    """Write a runnable zip with sorted entries and fixed metadata.

    zipapp would embed file mtimes; writing the zip by hand keeps it
    reproducible. create_system is set because its default differs by OS.
    """
    with open(path, "wb") as fp:
        fp.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(fp, "w") as zf:
            for name in sorted(files):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_STORED  # no zlib build differences
                zf.writestr(info, files[name])


def main():
    """Build this runner's archive and return the exit code."""
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / "fixture"))
    import hello
    version, switches = hello.source_config(ROOT)
    DIST.mkdir(exist_ok=True)
    out = DIST / f"hello-{version}-{target()}.pyz"
    write_archive(out, entries(version, switches))
    print(f"build: {out.relative_to(ROOT).as_posix()} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
