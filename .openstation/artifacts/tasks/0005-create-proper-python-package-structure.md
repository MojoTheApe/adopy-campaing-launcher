---
kind: task
name: 0005-create-proper-python-package-structure
type: feature
status: ready
assignee: developer
owner: user
parent: "[[0002-refactor-fb-agents-mvp-into]]"
created: 2026-04-02
---

# Create Proper Python Package Structure

## Requirements

1. Add `pyproject.toml` making the project installable via `pip install -e .`.
2. Remove all `sys.path` hacks from entry points and agent modules.
3. Define shared data contracts (dataclasses or TypedDicts) for `ComboRow`, `PlanRow`, `AnalysisResult` in a shared `models` module.
4. Add proper `__init__.py` exports for all packages.
5. Replace hardcoded Windows paths in `config/paths.py` with environment-variable-only configuration (no default paths to a specific user's desktop).
6. All existing functionality must continue to work after restructuring.

## Verification

- [ ] `pip install -e .` succeeds
- [ ] No `sys.path.insert` calls remain in codebase
- [ ] Shared data contracts defined and used by agents
- [ ] `__init__.py` files export public APIs
- [ ] No hardcoded Windows paths in source code
- [ ] Existing CLI entry points still work
