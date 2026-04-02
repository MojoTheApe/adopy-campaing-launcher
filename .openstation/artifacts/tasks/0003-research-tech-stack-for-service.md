---
kind: task
name: 0003-research-tech-stack-for-service
type: feature
status: done
completed: 2026-04-02
assignee: researcher
owner: user
parent: "[[0002-refactor-fb-agents-mvp-into]]"
artifacts:
  - "[[artifacts/research/tech-stack-for-service]]"
created: 2026-04-02
started: 2026-04-02
---

# Research Tech Stack For Service Transformation

## Context

End users are **non-technical** (media buyers, campaign managers). The UI must be simple, intuitive, and require zero technical knowledge to operate. Prioritize simplicity and ease of use over flexibility and power-user features in all recommendations.

## Requirements

1. Evaluate **web framework** options for Python (FastAPI, Django, Flask) considering: API-first design, async support, OpenAPI auto-generation, ecosystem maturity.
2. Evaluate **database** options (PostgreSQL, SQLite, etc.) for per-customer single-tenant deployment storing campaign stats, combos, KB data, and generated files.
3. Evaluate **AI agent framework** options (LangChain, CrewAI, custom, etc.) for exposing the analysis/upload pipeline as a conversational agent. The AI agent may be the primary interface for non-technical users — evaluate with that in mind.
4. Evaluate **containerization** approach (Docker, Docker Compose) and what's needed for provider-agnostic deployment.
5. Evaluate **web app** approach (server-rendered vs SPA, framework choice if SPA). Key constraint: **target users are non-technical** — prioritize simple, clean UIs with minimal learning curve over feature-rich dashboards.
6. Produce a recommendation document with trade-offs for each choice, optimized for simplicity and single-tenant deployment.

## Verification

- [ ] Web framework comparison with recommendation
- [ ] Database comparison with recommendation
- [ ] AI agent framework comparison with recommendation
- [ ] Containerization approach documented
- [ ] Web app approach documented (with non-technical user focus)
- [ ] All recommendations justified with trade-offs
- [ ] Recommendations prioritize simplicity for non-technical end users

## Findings

Produced a comprehensive tech stack recommendation document at [[artifacts/research/tech-stack-for-service]]. Key conclusions:

| Component | Recommendation | Confidence |
|-----------|---------------|------------|
| Web Framework | **FastAPI** — API-first, native async, auto OpenAPI, Pydantic validation | High |
| Database | **SQLite (v1)** with SQLAlchemy for PostgreSQL migration path — zero-ops for single-tenant | High |
| AI Agent | **Custom wrapper** over Claude/OpenAI SDK — simple tool-use pattern, avoids LangChain overhead | High |
| Containerization | **Docker + Compose**, single container for v1 — simplest per-customer deployment | High |
| Web App | **Jinja2 + HTMX + Tailwind CSS** — no JS build chain, Python-only stack, clean UX for non-technical users | High |

Design principle: **Python-only stack** — no JavaScript framework, no separate frontend build, no heavy agent framework. Every choice minimizes moving parts while preserving escape hatches for future upgrades.

## Downstream

- The architecture spec task (0004) should use these recommendations as input constraints.
- SQLAlchemy model design should use async patterns from the start (`aiosqlite` for SQLite, `asyncpg` for future PostgreSQL).
- HTMX + SSE pattern should be validated with a prototype for the AI chat streaming interface before committing to the approach.

## Progress

- **2026-04-02** — Researched and evaluated 5 technology categories against project constraints (single-tenant, non-technical users, existing Python codebase). Produced recommendation document with comparison tables, trade-offs, and migration paths. All 7 verification items addressed.
