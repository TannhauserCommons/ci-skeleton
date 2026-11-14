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
        with:
          cache_save: ${{{{ github.ref == 'refs/heads/main' }}}}
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
        text = "on:\n  pull_request_target:\n    branches: [main]\npermissions: {contents: read}\njobs: {}\n"
        self.assertEqual(findings(text), [(2, "pull_request_target is not allowed")])

    def test_list_item_and_inline_forms_are_findings(self):
        for on in ("on: [push, pull_request_target]", "on:\n  - pull_request_target", "on: pull_request_target"):
            with self.subTest(on=on):
                self.assertEqual(messages(f"{on}\npermissions: {{contents: read}}\n"), ["pull_request_target is not allowed"])

    def test_comment_is_not_a_finding(self):
        text = "on:\n  pull_request: # never pull_request_target here\n# pull_request_target\npermissions: {contents: read}\n"
        self.assertEqual(findings(text), [])

    def test_in_quotes_with_hash_before_is_still_a_finding(self):
        text = "on: pull_request\npermissions: {contents: read}\nenv:\n  X: 'a # pull_request_target'\n"
        self.assertEqual(messages(text), ["pull_request_target is not allowed"])


class PermissionsTest(unittest.TestCase):
    def test_missing_top_level_permissions_is_a_finding(self):
        text = "on: push\njobs: {}\n"
        self.assertEqual(findings(text), [(1, "no top-level permissions:")])

    def test_job_level_permissions_do_not_count(self):
        text = job("      - uses: ./x\n").replace("    steps:", "    permissions:\n      contents: read\n    steps:")
        self.assertEqual(messages("on: push\njobs:\n" + text), ["no top-level permissions:"])

    def test_contents_read_is_fine_in_block_and_inline_form(self):
        for permissions in (
            "permissions:\n  contents: read\n",
            "permissions: # least\n  contents: read # only\n\njobs: {}\n",
            "permissions: {contents: read}\n",
            "permissions: { contents: read }\n",
        ):
            with self.subTest(permissions=permissions):
                self.assertEqual(findings("on: push\n" + permissions), [])

    def test_anything_but_contents_read_is_a_finding_on_the_permissions_line(self):
        for permissions in (
            "permissions: write-all\n",
            "permissions: read-all\n",
            "permissions: {}\n",
            "permissions:\n  contents: write\n",
            "permissions:\n  contents: read\n  issues: write\n",
            "permissions:\n  issues: read\n",
            "permissions: {contents: read, pull-requests: write}\n",
            "permissions: {contents: write}\n",
            "permissions:\n",
        ):
            with self.subTest(permissions=permissions):
                self.assertEqual(
                    findings("on: push\n" + permissions),
                    [(2, "top-level permissions must be exactly `contents: read`")],
                )

    def test_job_level_permissions_are_not_held_to_it(self):
        text = job("      - uses: ./x\n").replace("    steps:", "    permissions:\n      issues: write\n    steps:")
        self.assertEqual(findings(workflow(text)), [])

    def test_commented_permissions_does_not_count(self):
        self.assertEqual(messages("on: push\n# permissions:\n"), ["no top-level permissions:"])


