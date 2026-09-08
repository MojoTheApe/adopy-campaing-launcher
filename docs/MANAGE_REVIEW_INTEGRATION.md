# Manage integration, 2026-09-08

This combines the country's goals, manager comparison, account status and
campaign builder into Manage. The builder stays collapsed by default and
remembers its expansion state. Existing country URLs continue to work.

## Scope

- Production base: `a91c327055667c35f31d11e4833217b9fadf86a1` (`master`).
- Feature commit: `e85e4ee17e840a0708ac82232429b7530eb0e0e2`.
- Integration branch: `codex/manage-review-fixes`, in a separate worktree.
- Claude authored the feature; Codex reviewed and fixed the integration, then
  self-checked its fixes. No independent review of these fixes is claimed.
- Shared file: `src/web/templates/report.html`. Other feature commits are
  excluded. The existing targeting handlers and theme bootstrap are preserved.

## Findings and corrections

1. **P1 — builder lookups still used removed panels.** `accountOptionsFor` and
   `findAccountRow` searched `view-country-…-open`, which no longer existed.
   Loaded accounts were labelled as needing refresh, and opening the cart
   refreshed the page instead of building. All callers now use the actual
   country's `manage-builder-…` container. Both normal and pending builds
   activate Manage and reveal the builder before showing the resulting slot.
2. **P1 — Multilogin helpers lost their country scope.** Missing old panels
   made manual binding and the profile filter fall back to the whole document.
   They now use the same builder lookup without a document-wide fallback.
   The manual binding helper is tested with synthetic diagnostic controls;
   profile filtering uses the existing token-check UI with mocked responses.
3. **P2 — profile selectors were never populated on Manage.** The old lazy
   loader only listened for clicks on `-accounts`. It now reacts to the active
   view through a navigation event and initializes after DOM readiness. This
   covers country clicks, Manage clicks, legacy hashes and programmatic entry.
4. **P2 — legacy builder links hid the requested builder.** `-open` now
   resolves to Manage and expands/scrolls to its builder. A normal Manage visit
   still honors the default and saved expansion state.
5. **Merge conflict:** the old duplicate theme handler was excluded; the
   current production theme bootstrap remains unchanged. The new builder
   expansion persistence handler was retained.

## Verification

- Five Node scenarios execute actual template handlers against panel IDs and
  account rows extracted from a rendered two-country fixture. Four failed on
  the pre-fix rendered page; all five pass after correction.
- Local Python 3.12: **676 passed, 1 Windows-only skip**, with Node enabled.
  Application smoke check, tracked secret scan and diff whitespace check pass.
- Full dashboard JavaScript and DOM are exercised in isolated headless Chrome
  with two synthetic countries. Every network response is mocked locally;
  external requests are blocked. Tests cover legacy navigation, account
  selection, cart materialization, country-scoped binding, profile population,
  saved expansion and pending-build recovery. The browser checks are separate
  from the Node tests; they do not use a real advertising or Multilogin session.
- The PR records the final head, Windows runtime/installer CI and merge status.
  Old successful CI is not reused as verification of this new candidate.

## Remaining release requirements

The live Windows/Multilogin pilot was previously deferred by the owner and is
still unperformed. No `acl-release-*` tag or machine rollout is part of this
task. If GitHub billing prevents CI from starting, that limitation must be
reported explicitly rather than marked as successful testing.
