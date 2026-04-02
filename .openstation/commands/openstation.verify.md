---
name: openstation.verify
description: Verify a task in review status. $ARGUMENTS = task name. Use when user says "verify task", "check task", "review task", or wants to verify work before marking done.
---

# Verify Task

Verify a task in `review` status by checking each item in its
`## Verification` section against the actual implementation.

## Input

`$ARGUMENTS` — the task name (ID-prefixed or slug).

Example: `0042-add-login-page` or `add-login-page`

## Procedure

1. Parse the task name from `$ARGUMENTS`.
2. Resolve the task file per `docs/task.spec.md` § Task Resolution.
3. Read the full task file. Verify `status: review` — refuse
   with an error if the task is not in review.
4. Extract the `## Verification` section. Parse each checklist
   item (`- [ ] ...`) into a list of verification criteria.
5. For **each** verification item, gather evidence:
   - Read referenced files, code, tests, or artifacts mentioned
     in the task requirements or verification items.
   - Use Glob/Grep to locate relevant files if paths are not
     explicit (e.g., search for filenames mentioned in criteria).
   - Check that artifacts listed in the `artifacts` frontmatter
     field actually exist.
   - Inspect actual file contents — do not rely on self-reported
     claims in the task body.
6. Determine pass/fail for each item based on the evidence
   gathered. An item passes only if concrete evidence confirms it.
7. **Update the task file** — for each passing item, change
   `- [ ]` to `- [x]` in the `## Verification` section. Leave
   failing items unchecked.
8. Present a **verification report** to the user:

   ```
   ## Verification Report: <task-name>

   | # | Criterion | Result | Evidence |
   |---|-----------|--------|----------|
   | 1 | <item>    | ✅ PASS | <brief evidence> |
   | 2 | <item>    | ❌ FAIL | <what's missing or wrong> |
   ```

9. **Persist the report in the task file.** Write (or replace) a
   `## Verification Report` section immediately after the
   `## Verification` section in the task file. Use the following
   format:

   ```markdown
   ## Verification Report

   *Verified: YYYY-MM-DD*

   | # | Criterion | Result | Evidence |
   |---|-----------|--------|----------|
   | 1 | <item>    | PASS   | <brief evidence> |
   | 2 | <item>    | FAIL   | <what's missing> |

   ### Summary

   <N> passed, <M> failed. <outcome sentence>.

   ### What Needs Fixing

   <only present when items fail — bulleted list of what to fix>
   ```

   If a `## Verification Report` section already exists, **replace
   it entirely** — there should always be exactly one current
   report.

10. If **all items pass**:
    - Transition the task to `verified`:

      ```bash
      openstation status <task-name> verified
      ```

      **Manual fallback** — if the CLI is unavailable, edit
      `status: review` → `status: verified` directly in the task
      frontmatter.
    - Confirm to the user that all verification criteria passed and
      the task is now `verified`.
    - Tell the user they can run `/openstation.done <task-name>`
      when ready to accept and complete the task.

11. If **any items fail**:
    - Report which items failed and why.
    - Transition the task to `in-progress` so the assignee can
      rework the failing items:

      ```bash
      openstation status <task-name> in-progress
      ```

      **Manual fallback** — if the CLI is unavailable, edit
      `status: review` → `status: in-progress` directly in the
      task frontmatter.
    - Suggest what needs to be fixed before re-verification.