class MiseActionTest(unittest.TestCase):
    MESSAGE = (
        "jdx/mise-action must set cache_save to false or to a comparison of github.ref "
        "with the release branch (only the release branch saves caches)"
    )
    MAIN = "${{ github.ref == 'refs/heads/main' }}"

    def steps(self, body, ref=f"jdx/mise-action@{SHA}"):
        return workflow(job(f"      - uses: {ref}\n{body}"))

    def with_value(self, value):
        return self.steps(f"        with:\n          version: 1\n          cache_save: {value}\n")

    def test_no_with_is_a_finding_on_the_uses_line(self):
        self.assertEqual(findings(self.steps("")), [(12, self.MESSAGE)])

    def test_with_without_cache_save_is_a_finding(self):
        text = self.steps("        with:\n          version: 2026.10.23\n")
        self.assertEqual(findings(text), [(12, self.MESSAGE)])

    def test_false_and_the_main_expression_pass_quoted_or_not(self):
        for value in ("false", "'false'", '"false"', f"{self.MAIN}", f'"{self.MAIN}"', f"'{self.MAIN}'", "false # why"):
            with self.subTest(value=value):
                self.assertEqual(findings(self.with_value(value)), [])

    def test_release_branch_comparisons_pass(self):
        for expression in (
            "github.ref == 'refs/heads/next'",
            "github.ref == 'refs/heads/release/1.x_a-b'",
            "github.ref == 'refs/heads/main' || github.ref == 'refs/heads/next'",
            "github.ref == 'refs/heads/a' || github.ref == 'refs/heads/b' || github.ref == 'refs/heads/c'",
            "github.ref=='refs/heads/main'",
            "  github.ref  ==  'refs/heads/main'  ",
            "github.ref == 'refs/heads/main'||github.ref == 'refs/heads/next'",
        ):
            with self.subTest(expression=expression):
                self.assertEqual(findings(self.with_value("${{" + expression + "}}")), [])
                self.assertEqual(findings(self.with_value(f'"${{{{ {expression} }}}}"')), [])

    def test_any_other_value_is_a_finding(self):
        for value in (
            "true",
            "'true'",
            "${{ github.event_name == 'push' }}",
            "${{ github.ref_name == 'main' }}",
            "${{ github.ref != 'refs/heads/main' }}",
            "${{ github.ref == 'refs/heads/main' && github.event_name == 'push' }}",
            "${{ github.ref == 'refs/heads/main' && github.ref == 'refs/heads/next' }}",
            "${{ github.ref == 'refs/heads/main' || github.event_name == 'push' }}",
            "${{ github.ref == 'refs/heads/main' || }}",
            "${{ startsWith(github.ref, 'refs/heads/') }}",
            "${{ github.ref }}",
            "${{ github.ref == 'refs/tags/v1' }}",
            "${{ github.ref == 'refs/pull/1/merge' }}",
            "${{ github.ref == 'refs/heads/' }}",
            "${{ github.ref == 'refs/heads/a b' }}",
            "${{ github.ref == 'refs/heads/main' }} && true",
            "${{ true }}",
            "refs/heads/main",
            "False",
            "",
            "0",
        ):
            with self.subTest(value=value):
                self.assertEqual(findings(self.with_value(value)), [(12, self.MESSAGE)])

    def test_cache_save_first_in_with_passes(self):
        self.assertEqual(findings(self.steps("        with:\n          cache_save: false\n")), [])

    def test_inline_with(self):
        for flow in (
            "{cache_save: false}",
            "{version: x, cache_save: false}",
            "{ cache_save: false, version: x }",
            f"{{version: x, cache_save: {self.MAIN}}}",
            "{cache_save: ${{ github.ref == 'refs/heads/next' || github.ref == 'refs/heads/main' }}}",
            f"{{cache_save: {self.MAIN}, version: x}}",
            "{version: x, 'cache_save': 'false'}",
        ):
            with self.subTest(flow=flow):
                self.assertEqual(findings(self.steps(f"        with: {flow}\n")), [])
        for flow in ("{version: x, cache_save: true}", "{version: x}", "{}", "{cache_save: ${{ github.ref }}}"):
            with self.subTest(flow=flow):
                self.assertEqual(findings(self.steps(f"        with: {flow}\n")), [(12, self.MESSAGE)])

    def test_cache_save_in_a_sibling_env_block_does_not_count(self):
        text = self.steps("        with:\n          version: 1\n        env:\n          cache_save: false\n")
        self.assertEqual(findings(text), [(12, self.MESSAGE)])

    def test_sibling_env_at_the_same_indent_as_with_children_does_not_count(self):
        # The reviewer's case: env: nested so its child sits where with's children sit.
        text = self.steps("        with:\n          version: 1\n          env:\n            x: 1\n        env:\n          cache_save: false\n")
        self.assertEqual(findings(text), [(12, self.MESSAGE)])

    def test_env_before_with_does_not_leak_into_it(self):
        text = self.steps("        env:\n          cache_save: false\n        with:\n          version: 1\n")
        self.assertEqual(findings(text), [(12, self.MESSAGE)])

    def test_with_after_env_still_reads_its_own_cache_save(self):
        text = self.steps("        env:\n          cache_save: true\n        with:\n          cache_save: false\n")
        self.assertEqual(findings(text), [])

    def test_with_before_and_after_uses(self):
        for body in (
            f"      - with:\n          cache_save: false\n        uses: jdx/mise-action@{SHA}\n",
            f"      - uses: jdx/mise-action@{SHA}\n        with:\n          cache_save: false\n",
        ):
            with self.subTest(body=body):
                self.assertEqual(findings(workflow(job(body))), [])
        bad = workflow(job(f"      - with:\n          cache_save: true\n        uses: jdx/mise-action@{SHA}\n"))
        self.assertEqual(findings(bad), [(14, self.MESSAGE)])

    def test_cache_save_on_the_next_step_does_not_count(self):
        text = self.steps("        with:\n          version: 1\n") + (
            f"      - uses: actions/checkout@{SHA}\n        with:\n          cache_save: false\n"
        )
        self.assertEqual(findings(text), [(12, self.MESSAGE)])

    def test_other_actions_need_no_cache_save(self):
        text = workflow(job(f"      - uses: actions/cache@{SHA}\n"))
        self.assertEqual(findings(text), [])

    def test_unpinned_mise_action_gets_both_findings(self):
        text = self.steps("        with:\n          cache_save: false\n", ref="jdx/mise-action@v3")
        self.assertEqual(len(findings(text)), 1)
        self.assertEqual(len(findings(self.steps("", ref="jdx/mise-action@v3"))), 2)


