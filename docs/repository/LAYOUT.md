# File ownership

Paths in this document are relative to the repository root.

| Area | Contents | Owner |
|---|---|---|
| `docs/repository/` | Git, tasks, review, checks, adoption and repository change history | Shared process, with reviewed project exceptions |
| `docs/system/` | What the product does, architecture, requirements, domain constraints and system history | Project |
| `docs/runtime/` | Environment identities, credentials handling, operational authorization, deployment, backup and recovery | Project |
| `.workflow/tools/` | Shared exported Tracker helpers | Pinned shared template |
| Application directories | Workflows, generators, tests and product scripts | Project |

`AGENTS.md` is a short reading map. Keep substantive rules in the appropriate
area. Business rules belong to the system, even when an agent must follow them.
A new Git rule belongs to repository guidance. An instruction to access, publish
or test a live environment belongs to runtime guidance. Link between the areas
instead of copying their rules into each other. Mixed legacy documents must be
split by section, preserving their requirements and dated evidence.

`delivery.json` and `tracker/config.json` are the small machine-readable bridge:
they identify checks and completion policy and may link project procedures. They
are not places for a product specification or full operational runbook.

New projects receive starting outlines in `docs/system/` and `docs/runtime/`.
Those files become project-owned immediately, even before they are customized.
Standard upgrades never replace, merge or delete their contents. Missing required
project documents need explicit repair in a project PR. The recorded template
baseline stays pristine; it is not a backup or authority for project knowledge.

Upgrades of an earlier mixed layout stop without writes for an audited migration.
Move the original material, adjust links, preserve local safeguards and record the
move map. Old paths may remain short forwarding files; do not maintain two copies
of the rules. Existing `scripts/task_tracker.py` and related helper paths are
compatibility entry points only; implementations live in `.workflow/tools/`.

This layout changes no task status, delivery policy or deployment connection.
