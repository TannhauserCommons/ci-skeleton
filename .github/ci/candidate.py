"""The release candidate: `main`'s last green build, parked on one standing draft release.

Subcommands (see docs/context/contract.md): `base`, `record`, `fetch`, `publish`, `bump`.
Every one is safe to run again, so a failure anywhere costs a rerun and never a version:
the tag is created last, by publishing the draft.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import ghapi

# Ends the body of the one draft release that holds the candidate.
MARKER = "<!-- ci:candidate -->"
# Files of dist/ that are never assets: the manifest is handled apart, the notes become the body.
NOT_ASSETS = ("candidate.json", "NOTES.md")
# GitHub rewrites asset names it dislikes (spaces become dots, ...), which would break
# the name -> hash map, so record refuses names it would not keep as they are.
SAFE_NAME = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+-]*")
BUMP_ATTEMPTS = 3
# The release's own bump commit: it changes no behaviour, so it is not in the notes.
BUMP_SUBJECT = re.compile(r"Start v\d+\.\d+\.\d+")


class CandidateError(Exception):
    """A refusal with a message for the person reading the log."""


# --- process helpers (stubbed in tests) -------------------------------------------------


def git(*args):
    """Run git and return stdout; a non-zero exit raises with git's own message."""
    done = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def mise_version():
    """The version `main` carries: the last non-empty stdout line of `mise run version`.

    The last line, because mise and the project task may print progress before it.
    """
    mise = shutil.which("mise") or "mise"  # Windows needs the resolved .exe/.cmd
    done = subprocess.run([mise, "run", "version"], capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise RuntimeError(f"mise run version: {done.stderr.strip() or done.stdout.strip()}")
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    if not lines:
        raise CandidateError("mise run version printed nothing")
    return lines[-1]


def mise_bump(version):
    """Have the project write `version` into every file that holds it."""
    mise = shutil.which("mise") or "mise"
    done = subprocess.run([mise, "run", "bump", version], capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise RuntimeError(f"mise run bump {version}: {done.stderr.strip() or done.stdout.strip()}")


def switches(*args):
    """Run `switches.py <args>` and return its stdout (a separate script, so a subprocess)."""
    script = Path(__file__).with_name("switches.py")
    done = subprocess.run(
        [sys.executable, str(script), *args], capture_output=True, text=True, encoding="utf-8"
    )
    if done.returncode != 0:
        raise RuntimeError(f"switches.py {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


# --- GitHub lookups -------------------------------------------------------------------------


def all_releases():
    """Releases of the repository, drafts included (the token has write access)."""
    return ghapi.api("releases?per_page=100", paginate=True) or []


def standing_draft(releases=None):
    """The draft release that carries the candidate, or None.

    More than one is an error: two candidates mean nobody knows which one is real.
    """
    releases = all_releases() if releases is None else releases
    found = [
        r for r in releases if r.get("draft") and (r.get("body") or "").strip().endswith(MARKER)
    ]
    if len(found) > 1:
        names = ", ".join(f"{r.get('tag_name')} (id {r.get('id')})" for r in found)
        raise CandidateError(f"more than one candidate draft: {names}; delete the extra ones")
    return found[0] if found else None


def tag_ref(tag):
    """The ref object of the git tag (`{"ref", "object": {"sha", "type"}}`), or None.

    The prefix listing never 404s, unlike `git/ref/tags/<tag>`, and a prefix
    match (`v0.2.0-rc1` for `v0.2.0`) is filtered out here.
    """
    refs = ghapi.api(f"git/matching-refs/tags/{tag}?per_page=100") or []
    return next((ref for ref in refs if ref.get("ref") == f"refs/tags/{tag}"), None)


def tag_exists(tag):
    """Whether the git tag exists."""
    return tag_ref(tag) is not None


def tag_commit(tag):
    """The commit a tag points at: lightweight tags point straight at it, annotated ones via a tag object."""
    obj = tag_ref(tag)["object"]
    if obj.get("type") == "tag":
        obj = ghapi.api(f"git/tags/{obj['sha']}")["object"]
    return obj["sha"]


def draft_assets(release_id):
    """The assets currently on a release."""
    return ghapi.api(f"releases/{release_id}/assets?per_page=100", paginate=True) or []


# --- files -------------------------------------------------------------------------------------


def sha256(path):
    """Hex SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_files(directory):
    """The regular files directly in `directory` that are release files, sorted by name.

    No recursion: assets are flat, so a subdirectory is an error rather than silently skipped.
    """
    root = Path(directory)
    if not root.is_dir():
        raise CandidateError(f"{root} is not a directory")
    files = []
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if entry.is_dir():
            raise CandidateError(f"{entry} is a directory; release files must sit directly in {root}")
        if not entry.is_file():
            raise CandidateError(f"{entry} is not a regular file")
        if entry.name not in NOT_ASSETS:
            files.append(entry)
    return files


def read_manifest(directory):
    """`candidate.json` of `directory` as a dict; raises CandidateError when unusable."""
    path = Path(directory) / "candidate.json"
    if not path.is_file():
        raise CandidateError(f"{path} is missing")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as err:
        raise CandidateError(f"{path} is not valid JSON: {err}") from err
    problems = manifest_problems(manifest)
    if problems:
        raise CandidateError(f"{path}: " + "; ".join(problems))
    return manifest


def manifest_problems(manifest):
    """What is wrong with a parsed manifest (empty list when it is usable)."""
    if not isinstance(manifest, dict):
        return ["not a JSON object"]
    problems = []
    for key in ("sha", "version"):
        if not isinstance(manifest.get(key), str) or not manifest.get(key):
            problems.append(f"missing `{key}`")
    if not isinstance(manifest.get("files"), dict):
        problems.append("missing `files`")
    return problems


# --- notes ---------------------------------------------------------------------------------


def split_subject(subject):
    """`(area, text)` of a commit subject: the prefix before the first `: ` when short, else Other."""
    prefix, sep, rest = subject.partition(": ")
    if sep and rest.strip() and prefix.strip() and len(prefix) <= 40 and len(prefix.split()) <= 3:
        return prefix, rest.strip()
    return "Other", subject


def group_subjects(subjects):
    """Subjects (newest first) as `[(area, [text, ...])]`: areas by first appearance, Other last."""
    groups = {}
    for subject in subjects:
        area, text = split_subject(subject)
        groups.setdefault(area, []).append(text)
    other = groups.pop("Other", None)
    ordered = list(groups.items())
    if other:
        ordered.append(("Other", other))
    return ordered


def build_notes(version, sha, last_tag, subjects, switch_notes="", extra_notes=""):
    """The draft's Markdown body. The last two lines (candidate sha, marker) identify the draft."""
    parts = [f"## v{version}"]
    if subjects:
        for area, items in group_subjects(subjects):
            parts.append(f"### {area}\n" + "\n".join(f"- {item}" for item in items))
    else:
        parts.append(f"No changes since {last_tag}." if last_tag else "No changes.")
    for extra in (switch_notes, extra_notes):
        if extra.strip():
            parts.append(extra.strip())
    return "\n\n".join(parts) + f"\n\nCandidate: {sha}\n{MARKER}\n"


def strip_candidate_lines(body):
    """The body without its two trailing identity lines, for the published release."""
    lines = (body or "").rstrip().splitlines()
    if lines and lines[-1].strip() == MARKER:
        lines.pop()
    if lines and lines[-1].startswith("Candidate: "):
        lines.pop()
    return "\n".join(lines).rstrip() + "\n"


def last_release_tag(sha):
    """The newest `v*` tag reachable from `sha`, or None when there is none."""
    try:
        return git("describe", "--tags", "--abbrev=0", "--match", "v*", sha).strip() or None
    except RuntimeError:  # git describe fails when no tag matches
        return None


def commit_subjects(sha, last_tag):
    """Subjects of the non-merge commits since the last release, newest first."""
    span = f"{last_tag}..{sha}" if last_tag else sha
    subjects = git("log", "--no-merges", "--format=%s", span).splitlines()
    return [s for s in subjects if s.strip() and not BUMP_SUBJECT.fullmatch(s.strip())]


# --- subcommands -----------------------------------------------------------------------------


def cmd_base(args):
    """Output `base`: the commit `main` last tested green; nothing when there is none."""
    runs = ghapi.api("actions/workflows/main.yml/runs?branch=main&status=success&per_page=1") or {}
    found = runs.get("workflow_runs") or []
    if found:
        ghapi.output("base", found[0]["head_sha"])
    return 0


def replace_assets(draft, tag, files, manifest_path):
    """Swap the draft's assets for `files`, ordered so a half-done swap can never pass `fetch`.

    The manifest goes first on the way out and last on the way in: with no manifest
    on the draft, `fetch` refuses, so a crash in between costs a rerun only.
    """
    old = sorted(draft_assets(draft["id"]), key=lambda a: a["name"] != "candidate.json")
    for asset in old:
        ghapi.api(f"releases/assets/{asset['id']}", "DELETE")
    if files:
        ghapi.gh("release", "upload", tag, *[str(f) for f in files], "--clobber")
    ghapi.gh("release", "upload", tag, str(manifest_path), "--clobber")


def cmd_record(args):
    """Make `dist/` the release candidate on the standing draft."""
    version = mise_version()
    tag = f"v{version}"
    sha = os.environ.get("GITHUB_SHA") or git("rev-parse", "HEAD").strip()
    if tag_exists(tag):
        # Between the released commit and the release's bump commit, main may take
        # more commits that still carry this version. Their runs must stay green,
        # so this is not a failure; the release moves main on, and the next green
        # main records the next candidate.
        print(
            f"candidate: {tag} is already released; this commit is not a candidate "
            "(the release moves main to the next version)"
        )
        print(f"::notice title=No candidate::{tag} is already released")
        return 0
    files = release_files(args.dir)
    if not files:
        raise CandidateError(f"no release files in {args.dir}")
    bad = [f.name for f in files if not SAFE_NAME.fullmatch(f.name)]
    if bad:
        raise CandidateError(
            "file names GitHub would rewrite as assets (use letters, digits, . _ + -): "
            + ", ".join(bad)
        )

    manifest = {
        "sha": sha,
        "version": version,
        "run_id": int(os.environ.get("GITHUB_RUN_ID") or 0),
        "run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT") or 0),
        "files": {f.name: sha256(f) for f in files},
    }
    manifest_path = Path(args.dir) / "candidate.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    last_tag = last_release_tag(sha)
    notes_file = Path(args.dir) / "NOTES.md"
    body = build_notes(
        version,
        sha,
        last_tag,
        commit_subjects(sha, last_tag),
        switches("notes"),
        notes_file.read_text(encoding="utf-8") if notes_file.is_file() else "",
    )

    fields = {
        "tag_name": tag,
        "name": tag,
        "target_commitish": sha,
        "body": body,
        "draft": True,
    }
    draft = standing_draft()
    if draft is None:
        draft = ghapi.api("releases", "POST", fields)
    else:
        draft = ghapi.api(f"releases/{draft['id']}", "PATCH", fields)
    replace_assets(draft, tag, files, manifest_path)
    print(f"candidate: {tag} {sha[:7]} ({len(files)} files)")
    return 0


def verify_candidate(directory, draft):
    """Every way the downloaded files disagree with the manifest and the draft, as messages."""
    root = Path(directory)
    problems = []
    present = {e.name for e in root.iterdir() if e.name != "candidate.json"}
    path = root / "candidate.json"
    if not path.is_file():
        return ["candidate.json is missing"] + [f"unlisted file: {n}" for n in sorted(present)], None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as err:
        return [f"candidate.json is not valid JSON: {err}"], None
    bad = manifest_problems(manifest)
    if bad:
        return [f"candidate.json: {b}" for b in bad], None

    listed = manifest["files"]
    for name in sorted(listed):
        file = root / name
        if not file.is_file():
            problems.append(f"missing file: {name}")
        elif sha256(file) != listed[name]:
            problems.append(f"hash differs: {name}")
    problems += [f"unlisted file: {n}" for n in sorted(present - set(listed))]
    if manifest["sha"] != draft.get("target_commitish"):
        problems.append(
            f"candidate.json sha {manifest['sha'][:7]} differs from the draft's "
            f"{str(draft.get('target_commitish'))[:7]}"
        )
    if draft.get("tag_name") != f"v{manifest['version']}":
        problems.append(
            f"candidate.json version {manifest['version']} differs from the draft's tag "
            f"{draft.get('tag_name')}"
        )
    return problems, manifest


def resume_published():
    """Report a release that published but did not finish, or fail with "no candidate".

    With no draft, the version `main` carries is still the released one only
    when the bump never landed. Then only the bump is left, and its inputs are
    read from the published release instead of a download.
    """
    version = mise_version()
    tag = f"v{version}"
    released = any(
        r.get("tag_name") == tag and not r.get("draft") for r in all_releases()
    )
    if not released or not tag_exists(tag):
        raise CandidateError("no candidate: main has not been green since the last release")
    print(f"release: {tag} is already published; only the bump is left")
    ghapi.output("published", "true")
    ghapi.output("sha", tag_commit(tag))
    ghapi.output("version", version)
    ghapi.output("tag", tag)
    return 0


def cmd_fetch(args):
    """Download the candidate into `--dir` and check every hash.

    Always sets the output `published`: `true` only on the resume path.
    """
    draft = standing_draft()
    if draft is None:
        return resume_published()
    out = Path(args.dir)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise CandidateError(f"{out} is not empty; fetch needs an empty directory")
    out.mkdir(parents=True, exist_ok=True)
    ghapi.gh("release", "download", draft["tag_name"], "--dir", str(out))
    problems, manifest = verify_candidate(out, draft)
    if problems:
        for problem in problems:
            print(f"fetch: {problem}", file=sys.stderr)
        raise CandidateError(f"the candidate failed {len(problems)} check(s)")
    ghapi.output("published", "false")
    ghapi.output("sha", manifest["sha"])
    ghapi.output("version", manifest["version"])
    ghapi.output("tag", f"v{manifest['version']}")
    return 0


def cmd_publish(args):
    """Upload the signed files and publish the draft; publishing creates the tag, so it goes last."""
    manifest = read_manifest(args.dir)
    version, sha = manifest["version"], manifest["sha"]
    tag = f"v{version}"
    draft = standing_draft()
    if draft is None:
        if tag_exists(tag):
            # A rerun after the publish already happened: nothing left to do here.
            print(f"already published: {tag}")
            return 0
        raise CandidateError(f"no candidate draft and no tag {tag}: nothing to publish")
    if draft.get("tag_name") != tag or draft.get("target_commitish") != sha:
        # A newer green main replaced the candidate while this release ran; its
        # assets are not the ones that were signed.
        raise CandidateError(
            f"the draft is {draft.get('tag_name')} at {str(draft.get('target_commitish'))[:7]}, "
            f"but {args.dir}/candidate.json says {tag} at {sha[:7]}; rerun the release"
        )
    files = release_files(args.dir)
    if not files:
        raise CandidateError(f"no release files in {args.dir}")

    names = ", ".join(f.name for f in files)
    if args.dry_run:
        print(f"dry run: would upload {len(files)} files to {tag}: {names}")
        print("dry run: would run switches.py shipped")
        print(f"dry run: would publish {tag} (creates the tag at {sha[:7]})")
        return 0
    ghapi.gh("release", "upload", draft["tag_name"], *[str(f) for f in files], "--clobber")
    switches("shipped")
    ghapi.api(
        f"releases/{draft['id']}",
        "PATCH",
        {"draft": False, "body": strip_candidate_lines(draft.get("body"))},
    )
    print(f"published: {tag} {sha[:7]} ({len(files)} files)")
    return 0


def next_patch(version):
    """`x.y.z` -> `x.y.(z+1)`; any other shape is an error, never a guess."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise CandidateError(f"cannot bump version {version!r}: not x.y.z")
    major, minor, patch = match.groups()
    return f"{major}.{minor}.{int(patch) + 1}"


def cmd_bump(args):
    """Move `main` to the version after the one just released."""
    released = args.released.removeprefix("v")
    nxt = next_patch(released)
    if args.dry_run:
        # Read the tree as it is: a dry run must not touch the checkout.
        carried = mise_version()
        if carried != released:
            print(f"main already carries {carried}; nothing to bump")
        else:
            print(f"dry run: would bump main to {nxt}")
        return 0
    for attempt in range(1, BUMP_ATTEMPTS + 1):
        # Start from the real tip of main each time: a rejected push means it moved.
        git("fetch", "origin", "main")
        git("checkout", "--detach", "origin/main")
        carried = mise_version()
        if carried != released:
            print(f"main already carries {carried}; nothing to bump")
            return 0
        mise_bump(nxt)
        git("commit", "-am", f"Start v{nxt}")
        try:
            git("push", "origin", "HEAD:main")
        except RuntimeError as err:
            if attempt == BUMP_ATTEMPTS:
                raise CandidateError(f"push to main failed {BUMP_ATTEMPTS} times: {err}") from err
            print(f"push rejected (attempt {attempt}); main moved, trying again")
            continue
        print(f"bumped main to {nxt}")
        # A push made with the workflow's token starts no workflows, so start the
        # next candidate's build by hand. The bump has landed either way: if the
        # dispatch fails, the next merge builds the candidate, so warn, don't fail.
        try:
            ghapi.api("actions/workflows/main.yml/dispatches", "POST", {"ref": "main"})
        except RuntimeError as err:
            print(f"::warning title=main not started::the next merge builds v{nxt}: {err}")
        else:
            print(f"started main for v{nxt}")
        return 0
    return 1  # unreachable: the loop returns or raises


def parser():
    """The argparse tree: one subcommand per step."""
    top = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = top.add_subparsers(dest="command", required=True)
    sub.add_parser("base", help="print the last commit main tested green").set_defaults(run=cmd_base)
    for name, func, text in (
        ("record", cmd_record, "put the build on the standing draft"),
        ("fetch", cmd_fetch, "download the candidate and check it"),
        ("publish", cmd_publish, "upload signed files and publish the draft"),
    ):
        one = sub.add_parser(name, help=text)
        one.add_argument("--dir", default="dist")
        if name == "publish":
            one.add_argument("--dry-run", action="store_true")
        one.set_defaults(run=func)
    bump = sub.add_parser("bump", help="move main to the next version")
    bump.add_argument("--released", required=True, help="the version just released")
    bump.add_argument("--dry-run", action="store_true")
    bump.set_defaults(run=cmd_bump)
    return top


def main(argv=None):
    """Run one subcommand; a refusal prints one message and exits 1."""
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except (CandidateError, RuntimeError) as err:
        print(f"candidate: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
