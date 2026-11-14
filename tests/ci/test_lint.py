import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2] / ".github" / "ci"))

import lint  # noqa: E402

SHA = "a" * 40

CLEAN = f"""\
name: check

on:
  pull_request:
  workflow_call:

permissions:
  contents: read

concurrency:
  group: check-${{{{ github.ref }}}}

jobs:
  # Reusable job: only a `uses:`, so no timeout-minutes.
  shared:
    uses: ./.github/workflows/check.yml
    with:
      flag: true

  test:
    name: test (${{{{ matrix.os }}}})
    runs-on: ${{{{ matrix.os }}}}
    timeout-minutes: 20
    strategy:
      fail-fast: false
      matrix:
        os:
          - ubuntu-latest
          - windows-latest
    steps:
      - uses: actions/checkout@{SHA} # v7.0.1
        with:
          fetch-depth: 0
      - uses: "jdx/mise-action@{SHA}"
      - name: mise run check
        run: mise run check
      - run: mise run looks ${{{{ github.event.pull_request.base.sha || github.event.before }}}}
        name: "mise run looks"
      - name: mise run build
        run: |
          mise run build
      - name: mise run budgets
        env:
          VISUAL_CHANGE: 1
        run: 'mise run budgets'

  gate:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    needs: [test]
    steps:
      - uses: ./.github/actions/gate
"""


def workflow(jobs, head="permissions:\n  contents: read\n"):
    return f"on:\n  pull_request:\n\n{head}\njobs:\n{jobs}"


def job(steps, timeout="    timeout-minutes: 5\n"):
    return f"  build:\n    runs-on: ubuntu-latest\n{timeout}    steps:\n{steps}"


def findings(text):
    return lint.lint_text(text)


def messages(text):
    return [message for _, message in findings(text)]


class CleanTest(unittest.TestCase):
    def test_realistic_workflow_is_clean(self):
        self.assertEqual(findings(CLEAN), [])

    def test_two_space_steps_flush_with_steps_key(self):
        text = workflow(job(f"    - uses: actions/checkout@{SHA}\n    - name: mise run check\n      run: mise run check\n"))
        self.assertEqual(findings(text), [])

    def test_uses_value_on_its_own_line(self):
        text = workflow(job(f"      - uses:\n          actions/checkout@{SHA}\n"))
        self.assertEqual(findings(text), [])

    def test_dash_on_its_own_line(self):
        text = workflow(job(f"      -\n        uses: actions/checkout@{SHA}\n"))
        self.assertEqual(findings(text), [])


class UsesTest(unittest.TestCase):
    def test_tag_is_a_finding(self):
        text = workflow(job("      - uses: actions/checkout@v4\n"))
        self.assertEqual(findings(text), [(12, "uses `actions/checkout@v4` is not pinned to a 40-hex commit SHA")])

    def test_branch_and_short_sha_are_findings(self):
        for ref in ("main", "abc1234", "A" * 40):
            with self.subTest(ref=ref):
                text = workflow(job(f"      - uses: org/repo@{ref}\n"))
                self.assertEqual(len(findings(text)), 1)

    def test_no_ref_is_a_finding(self):
        text = workflow(job("      - uses: actions/checkout\n"))
        self.assertEqual(len(findings(text)), 1)

    def test_sha_with_comment_and_quotes_is_fine(self):
        for value in (f"actions/checkout@{SHA} # v7.0.1", f"'actions/checkout@{SHA}'", f'"actions/checkout@{SHA}" # x'):
            with self.subTest(value=value):
                self.assertEqual(findings(workflow(job(f"      - uses: {value}\n"))), [])

    def test_sha_in_a_subpath_action_is_fine(self):
        text = workflow(job(f"      - uses: github/codeql-action/init@{SHA}\n"))
        self.assertEqual(findings(text), [])

    def test_local_path_is_fine(self):
        text = workflow(job("      - uses: ./.github/actions/setup\n"))
        self.assertEqual(findings(text), [])

    def test_docker_image_is_a_finding(self):
        text = workflow(job("      - uses: docker://alpine:3\n"))
        self.assertEqual(len(findings(text)), 1)

    def test_job_level_reusable_workflow_needs_a_pin_unless_local(self):
        text = workflow("  call:\n    uses: org/repo/.github/workflows/x.yml@main\n")
        self.assertEqual(len(findings(text)), 1)
        text = workflow(f"  call:\n    uses: org/repo/.github/workflows/x.yml@{SHA}\n")
        self.assertEqual(findings(text), [])


