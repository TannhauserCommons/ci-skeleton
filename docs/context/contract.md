# The contract

What a project that copies the skeleton fills in, and what the shared scripts promise. Code and tests outrank this page.

## What is copied, what is filled in

| Path | Kind | Notes |
| --- | --- | --- |
| `.github/workflows/check.yml`, `main.yml`, `release.yml`, `after.yml` | Copied, then the blocks marked `# PROJECT` are edited | Job list, OS and target matrix |
| `.github/ci/*.py`, `.github/ci/tasks.toml` | Copied unchanged | Shared scripts and the `ci:*` tasks that call them |
| `.github/ci/paths.yml` | Per project | Path map: one key per path-gated job id in `check.yml` |
| `mise.toml` | Per project | Tool pins, the project tasks below, and `[task_config] includes = [".github/ci/tasks.toml"]` |
| `switches.toml` | Per project | The one list of feature switches, at the repo root |

Everything else in this repo (`fixture/`, `tests/ci/`, `docs/`) proves the skeleton and is not copied.

## Project tasks

Every CI step is `mise run <task>`. A step's `name:` is exactly `mise run <task>`, without the arguments, because `after.py` tells infra failures from real ones by step name.

| Task | Runs in | Must do | Exit code |
| --- | --- | --- | --- |
| `check` | `check` (so also `main`) | Lint and tests that give the same answer every time. One retry inside the test runner to classify flakes; a pass on retry stays green and is written to `flakes/` | Non-zero on a real failure |
| `budgets` | `check` | Exact metrics (bytes, counts) against the committed budgets file | Non-zero when over |
| `looks <base>` | `check` | Render key screens on `<base>` and on the change, each twice, with the base's harness; exact compare. `VISUAL_CHANGE=1` declares an intended diff | Non-zero on an undeclared diff or an unstable screen |
| `build` | `main` | Build this runner's release target, unsigned, into `dist/` | Non-zero on failure |
| `smoke` | `main` | Install and update tests on the files in `dist/` | Non-zero on failure |
| `compare <base>` | `main` | Time `<base>` against the change, interleaved, same machine, with a base-against-base noise floor; write the table to `dist/NOTES.md` | Non-zero only past the noise floor |
| `publish` | `release` | Sign the files in `dist/`, check the signatures, deploy to production. Runs in the candidate's own tree. Must be safe to run twice, also on files it already signed. `DRY_RUN=1` signs and checks but deploys nothing | Non-zero on failure |
| `version` | `main`, `release` | Print the version `main` carries (the next release), as the last line of stdout | |
| `bump <version>` | `release` | Write `<version>` into every file that holds the version | |

The spec names the first seven; `smoke`, `version` and `bump` are what the candidate and release steps need on top.

### Flake reports

A job that ran tests uploads the folder `flakes/` as an artifact named `flakes-<job>-<os>`. Files in it:

- `*.xml`: JUnit. A `<testcase>` with a `<flakyFailure>`, `<flakyError>`, `<rerunFailure>` or `<rerunError>` child and no `<failure>` or `<error>` child is flaky (nextest writes this). Test id: `classname::name`.
- `*.txt`: one test id per line, for runners without JUnit flake marks.

## Settings the scripts read

