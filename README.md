# ci-skeleton

One CI/CD shape for any project, made to be copied. It is built for a small team (or one person) whose changes are mostly written by coding agents: checks never make anyone wait, a required status can't hang or pass by accident, flaky tests and runner hiccups don't turn things red, and a release takes minutes because it publishes files that were already built and tested.

This repository is the skeleton and its proof: the files under `.github/` are what you copy, and the tiny project in `fixture/` fills them in so this repo's own CI exercises every path.

## The shape

| Workflow | Runs on | Does |
| --- | --- | --- |
| `check` | Every pull request | The project's jobs in parallel, each only when its paths changed, behind one always-running job, `gate`, which is the only required status. Only checks that give the same answer every time. No secrets |
| `main` | Every push to `main` | Everything `check` runs, on the merged result; builds every release target; tests those files; then parks them on a draft release as the **release candidate**. Blocks nothing: red opens one issue |
| `release` | Your command (`workflow_dispatch`) | Takes the newest green candidate, signs it, publishes the draft (which creates the tag, last) and moves `main` to the next version. No rebuild, so a failure costs a rerun, never a version |
| `after` | When `check` or `main` finishes | Reruns an infra failure once, files one issue per flaky test, opens or closes the "main is red" issue |

`main` always carries the *next* version, so every candidate already has its version in its bytes. Small changes pile up on `main` until you release.

Every step in every workflow is `mise run <task>`. The workflows hold no shell; a project's tasks hold its commands, so the same commands run on a laptop and in CI.

## Copy it into a project

1. Copy `.github/workflows/`, `.github/ci/` and `switches.toml` into the project.
2. In the project's `mise.toml`, pin the tools and include the shared tasks:

   ```toml
   [tools]
   python = "3.14.10"     # the shared scripts need Python 3.11 or newer
   actionlint = "1.7.12"

   [task_config]
   includes = [".github/ci/tasks.toml"]
   ```

   Run `mise lock` and commit `mise.lock`, so tool downloads are checked by hash.
3. Write the project's tasks (the table below) in `mise.toml`.
4. Edit the blocks marked `# PROJECT` in the workflows: the job list and OS matrix in `check.yml`, the build targets in `main.yml`, the commit identity in `release.yml`. Every job you add to `check.yml` goes in the gate's `needs:`.
5. Edit `.github/ci/paths.yml`: one key per path-gated job id, with the paths that should trigger it. A job with no key always runs.
6. On GitHub:
   - require the status `gate` on `main` (a ruleset or branch protection; do not require branches to be up to date);
   - create an Environment named `release`, limited to `main`, holding the signing keys and deploy tokens. On a public repository, make yourself its required reviewer;
   - add a deploy key with write access and put its private half in the secret `BUMP_KEY` of that Environment. The release pushes its one-line "Start v\<next>" commit with it. If `main` has a ruleset, give deploy keys its only bypass. (The workflow's own token can't do this job: its pushes start no workflow runs, and on a personal account it can't be given a bypass.)
7. Run `mise run ci:lint` and `mise run check`, open a pull request, and watch `gate`.

`docs/context/contract.md` is the precise version of everything below.

## The tasks a project fills in

| Task | Runs in | Does |
| --- | --- | --- |
| `check` | `check` | Lint and tests that give the same answer every time. Retries a failed test once to classify it; a pass on retry stays green and is listed in `flakes/` |
| `budgets` | `check` | Exact metrics (bytes, counts) against a committed budgets file |
| `looks <base>` | `check` | Renders key screens on the base and on the change, each twice, and compares exactly |
| `build` | `main` | Builds this runner's release target, unsigned, into `dist/` |
| `smoke` | `main` | Install and update tests on the files in `dist/` |
| `compare <base>` | `main` | Times the base against the change on the same machine, with a noise floor |
| `publish` | `release` | Signs the files in `dist/`, checks the signatures, deploys |
| `version` | `main`, `release` | Prints the version `main` carries |
| `bump <version>` | `release` | Writes a version into the project |

Leave out what a project doesn't have (no screens: no `looks` job). `fixture/tasks/` has a small, honest example of each.

## What keeps it from lying

- **The gate.** `check` has no path filter on the workflow itself (that leaves a required status pending forever). A `changes` job decides which jobs run; `gate` runs with `if: !cancelled()` and fails on any failed or cancelled job, allowing a skip only for a job the path map left out. A plain `needs:` summary job would be skipped when a job fails, and skipped counts as passing.
- **Same answer every time.** Anything that depends on a network, a live service or a clock belongs in `main`, not `check`. Time is measured as a ratio against the last tested commit on the same machine and acts after merge.
- **Flaky is classified, not hidden.** One retry inside the test runner, only to classify. A pass on retry stays green and `after` files one issue per test (label `flaky`). There is no quarantine list.
- **Infra failures rerun once,** decided by which step failed (before the project's own command, or a lost runner), never by log text.
- **Cancelled is neither red nor green.** A newer push cancels a PR's older run; `main` runs one at a time and a newer pending run replaces an older pending one; `release` never cancels.
- **Pinned.** Actions by commit SHA, tools by version and hash, a timeout on every job. `mise run ci:lint` enforces this, and that every step is a `mise run`.
- **Only `main` saves caches;** pull requests read them.
- **Least privilege.** `contents: read` by default, no `pull_request_target`, pull requests get no secrets; issues and reruns are handled by `after`, which runs from `main`'s code.

## Releasing

`mise run status` shows the candidate. To release it, run the `release` workflow (`gh workflow run release.yml`, or the button). With `dry_run` it fetches, signs and checks, and publishes nothing.

To release a different version than the one `main` carries (a minor bump), land a commit that runs `mise run bump <version>`; `main` builds that candidate, then release.

If a release fails, rerun it. Nothing is tagged until the last step, and a rerun after the publish only finishes the bump. If a rerun says the candidate failed its checks, rerun the last `main` run instead: it records the candidate afresh.

A `main` run that is cancelled after it started counts as red, because GitHub reports a job that hit its timeout the same way. The next green run closes the issue.

## Feature switches

`switches.toml` is the one list of switches. A switch starts `off`; off must be exactly today's app. A finished feature waits with `ready = true`; a one-line commit sets `default = "on"` and it ships in the next release; after that release the code for off is deleted and the name goes in `tombstones`, never to be reused. `mise run ci:switches` fails on an expired or tombstoned switch and on more switches in one area than `cap_per_area`. The release draft lists what is on and what is ready.

## Commands

```sh
mise install        # the pinned tools
mise run status     # red main, red or stuck pull requests, the candidate, open flakes
mise run check      # this repo: the scripts' tests and the fixture's tests
mise run ci:lint    # actionlint plus the skeleton's own workflow rules
```

`gh` acts with the credential git has for the repository's remote, so on a machine with several GitHub accounts each project uses its own.

## License

MIT. See `LICENSE`.