class RunExpressionTest(unittest.TestCase):
    def run_line(self, command):
        return workflow(job(f"      - name: mise run x\n        run: {command}\n"))

    def reads(self, command):
        return messages(self.run_line(command))

    def test_trusted_contexts_are_fine(self):
        for expression in (
            "inputs.dry_run",
            "inputs.all && '--all' || ''",
            "steps.base.outputs.base",
            "steps.my-step.outputs.sha-x",
            "matrix.os",
            "github.sha",
            "github.run_id",
            "github.event.before",
            "github.event.pull_request.base.sha",
            "github.event.workflow_run.id",
            "github.event.pull_request.base.sha || github.event.before",
            "!inputs.dry_run && (matrix.os == 'linux' || matrix.os != 'mac') && true",
            "inputs.x == null || false",
        ):
            with self.subTest(expression=expression):
                self.assertEqual(self.reads(f"mise run x ${{{{ {expression} }}}}"), [])

    def test_untrusted_contexts_are_findings_naming_the_context(self):
        for context in (
            "github.head_ref",
            "github.event.pull_request.title",
            "github.ref_name",
            "github.actor",
            "env.X",
            "secrets.TOKEN",
            "needs.build.outputs.y",
            "github.event.pull_request.base.ref",
            "inputs.x.y",
            "matrix",
        ):
            with self.subTest(context=context):
                self.assertEqual(
                    self.reads(f"mise run x ${{{{ {context} }}}}"),
                    [f"run: reads ${{{{ {context} }}}}; pass it through env instead"],
                )

    def test_finding_is_on_the_run_line(self):
        self.assertEqual(
            findings(self.run_line("mise run x ${{ github.head_ref }}")),
            [(13, "run: reads ${{ github.head_ref }}; pass it through env instead")],
        )

    def test_function_calls_are_findings(self):
        self.assertEqual(
            self.reads("mise run x ${{ contains(inputs.a, 'b') }}"),
            ["run: reads ${{ contains() }}; pass it through env instead"],
        )
        self.assertEqual(len(self.reads("mise run x ${{ format('{0}', inputs.a) }}")), 1)

    def test_one_finding_per_untrusted_context_even_when_mixed(self):
        text = "mise run x ${{ github.head_ref }} ${{ inputs.ok }} ${{ github.ref_name || github.head_ref }}"
        self.assertEqual(
            self.reads(text),
            [
                "run: reads ${{ github.head_ref }}; pass it through env instead",
                "run: reads ${{ github.ref_name }}; pass it through env instead",
            ],
        )

    def test_string_literals_are_not_contexts(self):
        self.assertEqual(self.reads("mise run x ${{ inputs.a && 'github.head_ref' || 'it''s' }}"), [])

    def test_block_scalar_run_is_checked_too(self):
        text = workflow(job("      - name: mise run x\n        run: |\n          mise run x ${{ github.head_ref }}\n"))
        self.assertEqual(messages(text), ["run: reads ${{ github.head_ref }}; pass it through env instead"])

    def test_non_mise_run_with_untrusted_expression_gets_both_findings(self):
        self.assertEqual(
            self.reads("echo ${{ github.head_ref }}"),
            ["run is not a single `mise run ...` line", "run: reads ${{ github.head_ref }}; pass it through env instead"],
        )

    def test_with_env_and_if_are_not_checked(self):
        steps = (
            "      - name: mise run x\n        if: github.head_ref == 'x'\n"
            "        run: mise run x\n        env:\n          REF: ${{ github.head_ref }}\n"
            f"      - uses: org/a@{SHA}\n        with:\n          ref: ${{{{ github.head_ref }}}}\n"
        )
        self.assertEqual(findings(workflow(job(steps))), [])

    def test_chaining_inside_an_expression_still_is_not_chaining(self):
        self.assertEqual(self.reads("mise run x ${{ inputs.a && 'a' || 'b' }}"), [])
        self.assertEqual(self.reads("mise run x && echo ${{ inputs.a }}"), ["run is not a single `mise run ...` line"])


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
