# Dashboard integration, 2026-09-08

The header, light/dark theme switch, and zero-deposit account drill are
integrated into production while retaining the shared targeting library and
safe export workflow already in `master`.

## Scope and ownership

- Production base: `8a1f1debf4dac6ca05dfd0abe22bbe949bf6fd53`.
- Reviewed feature commits: `22870e5d0dd86c647c74944a65f29220a66d7c0c`,
  `9edf491d6402fba6acf8801f4b9936ff580a20d7`,
  `92bb0b5ba72607fac9e86decfde11cad27ac5069`,
  `d487196d2ba4acf4a65a259831cf335bd8c370d2`.
- Integration branch: `codex/dashboard-review-fixes`.
- Claude authored the features; Codex reviewed, fixed, and self-checked the
  integration. No independent review of Codex's fixes is claimed.
- The shared file is `src/web/templates/report.html`; integration takes place
  in a separate worktree. Later feature commits are outside this candidate.

## Findings resolved

1. **P1: changing the period could change account identity.** The feature
   resolved workbook aliases after filtering dates. An older exact match or
   a second account with the same three-character tail could disappear from
   that period, allowing another account's spend and ads to be shown. Both
   the map and `/details` now resolve against the full workbook within the
   requested country, then filter the selected dates. Ambiguous tails remain
   unmatched. Unused duplicate alias indexing was removed.
2. **P2: opening the page pinned the system theme as a manual choice.** The
   startup handler wrote localStorage unconditionally and had no media-change
   listener. Only a user click now saves an explicit preference. Otherwise
   the UI follows system changes, including across reloads. A storage failure
   does not prevent system-theme detection or use of the switch. The initial
   theme is applied in the head before paint and the switch exposes its state.
3. **P2: several dark-theme controls retained white foregrounds on pale
   backgrounds.** Measured text contrast on affected controls was as low as
   1.72:1. Foregrounds now follow accent, success, and error fills, including
   hover states, dynamic confirmations, and builder controls. Success fills
   also meet the text-contrast threshold in the light theme. The Facebook
   preview's fixed avatar colors are retained instead of following the app
   accent. The calendar's primary hover state no longer makes its label the
   same color as its background.
4. **Merge conflict:** the feature branch contained the older targeting UI.
   The current production implementation was retained, including local/shared
   status, retry, explicit export confirmation, pending-state guards, and
   duplicate warnings. Only its error colors were adapted to the new palette.

## Acceptance and verification

- Account drills show ad sets with zero deposits, sorted by deposits then
  spend. CM comparison still includes only ad sets with deposits.
- A linked account resolves to a unique workbook alias; its map, ad-set drill,
  and nested ad drill agree on country and period. Exact older aliases and
  ambiguous tails cannot borrow another account's data.
- Theme behavior is executed in Node using the actual template scripts:
  automatic preference, system changes, explicit selection and reload,
  blocked reads, and failed writes. Five new regression cases failed before
  the corrections; all nine added behavioral cases pass afterwards.
- Local Python 3.12 suite: **665 passed, 1 Windows-only skip**. Node checks are
  enabled. Secret scan and application smoke check also pass.
- Isolated headless Chrome verification uses synthetic rendered HTML, real
  template CSS and theme scripts, with external requests blocked. It checks
  light/dark control contrast, hover states, unchanged preview colors, standards
  mode, and header overflow at 1440, 1024, and 768 pixels. This is not a live
  advertising-account or Multilogin pilot.
- The PR records the final candidate SHA and Windows CI results. Required
  runtime CI covers Python 3.11 and 3.13; installer CI covers build, installation,
  launch, repair, and uninstall.

## Deferred checks and release

The owner previously deferred the live Windows/Multilogin manual pilot until
after merge. It remains unperformed. This task authorizes code merge, not
release promotion: no `acl-release-*` tag or machine rollout is created.
