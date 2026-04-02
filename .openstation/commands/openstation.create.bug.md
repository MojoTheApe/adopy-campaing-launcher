---
name: openstation.create.bug
description: File a well-structured bug task from a description. $ARGUMENTS is the bug description. Use when user says "file a bug", "this is broken", "X is stale", or reports a defect.
---

# Create Bug Task

File a well-structured bug task with root-cause context,
scoped requirements, and mechanical verification items.

## Input

`$ARGUMENTS` — the bug description (free text).

## Procedure

1. Take the description from `$ARGUMENTS`.

2. **Investigate the root cause.** Before drafting, gather
   enough context to answer:
   - What is wrong? (observed vs expected behavior)
   - Which file(s) or artifact(s) are affected?
   - Which prior task introduced or missed the issue?
     (check `git log`, task history, or grep for relevant changes)

3. **Draft the bug task.** Present a complete draft in one
   message. Do not create files yet.

   The draft must include:

   - **Root-cause context** — one sentence explaining *why* it's
     wrong and linking to the originating task if known
     (e.g., "0173 removed the dual-marker check but didn't
     update worktrees.md").
   - **Numbered requirements** — each a specific, scoped edit.
     Include file paths and line references when helpful. The
     assignee should be able to execute without interpretation.
   - **Preserve boundaries** — explicitly state what NOT to
     change, so the assignee doesn't over-edit.
   - **Verification items** — each mechanically checkable with
     grep/read. No judgment calls.
     Bad: "doc is accurate".
     Good: "No mention of `agents/` + `install.sh` in worktrees.md".
   - **Agent & owner** — suggest the best agent. Default owner:
     `user`.
   - **Status** — `ready` if the fix is clear, `backlog` if it
     needs research first.

   End with: **"Approve, or tell me what to change."**

4. **Iterate only if needed.** If the user approves, proceed.
   If they request changes, apply and re-present.

5. **Create the task file.** Use the CLI:

   ```bash
   openstation create "<description>" \
     --assignee <from draft> \
     --owner <from draft, default: user> \
     --status <from draft>
   ```

6. **Fill in the body.** Edit the generated file to replace
   the placeholder content with the approved draft:

   ```markdown
   # <Title>

   <Root-cause context sentence, linking to originating task.>

   ## Requirements

   1. <Specific, scoped edit with file path>
   2. ...
   N. <Preserve boundary — what NOT to change>

   ## Verification

   - [ ] <Mechanically checkable item>
   ```

   Set `type: bug` in the frontmatter (replace the default).

7. Confirm the file was created and show the path.
