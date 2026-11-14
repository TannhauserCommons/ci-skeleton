"""The one door to GitHub: every script calls `gh` through here, so tests stub this module."""

import json
import os
import subprocess

_repo = None


def repo():
    """`owner/name` of the repository the scripts act on."""
    global _repo
    if _repo is None:
        _repo = os.environ.get("GITHUB_REPOSITORY") or gh(
            "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"
        ).strip()
    return _repo


def gh(*args, stdin=None, check=True):
    """Run `gh` and return its stdout. A failure raises with gh's own message."""
    done = subprocess.run(
        ["gh", *args], input=stdin, capture_output=True, text=True, encoding="utf-8"
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def api(path, method="GET", body=None, paginate=False):
    """A REST call. `path` is relative to `repos/{repo}/` unless it starts with `/`.

    With `paginate`, list endpoints come back as one list and object endpoints
    that wrap a list (`{"total_count": .., "jobs": [..]}`) as a list of pages.
    """
    url = path.lstrip("/") if path.startswith("/") else f"repos/{repo()}/{path}"
    args = ["api", "--method", method, url]
    if paginate:
        args += ["--paginate", "--slurp"]
    if body is not None:
        args += ["--input", "-"]
    out = gh(*args, stdin=None if body is None else json.dumps(body))
    if not out.strip():
        return None
    data = json.loads(out)
    if paginate and data and all(isinstance(page, list) for page in data):
        return [item for page in data for item in page]
    return data


def ensure_label(name, color="d73a4a", description=""):
    """Create the label if the repository lacks it."""
    labels = api("labels?per_page=100", paginate=True) or []
    if not any(label["name"] == name for label in labels):
        api("labels", "POST", {"name": name, "color": color, "description": description})


def find_issue(label, title=None):
    """The open issue with that label (and exact title), or None."""
    issues = api(f"issues?state=open&labels={label}&per_page=100", paginate=True) or []
    for issue in issues:
        if "pull_request" not in issue and (title is None or issue["title"] == title):
            return issue
    return None


def upsert_issue(label, title, body, comment=None):
    """One issue per (label, title): create it, or comment on the open one.

    Returns `(number, created)`.
    """
    issue = find_issue(label, title)
    if issue:
        api(f"issues/{issue['number']}/comments", "POST", {"body": comment or body})
        return issue["number"], False
    ensure_label(label)
    made = api("issues", "POST", {"title": title, "body": body, "labels": [label]})
    return made["number"], True


def close_issue(number, comment):
    api(f"issues/{number}/comments", "POST", {"body": comment})
    api(f"issues/{number}", "PATCH", {"state": "closed", "state_reason": "completed"})


def output(name, value):
    """A step output: appended to GITHUB_OUTPUT in CI, always printed."""
    print(f"{name}={value}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            out.write(f"{name}={value}\n")