class TimeoutTest(unittest.TestCase):
    def test_missing_timeout_is_a_finding_on_the_job_line(self):
        text = workflow(job("      - uses: ./x\n", timeout=""))
        self.assertEqual(findings(text), [(8, "job has no timeout-minutes")])

    def test_timeout_present_is_fine(self):
        self.assertEqual(findings(workflow(job("      - uses: ./x\n"))), [])

    def test_uses_only_job_is_exempt(self):
        self.assertEqual(findings(workflow("  call:\n    uses: ./.github/workflows/x.yml\n")), [])

    def test_each_job_is_judged_alone(self):
        text = workflow(job("      - uses: ./x\n") + "  other:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ./x\n")
        self.assertEqual(messages(text), ["job has no timeout-minutes"])

    def test_timeout_key_inside_a_step_does_not_count(self):
        text = workflow("  build:\n    runs-on: x\n    steps:\n      - uses: ./x\n        timeout-minutes: 3\n")
        self.assertEqual(messages(text), ["job has no timeout-minutes"])


class RunTest(unittest.TestCase):
    def test_plain_command_is_a_finding(self):
        text = workflow(job("      - name: build\n        run: cargo build\n"))
        self.assertEqual(findings(text), [(13, "run is not a single `mise run ...` line")])

    def test_chained_commands_are_findings(self):
        for command in ("mise run a && mise run b", "mise run a; echo", "mise run a | tee x", "mise run a > out", "mise run $(x)"):
            with self.subTest(command=command):
                text = workflow(job(f"      - name: {command}\n        run: {command}\n"))
                self.assertEqual(messages(text), ["run is not a single `mise run ...` line"])

    def test_bare_mise_run_is_a_finding(self):
        text = workflow(job("      - name: mise run\n        run: mise run\n"))
        self.assertEqual(messages(text), ["run is not a single `mise run ...` line"])

    def test_block_with_two_lines_is_a_finding(self):
        text = workflow(job("      - name: mise run a\n        run: |\n          mise run a\n          mise run b\n"))
        self.assertEqual(messages(text), ["run is not a single `mise run ...` line"])

    def test_block_with_other_command_is_a_finding(self):
        text = workflow(job("      - name: x\n        run: |\n          echo hi\n"))
        self.assertEqual(messages(text), ["run is not a single `mise run ...` line"])

    def test_empty_run_is_a_finding(self):
        text = workflow(job("      - run:\n      - uses: ./x\n"))
        self.assertEqual(messages(text), ["run is not a single `mise run ...` line"])

    def test_single_line_block_variants_are_fine(self):
        for indicator in ("|", "|-", ">", ">-", "|+"):
            with self.subTest(indicator=indicator):
                text = workflow(job(f"      - name: mise run a\n        run: {indicator}\n          mise run a\n\n      - uses: ./x\n"))
                self.assertEqual(findings(text), [])

    def test_run_with_trailing_comment_is_fine(self):
        text = workflow(job("      - name: mise run a\n        run: mise run a # why\n"))
        self.assertEqual(findings(text), [])

    def test_run_text_in_a_with_block_is_not_a_step_key(self):
        text = workflow(job(f"      - uses: org/x@{SHA}\n        with:\n          run: cargo build\n"))
        self.assertEqual(findings(text), [])


