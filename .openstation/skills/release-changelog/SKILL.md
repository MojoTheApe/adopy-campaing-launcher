---
name: release-changelog
description: Generate a changelog entry for a new Open Station release from conventional commits and domain-specific categories.
user-invocable: false
---

# Release Changelog

Generate a changelog entry for a new Open Station release. Follows
conventional commits, domain-specific categories, and the existing
`CHANGELOG.md` format.

## When to Use

Use this skill when preparing a release — after all work is merged
and before tagging. The skill produces a draft changelog entry for
human review; it never auto-commits or auto-tags.

## Prerequisites

- All release work is merged to the current branch.
- `CHANGELOG.md` exists at the repository root.
- Git tags follow semver with `v` prefix (`v0.4.0`, `v1.0.0`).
- Commits use conventional prefixes (`feat:`, `fix:`, `docs:`,
  `refactor:`, `chore:`, `test:`).

---

## Workflow

### Step 0 — Idempotency Check

Before generating anything, check whether the target version
already has an entry in `CHANGELOG.md`.

```bash
grep -q "^## v<VERSION>" CHANGELOG.md
```

- **If found** — Stop and ask: "CHANGELOG.md already has an entry
  for v\<VERSION\>. Regenerate and replace it, or skip?"
- **If not found** — Proceed.

If the user has not specified a target version, do NOT invent one.
Proceed through Steps 1–4 first, then recommend a version in
Step 5 based on change severity.

### Step 1 — Determine Release Range

Find the latest release tag and scope the commit range.

```bash
# Latest tag
git tag --sort=-v:refname | head -1

# Verify it exists and note its date
git log -1 --format="%ai" <last-tag>
```

The range is `<last-tag>..HEAD`. If no tags exist, use
`--root..HEAD` (first release).

### Step 2 — Collect Commits

Gather commit subjects and file-change stats for the range.

```bash
# Commit hashes and subjects
git log --format="%H %s" <last-tag>..HEAD

# Files changed per commit (for category hints)
git log --format="%H" <last-tag>..HEAD | while read h; do
  echo "=== $h ==="
  git diff-tree --no-commit-id --name-only -r "$h"
done
```

Exclude merge commits and `chore:` commits that are purely
internal (CI config, tooling tweaks with no user-facing impact).

### Step 3 — Parse and Categorize

#### 3a — Parse conventional commit prefix

Extract the prefix from each commit subject:

| Prefix | Meaning |
|--------|---------|
| `feat:` | New feature or enhancement |
| `fix:` | Bug fix |
| `docs:` | Documentation change |
| `refactor:` | Code restructuring |
| `test:` | Test-only change |
| `chore:` | Maintenance (usually omit from changelog) |

#### 3b — Assign domain category

Open Station uses **domain categories**, not commit-type
categories. Map each commit to a domain using file paths as
the primary signal:

| File Path Signal | Category |
|------------------|----------|
| `bin/openstation`, `tests/` | CLI |
| `artifacts/agents/`, `agents/` | Agents |
| `docs/`, `artifacts/specs/` | Specs & Docs |
| `commands/` | Commands |
| `skills/` | Skills |
| `install.sh`, `setup.py`, `pyproject.toml` | Install |
| `fix:` prefix (any domain) | Fix |
| Structural / cross-cutting changes | Architecture |

Rules:
- A commit touching multiple domains goes into the **most
  prominent** domain (by number of files changed), or into
  **Architecture** if it's a cross-cutting structural change.
- `refactor:` commits: include if they change user-facing
  behavior or structure; omit if purely internal.
- `chore:` commits: omit unless they affect the user experience
  (e.g., changing install steps).
- `test:` commits: omit unless they introduce a new testing
  capability worth noting.

### Step 4 — Draft the Entry

Write the changelog entry following the established format:

```markdown
## v<VERSION>

Summary paragraph describing the release theme in 1–3 sentences.
Focus on the biggest user-facing change.

### <Category>

- **Bold feature name** — Description of the change. Reference
  file paths, command names, or spec names when relevant.
- **Another item** — More detail.

### <Category>

- **Item** — Description.
```

Format rules:
- H2 for the version heading (`## v<VERSION>`).
- Summary paragraph immediately after the heading.
- H3 for each category (`### CLI`, `### Commands`, etc.).
- Each entry: `- **Bold name** — Description.` (em dash, not
  hyphen).
- Multi-sentence descriptions are fine.
- Order categories by significance (most impactful first).
- Omit empty categories.

### Step 5 — Version Recommendation

If the user hasn't specified a version, recommend one:

| Condition | Bump |
|-----------|------|
| Breaking changes (removed features, changed interfaces) | **major** |
| New features, significant enhancements | **minor** |
| Bug fixes, documentation, internal improvements only | **patch** |

Present the recommendation with reasoning; do not decide
unilaterally.

### Step 6 — Present for Review

Show the complete draft entry to the user. Flag:
- Any commits you were unsure how to categorize.
- Any commits you excluded and why.
- The recommended version bump (if applicable).

**Wait for explicit approval before writing.**

### Step 7 — Write to CHANGELOG.md

On approval, insert the new entry into `CHANGELOG.md`:
- Place it immediately after the `# Changelog` heading.
- Keep a blank line before and after the new entry.
- Do not modify existing entries.

**Do not commit, tag, or push yet.** The changelog write is a
working-tree change only. Committing and tagging happen in Step 8
after the user approves the full release.

### Step 8 — Release

After writing the changelog, present this release checklist and
**wait for the user to approve before executing**:

```
Release v<VERSION>:
1. Update version in pyproject.toml → "<VERSION>"
2. Update OPENSTATION_VERSION in install.sh → "v<VERSION>"
3. Commit: "chore: release v<VERSION>"
4. Tag: git tag v<VERSION>
5. Push: git push origin main --tags
```

On approval, execute all five steps in sequence. Stop immediately
if any step fails.

## What This Skill Does NOT Do

- **Never scan for changesets, migrations, or API routes.**
  Single-package Python project.
- **Never fetch PR metadata.** Commits are self-descriptive.
- **Never write per-version release files.** Single
  `CHANGELOG.md` only.
