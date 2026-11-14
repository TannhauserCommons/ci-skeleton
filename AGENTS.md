# ci-skeleton

The one CI/CD every project copies: three workflows (`check`, `main`, `release`) behind one required status, a fourth (`after`) that reacts to their results, the shared Python scripts they call, and a tiny fixture project whose own CI proves it all works. GitHub Actions, `mise` tasks, Python standard library only.

## Start here

`docs/context/INDEX.md`: find the page for your task, then open the files it lists. `README.md` says how a project copies the skeleton.

## Commands

```sh
mise install          # the pinned tools (python, actionlint)
mise run status       # first thing every session: red main, red PRs, the candidate
mise run check        # script tests and the fixture's tests; run before opening a PR
mise run ci:lint      # actionlint plus the skeleton's own workflow rules
```

## Changes

- Git history is the changelog: subject says what changed, body says why and how it was verified. No change logs, session notes or handoff files.
- Stage only files you changed; other agents may have uncommitted work here.
- A change that makes a `docs/context/` page wrong updates that page in the same commit.
- Everything under `.github/` is copied into other repos. Keep it free of anything about this repo's fixture; per-project parts are marked `# PROJECT` in the workflows.
- Scripts in `.github/ci/` use the Python standard library only, reach GitHub only through `ghapi.py`, and each has tests in `tests/ci/` that stub it.
- Workflows, `mise` tasks, test harnesses and budget files are protected paths: the owner merges changes to them.

## Boundaries

- Never commit secrets or `.env*`.
- No Co-Authored-By or AI attribution in commits or PRs.
