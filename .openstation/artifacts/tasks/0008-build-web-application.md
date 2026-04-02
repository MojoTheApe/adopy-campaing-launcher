---
kind: task
name: 0008-build-web-application
type: feature
status: backlog
assignee: developer
owner: user
parent: "[[0002-refactor-fb-agents-mvp-into]]"
created: 2026-04-02
---

# Build Web Application

## Requirements

1. Implement web UI based on service architecture spec (0004-design-service-architecture-spec).
2. Dashboard: overview of campaign stats, recent analysis runs, combo highlights.
3. Analysis view: trigger analysis, browse combos by country, view reports (CM breakdown, offer comparison).
4. Upload view: select combos or submit plan, generate upload file, download.
5. Knowledge Base view: browse/edit targets, offers, creatives, landing pages.
6. Consumes the REST API (0007-build-rest-api-layer) — no direct DB access.
7. Responsive design suitable for desktop use.

## Verification

- [ ] Dashboard shows campaign overview
- [ ] Analysis can be triggered and results browsed
- [ ] Upload files can be generated and downloaded
- [ ] KB data can be viewed and edited
- [ ] All data flows through REST API
- [ ] UI works on standard desktop browsers