class NameTest(unittest.TestCase):
    def test_wrong_name_is_a_finding_on_the_name_line(self):
        text = workflow(job("      - name: Build it\n        run: mise run build\n"))
        self.assertEqual(findings(text), [(12, "step name `Build it` must be exactly `mise run build`")])

    def test_name_after_run_is_checked_too(self):
        text = workflow(job("      - run: mise run build\n        name: Build it\n"))
        self.assertEqual(messages(text), ["step name `Build it` must be exactly `mise run build`"])

    def test_missing_name_is_a_finding(self):
        text = workflow(job("      - run: mise run build\n"))
        self.assertEqual(messages(text), ["step has no name; it must be exactly `mise run build`"])

    def test_name_before_or_after_run_matches(self):
        for steps in (
            "      - name: mise run build\n        run: mise run build\n",
            "      - run: mise run build\n        name: mise run build\n",
            "      - run: mise run build\n        name: 'mise run build'\n",
        ):
            with self.subTest(steps=steps):
                self.assertEqual(findings(workflow(job(steps))), [])

    def test_arguments_stay_out_of_the_name(self):
        ok = workflow(job("      - name: mise run looks\n        run: mise run looks origin/main\n"))
        self.assertEqual(findings(ok), [])
        bad = workflow(job("      - name: mise run looks origin/main\n        run: mise run looks origin/main\n"))
        self.assertEqual(messages(bad), ["step name `mise run looks origin/main` must be exactly `mise run looks`"])

    def test_expression_operators_are_not_chaining(self):
        steps = "      - name: mise run ci:select\n        run: mise run ci:select ${{ inputs.all && '--all' || '' }}\n"
        self.assertEqual(findings(workflow(job(steps))), [])


class PullRequestTargetTest(unittest.TestCase):
    def test_key_is_a_finding(self):
        text = "on:\n  pull_request_target:\n    branches: [main]\npermissions: {}\njobs: {}\n"
        self.assertEqual(findings(text), [(2, "pull_request_target is not allowed")])

    def test_list_item_and_inline_forms_are_findings(self):
        for on in ("on: [push, pull_request_target]", "on:\n  - pull_request_target", "on: pull_request_target"):
            with self.subTest(on=on):
                self.assertEqual(messages(f"{on}\npermissions: {{}}\n"), ["pull_request_target is not allowed"])

    def test_comment_is_not_a_finding(self):
        text = "on:\n  pull_request: # never pull_request_target here\n# pull_request_target\npermissions: {}\n"
        self.assertEqual(findings(text), [])

    def test_in_quotes_with_hash_before_is_still_a_finding(self):
        text = "on: pull_request\npermissions: {}\nenv:\n  X: 'a # pull_request_target'\n"
        self.assertEqual(messages(text), ["pull_request_target is not allowed"])


class PermissionsTest(unittest.TestCase):
    def test_missing_top_level_permissions_is_a_finding(self):
        text = "on: push\njobs: {}\n"
        self.assertEqual(findings(text), [(1, "no top-level permissions:")])

    def test_job_level_permissions_do_not_count(self):
        text = job("      - uses: ./x\n").replace("    steps:", "    permissions:\n      contents: read\n    steps:")
        self.assertEqual(messages("on: push\njobs:\n" + text), ["no top-level permissions:"])

    def test_empty_permissions_is_fine(self):
        self.assertEqual(findings("on: push\npermissions: {}\n"), [])

    def test_commented_permissions_does_not_count(self):
        self.assertEqual(messages("on: push\n# permissions:\n"), ["no top-level permissions:"])


class FilesTest(unittest.TestCase):
    def run_main(self, *paths):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = lint.main([str(p) for p in paths])
        return code, out.getvalue().splitlines()

    def test_findings_are_file_line_message_and_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.yml"
            bad.write_text(workflow(job("      - uses: actions/checkout@v4\n")), encoding="utf-8")
            code, lines = self.run_main(bad)
        self.assertEqual(code, 1)
        self.assertEqual(lines, [f"{bad.as_posix()}:12: uses `actions/checkout@v4` is not pinned to a 40-hex commit SHA"])

    def test_clean_file_exits_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.yml"
            good.write_text(CLEAN, encoding="utf-8")
            code, lines = self.run_main(good)
        self.assertEqual((code, lines), (0, ["lint: ok (1 files)"]))

    def test_missing_file_is_a_finding(self):
        code, lines = self.run_main(Path(tempfile.gettempdir()) / "no-such-workflow.yml")
        self.assertEqual(code, 1)
        self.assertIn("cannot read", lines[0])

    def test_all_files_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            one, two = Path(tmp) / "one.yml", Path(tmp) / "two.yml"
            one.write_text("on: push\n", encoding="utf-8")
            two.write_text("on: push\n", encoding="utf-8")
            code, lines = self.run_main(one, two)
        self.assertEqual((code, len(lines)), (1, 2))


if __name__ == "__main__":
    unittest.main()
