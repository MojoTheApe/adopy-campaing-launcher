---
kind: task
name: 0010-containerize-for-provider-agnostic-deployment
type: feature
status: ready
assignee: developer
owner: user
parent: "[[0002-refactor-fb-agents-mvp-into]]"
created: 2026-04-02
---

# Containerize For Provider-Agnostic Deployment

## Requirements

1. Create `Dockerfile` for the service (API + web app + AI agent).
2. Create `docker-compose.yml` for local development (app + database).
3. Configuration via environment variables only (no hardcoded values).
4. Health check endpoint for container orchestration.
5. Production-ready: non-root user, minimal image size, proper signal handling.
6. Documentation: how to build, configure, and deploy for a new customer.

## Verification

- [ ] `docker build` succeeds
- [ ] `docker-compose up` starts full stack locally
- [ ] All configuration via environment variables
- [ ] Health check endpoint responds
- [ ] Container runs as non-root user
- [ ] Deployment documentation exists
