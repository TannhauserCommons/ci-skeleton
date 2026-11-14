"""The skeleton's own workflow rules, on top of actionlint.

Standard library only, so there is no YAML parser: workflows are read by
indentation, the way GitHub workflows are written (`jobs:` at column 0, job ids
one level in, job keys one level further, steps as `- ` list items).
"""

import argparse
import glob
import re
import sys

SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")
KEY = re.compile(r"""^(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_.-]+))\s*:(?:\s+(.*)|)$""")
BLOCK = re.compile(r"^[|>][+-]?\d?[+-]?$")
# What would make a `mise run` line more than one command.
CHAINED = re.compile(r"[;&|`<>]|\$\(")
MISE_RUN = re.compile(r"^mise run (\S+)")
# `${{ a && b || c }}` is one value to the shell, whatever operators it holds.
EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
PULL_REQUEST_TARGET = re.compile(r"\bpull_request_target\b")


def strip_comment(line):
    """The line without a trailing `# comment`; a `#` inside quotes is text."""
    quote = None
    for i, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def content_of(line):
    """The line's text without indentation or comment; empty for blank and comment lines."""
    return strip_comment(line).strip()


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_key(content):
    """`(key, value)` of a `key: value` line, or None. The value is unquoted."""
    match = KEY.match(content)
    if not match:
        return None
    key = next(group for group in match.groups()[:3] if group)
    return key, unquote(match.group(4) or "")


def read_keys(lines, start, end, key_indent, scalars):
    """The `key: value` pairs at exactly `key_indent` in `lines[start:end]`.

    Returns `{key: (line index, value)}`. For the `scalars` keys a value that
    is empty (the value sits on the next line) or a block scalar (`|`, `>`) is
    read from the more indented lines below; a block's value is its non-blank
    lines joined by newlines, so one command is one line.
    """
    found = {}
    i = start
    while i < end:
        parsed = parse_key(content_of(lines[i])) if indent_of(lines[i]) == key_indent else None
        if not parsed:
            i += 1
            continue
        (key, value), at = parsed, i
        i += 1
        if key in scalars and (value == "" or BLOCK.match(value)):
            is_block = value != ""
            body = []
            while i < end and (not lines[i].strip() or indent_of(lines[i]) > key_indent):
                # A block's body is script text: a `#` there is not a YAML comment.
                body.append((lines[i] if is_block else strip_comment(lines[i])).strip())
                i += 1
            body = [text for text in body if text]
            if is_block:
                value = "\n".join(body)
            elif body:
                value = unquote(body[0])
        found[key] = (at, value)
    return found


def section_end(lines, start, key_indent, allow_dash=False):
    """Index after the block that begins at `start`, whose parent key is at `key_indent`."""
    for i in range(start, len(lines)):
        content = content_of(lines[i])
        if not content:
            continue
        indent = indent_of(lines[i])
        if indent < key_indent or (indent == key_indent and not (allow_dash and content[0] == "-")):
            return i
    return len(lines)


def check_pin(value):
    """A message when `uses:` is not a SHA pin (local paths are fine), else None."""
    if value.startswith("./") or SHA_PIN.match(value):
        return None
    return f"uses `{value}` is not pinned to a 40-hex commit SHA"


def check_step(sub, key_indent):
    """Findings `(offset in sub, message)` for one step whose keys sit at `key_indent`."""
    keys = read_keys(sub, 0, len(sub), key_indent, ("uses", "run", "name"))
    found = []
    if "uses" in keys:
        message = check_pin(keys["uses"][1])
        if message:
            found.append((keys["uses"][0], message))
    if "run" in keys:
        at, value = keys["run"]
        task = MISE_RUN.match(value)
        if "\n" in value or not task or CHAINED.search(EXPRESSION.sub("", value)):
            found.append((at, "run is not a single `mise run ...` line"))
        else:
            # The arguments stay out of the name: after.py matches on `mise run <task>`.
            want = f"mise run {task.group(1)}"
            if "name" not in keys:
                found.append((at, f"step has no name; it must be exactly `{want}`"))
            elif keys["name"][1] != want:
                found.append((keys["name"][0], f"step name `{keys['name'][1]}` must be exactly `{want}`"))
    return found


