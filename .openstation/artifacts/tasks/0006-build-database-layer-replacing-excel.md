---
kind: task
name: 0006-build-database-layer-replacing-excel
type: feature
status: backlog
assignee: developer
owner: user
parent: "[[0002-refactor-fb-agents-mvp-into]]"
created: 2026-04-02
---

# Build Database Layer Replacing Excel I/O

## Requirements

1. Implement database schema based on service architecture spec (0004-design-service-architecture-spec).
2. Replace Excel file reading in Agent 1 with database queries for campaign stats.
3. Replace Excel reference data reading in Agent 8 with database queries (accounts, ad copy, countries, interests, creatives).
4. Replace KB JSON/markdown storage with database tables (targets, offers, creatives, landing pages).
5. Provide a data ingestion layer (import from CSV/API) to populate the database.
6. Store generated outputs (upload Excel, reports) as downloadable artifacts in the DB or file storage.
7. Include database migrations support.

## Verification

- [ ] Database schema covers all entities from Excel files
- [ ] Agent 1 reads campaign stats from DB
- [ ] Agent 8 reads reference data from DB
- [ ] KB Agent reads/writes targets and offers from DB
- [ ] Data ingestion endpoint or script works
- [ ] Generated files are stored and retrievable
- [ ] Migrations run cleanly on empty database
