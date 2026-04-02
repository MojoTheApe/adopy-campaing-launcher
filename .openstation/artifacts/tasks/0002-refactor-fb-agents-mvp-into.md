---
kind: task
name: 0002-refactor-fb-agents-mvp-into
type: feature
status: in-progress
assignee: architect
owner: user
created: 2026-04-02
subtasks:
  - "[[0003-research-tech-stack-for-service]]"
  - "[[0004-design-service-architecture-spec]]"
  - "[[0005-create-proper-python-package-structure]]"
  - "[[0006-build-database-layer-replacing-excel]]"
  - "[[0007-build-rest-api-layer]]"
  - "[[0008-build-web-application]]"
  - "[[0009-build-ai-agent-interface]]"
  - "[[0010-containerize-for-provider-agnostic-deployment]]"
---

# Refactor Fb_Agents Mvp Into Customer-Deployable Service

## Requirements

1. Transform the local-only MVP (Excel-based, single-user, script entry points) into a service deployable per-customer.
2. Three interfaces: REST API, web app, and AI agent.
3. Replace Excel input/output with a persistent data layer (database).
4. Proper Python package structure (no `sys.path` hacks, installable).
5. Provider-agnostic deployment (containerized, runs on any cloud).
6. In scope: Analysis (Agent 1), Upload generation (Agent 8), Knowledge Base, Analysis→Upload pipeline, planned agents (6, 7, 9).
7. Out of scope: Jira agent (Agent 5), auth, billing.

## Verification

- [ ] All subtasks completed and verified
- [ ] Service runs in a container with API, web, and AI agent interfaces
- [ ] No Excel dependency for data input/output
- [ ] Deployable on any provider without code changes

## Subtasks

| # | Subtask | Agent | Status | Blocked by |
|---|---------|-------|--------|------------|
| 1 | Research tech stack | researcher | ready | — |
| 2 | Design service architecture spec | architect | backlog | #1 |
| 3 | Create proper Python package structure | developer | backlog | #2 |
| 4 | Build database layer replacing Excel I/O | developer | backlog | #2 |
| 5 | Build REST API layer | developer | backlog | #2, #3, #4 |
| 6 | Build web application | developer | backlog | #5 |
| 7 | Build AI agent interface | developer | backlog | #5 |
| 8 | Containerize for provider-agnostic deployment | developer | backlog | #3 |