def check_steps(lines, start, end):
    """Findings `(line index, message)` for the `- ` items in `lines[start:end]`."""
    items = [i for i in range(start, end) if content_of(lines[i]).startswith("-")]
    if not items:
        return []
    item_indent = indent_of(lines[items[0]])
    items = [i for i in items if indent_of(lines[i]) == item_indent]
    found = []
    for n, first in enumerate(items):
        last = items[n + 1] if n + 1 < len(items) else end
        sub = lines[first:last]
        # Turn the `- ` into spaces so the first key lines up with the others.
        dash = sub[0].lstrip(" ")
        rest = dash[1:].lstrip(" ")
        column = indent_of(sub[0]) + len(dash) - len(rest)
        sub[0] = " " * column + rest
        if not rest:
            nxt = next((l for l in sub[1:] if content_of(l)), None)
            if nxt is None:
                continue
            column = indent_of(nxt)
        found += [(first + at, message) for at, message in check_step(sub, column)]
    return found


def check_job(lines, start, end):
    """Findings `(line index, message)` for the job whose id line is `lines[start]`."""
    first = next((i for i in range(start + 1, end) if content_of(lines[i])), None)
    if first is None:
        return []
    key_indent = indent_of(lines[first])
    keys = read_keys(lines, start + 1, end, key_indent, ("uses",))
    found = []
    if "uses" in keys:
        # A job that calls a reusable workflow cannot set timeout-minutes at all.
        message = check_pin(keys["uses"][1])
        if message:
            found.append((keys["uses"][0], message))
    elif "timeout-minutes" not in keys:
        found.append((start, "job has no timeout-minutes"))
    if "steps" in keys:
        steps = keys["steps"][0] + 1
        found += check_steps(lines, steps, section_end(lines, steps, key_indent, allow_dash=True))
    return found


def lint_text(text):
    """All findings for one workflow, as sorted `(line number, message)`."""
    lines = text.splitlines()
    found = []
    top = {}
    for i, line in enumerate(lines):
        parsed = parse_key(content_of(line)) if indent_of(line) == 0 else None
        if parsed:
            top.setdefault(parsed[0], i)
        if PULL_REQUEST_TARGET.search(strip_comment(line)):
            found.append((i, "pull_request_target is not allowed"))
    if "permissions" not in top:
        found.append((0, "no top-level permissions:"))
    if "jobs" in top:
        start = top["jobs"] + 1
        end = section_end(lines, start, 0)
        body = [i for i in range(start, end) if content_of(lines[i])]
        if body:
            job_indent = indent_of(lines[body[0]])
            jobs = [i for i in body if indent_of(lines[i]) == job_indent and parse_key(content_of(lines[i]))]
            for n, job in enumerate(jobs):
                found += check_job(lines, job, jobs[n + 1] if n + 1 < len(jobs) else end)
    return sorted((i + 1, message) for i, message in found)


def lint_file(path):
    """Findings for one file as `file:line: message` strings."""
    shown = path.replace("\\", "/")
    try:
        with open(path, encoding="utf-8") as src:
            text = src.read()
    except OSError as err:
        return [f"{shown}:0: cannot read: {err}"]
    return [f"{shown}:{line}: {message}" for line, message in lint_text(text)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="default: .github/workflows/*.yml")
    args = parser.parse_args(argv)
    files = args.files or sorted(
        glob.glob(".github/workflows/*.yml") + glob.glob(".github/workflows/*.yaml")
    )
    findings = [finding for path in files for finding in lint_file(path)]
    for finding in findings:
        print(finding)
    if findings:
        return 1
    print(f"lint: ok ({len(files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