| Variable | Default | Meaning |
| --- | --- | --- |
| `CI_BRANCH` | `main` | The branch that carries the next release: the one `main.yml` runs on and candidates are cut from. A project that keeps its default branch for released commits only sets another name (and the same name in `main.yml`'s trigger) |
| `CI_REPO` | the checkout's remote | The repository to look at when run outside CI, for a source tree whose releases live in another repository |
| `RELEASE_NOTES` | empty | Set by `release.yml` from its `notes` input: a note that goes at the top of the release text |
| `BUMP_KEY_SET` | unset | Set by `release.yml`: `true` when the secret `BUMP_KEY` exists. Absent in a project that has removed the bump step |

## Shared tasks (`.github/ci/tasks.toml`)

| Task | Script | Does |
| --- | --- | --- |
| `status` | `status.py` | What an agent runs first: red `main`, red PRs, PRs stuck over an hour, the current candidate |
| `ci:select` | `paths.py` | Turns the path filter's answer into the job list `check` runs and the skips the gate allows |
| `ci:lint` | `lint.py`, `actionlint` | Workflow lint plus the skeleton's own rules (SHA pins, timeouts, `mise run` steps) |
| `ci:switches` | `switches.py check` | Fails on an expired, tombstoned, duplicate or malformed switch, or more than the cap |
| `ci:after` | `after.py` | After a `check` or `main` run: one infra rerun, flake issues, one red-`main` issue per failed check |
| `ci:base` | `candidate.py base` | Prints the last commit `main` tested green |
| `ci:candidate` | `candidate.py record` | Green `main`: puts `dist/` on the standing release draft as the candidate |
| `ci:fetch` | `candidate.py fetch` | Release: checks every hash, then puts the candidate's commit in `candidate/` and its files in `candidate/dist` |
| `ci:publish` | `candidate.py publish` | Release: uploads the signed files, publishes the draft (which creates the tag, last), files switch-removal issues |
| `ci:bump` | `candidate.py bump` | Release: moves `main` to the next version |

## Script interfaces

All scripts are Python 3.11+ standard library only, run from the repo root, and reach GitHub only through `.github/ci/ghapi.py` (so tests stub one module). They print short, plain lines. A script that sets step outputs appends `name=value` lines to the file named by `GITHUB_OUTPUT` when that variable is set.

### `paths.py`

`python .github/ci/paths.py [--all]`. Reads the top-level keys of `.github/ci/paths.yml` (the path-gated job ids) and the env var `CHANGES` (JSON array of keys whose paths changed, from dorny/paths-filter; empty or unset means none). Outputs:
- `run`: JSON array of the gated job ids to run (`--all`: every key);
- `skippable`: comma-separated gated job ids not in `run` (the only jobs the gate lets skip).

### `lint.py`

`python .github/ci/lint.py [files...]` (default: `.github/workflows/*.yml`). Fails, one line per finding as `file:line: message`, on:
- a `uses:` that is not a 40-hex SHA pin (local `./` paths are fine);
- a top-level `permissions:` that is anything but `contents: read`;
- a `jdx/mise-action` step whose `cache_save` is not `false` or a comparison of `github.ref` with the release branch (only the release branch saves caches);
- an expression in a `run:` line that reads anything but `inputs`, `steps.*.outputs`, `matrix`, and a few fixed `github` values (untrusted text must go through `env`);
- a job without `timeout-minutes` (jobs that are only `uses: ./...` are exempt);
- a `run:` whose command is not a single `mise run ...` line;
- a `run:` step whose `name:` is not exactly `mise run <task>`;
- `pull_request_target` anywhere.

### `switches.py`

`switches.toml`:

```toml
cap_per_area = 2          # the most switches one release area may hold
tombstones = ["old-name"] # names of removed switches, never reused

[[switch]]
name = "shout"            # [a-z0-9-]+
what = "Greets in capitals"
owner = "owner"
area = "cli"
default = "off"           # "off" or "on"; a one-line commit flips it
ready = false             # true: finished, waiting for the owner to turn it on
expires = "0.3.0"         # a release (x.y.z) or a date (YYYY-MM-DD)
```

- `switches.py check [--version X]`: validates the file. A release expiry is past when the version `main` carries (`--version`, else `mise run version`) is that release or later. A date expiry is past when HEAD's commit date (`git log -1 --format=%cs`) is after it, so the same commit always gets the same answer. Prints every finding and exits 1 if any.
- `switches.py list [--off|--on|--ready]`: names, one per line (for a test task that runs each off switch's tests with it on).
- `switches.py notes`: Markdown for the release draft: switches on in this release, and switches ready to turn on. Empty output when there are none.
- `switches.py shipped`: after a release: one deduplicated issue per `on` switch asking for its removal before the next release (label `switch-removal`).

### `after.py`

`python .github/ci/after.py <run_id>`. Reads the run and its jobs through the API.

1. Conclusion `skipped`, or `cancelled` on anything but a `main` run that had started a job: do nothing (cancelled is neither red nor green). GitHub also concludes a run `cancelled` when a job hits its timeout, and nothing tells that from a cancel by hand, so a `main` run stopped after it started is handled as a failure in step 4, with no rerun: main is unverified either way, and the next green run closes the issue.
2. Conclusion `failure` and `run_attempt == 1`: if **every** failed job is an infra failure, rerun the failed jobs once and stop. A failed job is infra when its first failed step comes before the job's first step named `mise run ...`, or when no step is recorded as failed (a lost runner). The `gate` job is left out: it fails because another job did. Never by log text.
3. Flakes (any conclusion): download, by id, the `flakes-*` artifacts uploaded since this attempt of the run started (a rerun still lists the artifacts of the jobs it carried over from an earlier attempt, whose flakes were filed then), and for each flaky test open or comment on one issue titled `flaky: <test id>` (label `flaky`); a recurrence comments `Flaked again: <run> at <sha>`. At most 10 new issues per run. A run whose `head_repository` (from the run object in the API) is not this repository, such as a fork's pull request, gets no flake issues: its artifacts are the fork author's text, so they are not downloaded; the job log says so. Its infra rerun still happens (once, with the fork's own read-only token and no secrets). It can never be a `main` run, which needs a push event.
4. Workflow `main` only, and only when no later `main` run has finished since: on failure, for each failed job (the gate only when it failed alone), open or update the issue titled `main is red: <job name>` and labelled `main-red`, naming the commit range since the last green `main` run, its commits and authors, the job and the run link; the rule is fix or revert, revert by default. A run with no failed job recorded uses the bare title `main is red`. In the same run, close each open `main-red` issue whose job succeeded. On success close every open `main-red` issue with a comment. One issue per check, so checks that break for different reasons are fixed and closed on their own. On success also close each open `flaky: <test id>` issue whose test did not flake in this run and has had 30 green `main` push runs made since it last flaked (the newest `Flaked again` comment, else the issue's opening), with a comment naming the count, the runs and the reason; a red run neither counts nor resets. 30 clean runs put the flake rate under 10% (rule of three, 95% confidence). A later flake opens a new issue. A `flaky`-labelled issue with another title is left alone. The count is of green runs, not of runs of that test: a `main` that skips jobs by path counts a run that skipped the test.

Infra reruns are counted from run history: `check` and `main` runs with `run_attempt > 1`.

### `candidate.py`

The candidate lives on the **standing release draft**: the one draft release whose body ends with the marker `<!-- ci:candidate -->`. Its assets are the files of `dist/` plus `candidate.json`:

```json
{"sha": "...", "version": "0.2.0", "run_id": 1, "run_attempt": 1, "files": {"name": "sha256-hex"}}
```

- `base`: outputs `base`, the `head_sha` of the newest successful `main` run on `main` (nothing when there is none).
- `record [--dir dist]`: when the tag `v<version>` already exists the commit is not a candidate (it landed between a released commit and the release's bump): prints a notice and exits 0. Otherwise writes `candidate.json`, builds the notes (commits since the last release tag grouped by the area before the first `: ` of the subject, leaving out commits that only name the version, `switches.py notes`, then `dist/NOTES.md` if present, then the candidate's commit and the marker), creates or updates the draft (`tag_name` `v<version>`, `target_commitish` the commit), deletes its old assets with `candidate.json` first, uploads the new files with `candidate.json` last. `NOTES.md` is not uploaded. File names must be ones GitHub keeps as they are (letters, digits, `.`, `_`, `+`, `-`).
- `fetch [--tree candidate]`: refuses when `BUMP_KEY_SET` is set and not `true` (so a release that could not finish never starts, dry runs included). Downloads the draft's assets, checks every hash against `candidate.json`, and fails on any mismatch, missing or extra file, a manifest that disagrees with the draft, or a commit that is not on the release branch. Then puts the candidate's commit in `<tree>` (a git worktree) and its files in `<tree>/dist`. Outputs `published=false`, `sha`, `version`, `tag`. Before looking for a draft it reads the version the checked-out tree carries: when that version is already published (a rerun of a release that published and then failed; a rerun checks out the same commit), it downloads nothing and outputs `published=true` with the same three values, whatever drafts exist, so the rerun only finishes what is left. With no draft and nothing published under that version: fails, there is no candidate.
- `publish [--dir candidate/dist] [--dry-run]`: refuses if the draft no longer matches `candidate.json` (a newer candidate replaced it). Writes `signed.json` (the hashes of every file as it is now, after signing, added to those of earlier attempts) and uploads it first, then every file in the dir except the manifests and `NOTES.md` (replacing same-named assets), runs `switches.py shipped`, publishes the draft (with `RELEASE_NOTES`, when given, at the top of its text), which creates the tag at the candidate's commit, and last removes the two manifests from the release. A release that died between the first upload and the publish can be rerun: `fetch` accepts a file that matches either manifest. If an upload itself was cut off and lost a file, rerun the last `main` run: it records the candidate afresh. With no draft and the tag present it is already published: exits 0. `--dry-run` prints what it would do.
- `bump --released <version> [--dry-run]`: on `origin/main`: if `mise run version` still equals the released version, runs `mise run bump <next patch>`, commits `Start v<next>` and pushes to `main` (three attempts if `main` moved); if `main` already carries another version, does nothing. The push must be made with a deploy key (the workflow's checkout sets it up from the secret `BUMP_KEY`; `bump` refuses without `BUMP_KEY_SET=true`): unlike a push by the workflow token it can pass a ruleset on `main`, and it starts the next `main` run.

### `status.py`

`python .github/ci/status.py`. Prints, in this order and only the lines that apply: `main: red` with the issue link, or with the newest finished `main` run when that did not succeed (a startup failure never reaches `after`), else `main: green`, each open PR whose `gate` check failed, each open PR whose `gate` is not green more than an hour after its last push, each open PR that is green and waiting for the person who approves merges (drafts excepted), the candidate (`candidate: v<version> <short sha>`, or `candidate: none`), and the number of open `flaky` issues. Exits 0 always: it reports, it does not gate.

### `ghapi.py`

- `repo()`: `owner/name` from `GITHUB_REPOSITORY`, else `gh repo view`.
- `gh(*args, stdin=None, check=True, out=None)`: runs `gh`, returns stdout; with `out` (a file opened for binary writing) stdout goes there as bytes and it returns `None`.
- `api(path, method="GET", body=None, paginate=False)`: REST call; `path` is relative to `repos/{repo}/` unless it starts with `/`. Returns parsed JSON (`None` for an empty body).
- `ensure_label(name, color="d73a4a", description="")`: creates the label if missing (`upsert_issue` calls it).
- `find_issue(label, title=None)`: the open issue with that label (and exact title), or `None`.
- `upsert_issue(label, title, body, comment=None)`: creates the issue, or adds `comment` (default `body`) to the open one. Returns `(number, created)`.
- `close_issue(number, comment)`.
- `output(name, value)`: appends to `GITHUB_OUTPUT` when set, and prints `name=value`.
