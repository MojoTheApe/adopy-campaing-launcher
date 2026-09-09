# Portable GitHub task tracker

An English, read-only workspace for GitHub Issues. Python 3.11+ standard library, an authenticated `gh` CLI, and any HTTPS host are sufficient. There is no frontend build, database, AI API or required hosting vendor.

## Setup

1. Configure `tracker/config.json`: project name, repository, base branch, module catalog and release names. Keep the six workflow IDs stable. The final label may be In main, In master or Merged.
2. Read [AGENT_WORKFLOW.md](AGENT_WORKFLOW.md) and link to it from the repository's AGENTS.md and CLAUDE.md.
3. Run `python -m tracker init` to create labels and the configured empty milestone. This never assigns existing tasks to a release.
4. Run `python -m tracker sync --output /path/to/private/public`. Serve that directory behind authentication and HTTPS. HTML, data.json and status.json must all use the same protection. Only a content-free health check may be public.
5. Repeat sync every minute, with one sync process per output directory. Credentials stay in `gh` on the host. `--gh-command-json` accepts a command array for a container or another host-specific adapter; never embed tokens in arguments or config.

## Workspace

All is the first-use view and shows every nonarchived task, including completed tasks. A single status strip contains All, Idea, Backlog, To do, In progress, Review, Master and Archive, each with a project-wide count. Master is the compact navigation label for In master; other configured completion names are preserved. Clicking a status clears search and extra filters so its result count matches the displayed number. There are no Work/Plan views or Workspace section in the sidebar.

The sidebar contains modules only. Their counters describe the selected status or release scope before search and extra filters; clicking a module refines that slice, and All modules removes only that constraint. On narrow screens the Navigation button opens the same module list.
List and Board show the same results. List has seven columns: Task, Status, Module, Priority, Release, Type and Focus. Focus displays any applicable Design, Required and Blocked properties. When the available workspace narrows (including a wider detail dock), all properties reflow into labeled rows below the title. Board cards retain the same metadata. Missing Type is shown as Not classified, not an assignee. Clicking any noninteractive part of a task row/card opens details; ID buttons and links keep their own actions. The title is also a keyboard-accessible button. Details dock on wide screens and overlay on smaller screens. Drag the left edge to resize (360–960 px, constrained to the viewport); the width is saved per repository. The separator also supports arrow keys, Home/End and double-click to reset. Phones use full-width details. Clicking outside, Escape or Close dismisses the panel; a drag that started inside does not dismiss it. The board groups by stage; its wide version scrolls horizontally within the task area, while narrow screens use an explicit Focus column selector. Cards are read-only; there is no drag-and-drop status change.

Search is a short field with one rounded focus border and matches ID, GitHub Issue number, title and description. The filter icon (with an active-condition count and accessible label) expands four compact, aligned rows: Priority, Type, Release and Focus. They sit directly on the page background without a framed panel. Status is selected in the top navigation only. Not classified appears in Type choices only when such tasks exist or that filter is already active. Closing it retains selected conditions. Priority/Type/Release selections within one field combine with OR; different fields and selected Focus properties combine with AND. Applied conditions remain visible and removable. Blocked includes explicit blockers and unfinished dependencies. Use the release selector inside Filters when viewing release scope.

All excludes Archive. Backlog means exactly that stage. The compact release widget sits beside the status strip and shows the completed percentage plus completed / total **required leaf tasks**. It opens Required tasks, including both completed and remaining members. Remaining, Blockers, Completed and Entire milestone are available within release scope. Archived required leaf tasks remain unresolved; initiative parents do not count twice. A nonempty incomplete scope never rounds up to 100%. High priority does not imply release membership. Empty scope displays Scope not defined, and stale percentages are explicitly marked Saved snapshot. Scope ready concerns task completion, not publication of a release.
The entire ID and its small copy icon are one button that copies exactly the displayed ID, or `#NUMBER` when no alias exists. Clipboard requires HTTPS and a user action. Success is shown only after the write succeeds; if denied, the ID remains available for manual copying. Descriptions, checklist, dependencies and Git references appear in task details.

Light / Dark / System transitions surfaces over 360 ms; text changes synchronously with a stable font family and metrics, without animating inherited text color. Task details enter over 280 ms; reduced-motion preferences disable these animations. The logo gradient moves distinct cobalt, teal and violet regions within the stationary mark over a more visible 7-second cycle; reduced motion freezes it. A 1 px border uses the same gradient on hovered or selected status buttons and the release widget, on hovered/focused search, and on the filter button when hovered, keyboard-focused, expanded or holding active conditions. It fades in/out without resizing controls or tinting their contents; collapsed active filters remain marked. Hover is enabled only on devices that support it. Reduced motion keeps a static gradient; keyboard focus remains visible. Rows are compact by default with no density switch. Preferences are namespaced by repository and UI version; view/layout/theme, filter expansion and panel width are remembered locally, while the current search, task selection and scroll are session state. Old tracker v2 preferences are migrated, discarding unknown filters. Retired saved Status filters are discarded so they cannot invisibly restrict results. Removed Work/Plan views reset to All; saved density no longer affects layout. Task URLs using `#task-NUMBER` and canonical ID fragments open the same task.

## Reliable updates

A failed API request, malformed pagination or unexpected empty response preserves the last successful board and exposes a safe error in status.json. Only explicit `--allow-empty` clears an existing board. The browser accepts a new data snapshot only when its schema, repository and published digest agree; it retains the last good display on errors.

Unchanged rows are reused. Refresh preserves filters, search, layout, selection, focus and scroll anchor. An open task that leaves the filter remains readable with Outside current filters. Unavailable tasks show the last saved details explicitly. UI-code revisions reload with saved context. No poll clears cards to show a loading screen.

## Reuse in another repository

```sh
python -m tracker export-template --output /tmp/new-project-tracker --repository OWNER/NEW_REPO --name "New Project" --base main
```

The export contains the portable package (including UI assets and local fonts), generic config, canonical agent instructions and GitHub templates. It excludes real snapshots, historical ACL mappings, credentials and deployment paths. Copy it into the new repository, merge instructions with existing rules, adapt modules, initialize labels and configure hosting. Export does not contact GitHub. The docs entry point links to the one canonical workflow rather than creating a second editable copy.

## Serving and assets

Generated HTML contains its CSS, JavaScript and locally bundled Manrope font, so deployment does not require a frontend asset build or external font service. The font's SIL Open Font License is in `web/fonts/OFL.txt`. Monospace IDs use the platform font.

Mount only generated public files read-only in the web server. Set `Cache-Control: private, no-store`, disable directory listings and protect all content endpoints. The CSP needs inline script/style, `font-src data:` and `connect-src 'self'`; it does not need Google Fonts. Keep credentials/config outside the public mount. ACL's existing systemd integration uses `scripts/acl_tracker.py` as a compatible command entry point.

## Migration and rollback

`migration-plan --mapping FILE --output PLAN` reads fresh Issues and produces a before/after label plan. `migrate --plan PLAN` validates the whole batch and applies label changes; `--rollback` reverses it. Keep the plan private. Retries reject unrelated concurrent changes. Issue numbers, titles, descriptions, checklists, states and milestones remain intact.

The Design v2 UI requires no label migration. ACL's earlier one-time mapping remains outside the portable package. Historical Obsidian exports are not inputs to this interface. For UI rollback, restore the previous renderer/assets and regenerate a complete matching HTML/data/status snapshot; retain authentication and the last-good-data guards.
