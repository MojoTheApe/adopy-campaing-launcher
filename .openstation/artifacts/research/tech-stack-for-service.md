---
kind: research
agent: researcher
task: "[[0003-research-tech-stack-for-service]]"
created: 2026-04-02
---

# Tech Stack Research: fb_agents Service Transformation

Research for transforming the fb_agents MVP (Excel-based, script entry points) into a per-customer deployable service with REST API, web UI, and AI agent interface.

**Key constraints**: single-tenant per-customer deployment, non-technical end users (media buyers, campaign managers), existing Python codebase (~9 modules, pandas/openpyxl stack).

---

## 1. Web Framework

### Options Evaluated

| Criterion | FastAPI | Django | Flask |
|-----------|---------|--------|-------|
| **API-first design** | Native — built around OpenAPI/JSON Schema | Possible via DRF but bolt-on | Manual or via extensions |
| **Async support** | Native (ASGI, async/await throughout) | Partial (ASGI views since 4.1, ORM still sync) | No native async (WSGI) |
| **OpenAPI auto-generation** | Built-in from type hints + Pydantic | Via drf-spectacular (third-party) | Via flask-smorest (third-party) |
| **Ecosystem maturity** | Young but large ecosystem, very active | Most mature, batteries-included | Mature, minimal core |
| **Learning curve** | Low for Python devs with type hints | Higher — ORM, admin, settings, apps pattern | Low — minimal abstractions |
| **Pydantic integration** | Native (request/response validation) | Third-party | Third-party |
| **Performance** | High (uvicorn/ASGI, async I/O) | Moderate | Moderate |
| **Background tasks** | Built-in `BackgroundTasks`, easy Celery integration | Celery integration well-documented | Celery integration available |

### Recommendation: **FastAPI**

**Confidence: High**

**Rationale**:
- The project needs API-first design (3 interfaces: REST, web, AI agent all consume the same API).
- Auto-generated OpenAPI docs reduce documentation burden and provide interactive Swagger UI.
- Native async matters for the AI agent interface (streaming LLM responses, concurrent API calls to Facebook).
- Pydantic models replace the current loose dict/DataFrame passing with validated schemas — critical for moving from Excel to structured data.
- The existing codebase is pure Python with no framework — FastAPI imposes minimal migration overhead.
- Django's batteries (admin, ORM, auth) are overkill — auth/billing are explicitly out of scope.

**Trade-offs**:
- FastAPI lacks Django's built-in admin panel (not needed — users are non-technical and won't use admin).
- Less opinionated about project structure (mitigated by the architecture spec task).
- Smaller ecosystem than Django for full-stack web (mitigated by using a separate frontend).

---

## 2. Database

### Options Evaluated

| Criterion | PostgreSQL | SQLite |
|-----------|-----------|--------|
| **Concurrent access** | Full MVCC, many readers+writers | Single writer, WAL helps but limited |
| **Deployment complexity** | Separate process, needs management | Zero config, single file |
| **Data types** | Rich (JSONB, arrays, full-text search) | Basic (no native JSON querying pre-3.38) |
| **Scalability** | Production-grade, handles large datasets | Good for small-medium, struggles with concurrent writes |
| **Backup/restore** | pg_dump, point-in-time recovery | File copy |
| **Python integration** | asyncpg (async), psycopg2 (sync), SQLAlchemy | aiosqlite (async), sqlite3 (stdlib), SQLAlchemy |
| **Single-tenant fit** | One DB per customer or schema-per-tenant | One file per customer — trivially isolated |

### Recommendation: **SQLite for v1, PostgreSQL migration path**

**Confidence: High**

**Rationale**:
- Single-tenant, per-customer deployment means low concurrency (typically 1-3 users per instance).
- SQLite is zero-ops: no database server to install, configure, or maintain — ideal for "runs anywhere" container deployment.
- Campaign stats data volume is modest (~7,000 rows per Excel import as seen in current usage).
- SQLite simplifies backup (copy the file) and customer data isolation.
- SQLAlchemy as the ORM layer means switching to PostgreSQL later requires only changing the connection string.

**Trade-offs**:
- If concurrent writes become a bottleneck (multiple agents writing simultaneously), PostgreSQL becomes necessary.
- No built-in full-text search as powerful as PostgreSQL's (but not needed for v1 — campaign data is structured).
- Less tooling for monitoring and debugging in production.

**Migration path**: Use SQLAlchemy with async support (via `aiosqlite` for SQLite, `asyncpg` for PostgreSQL). The schema and queries remain identical; only the engine URL changes.

---

## 3. AI Agent Framework

### Options Evaluated

| Criterion | LangChain | CrewAI | Custom (direct API) |
|-----------|-----------|--------|---------------------|
| **Complexity** | High — large abstraction layer, many concepts | Moderate — role-based agents | Low — you control everything |
| **Flexibility** | High but constrained by abstractions | Moderate — opinionated about agent roles | Maximum |
| **Dependencies** | Heavy (~50+ transitive deps) | Moderate (built on LangChain) | Minimal (anthropic/openai SDK only) |
| **Conversational interface** | Good — chat models, memory, chains | Designed for multi-agent, less for chat | Full control over prompts/context |
| **Streaming support** | Yes, but abstraction overhead | Limited | Native |
| **Debugging** | Difficult — deep call stacks, implicit behavior | Moderate | Easy — you see every API call |
| **Lock-in** | High — framework-specific patterns | High — depends on LangChain | None |
| **Maintenance** | Frequent breaking changes (fast-moving API) | Fast-moving, less stable | Stable (SDK follows API versioning) |

### Recommendation: **Custom lightweight wrapper over Claude/OpenAI SDK**

**Confidence: High**

**Rationale**:
- The AI agent's job is well-scoped: accept natural language queries about campaign data, run analysis functions, return results. This is a tool-use pattern, not a complex multi-agent orchestration.
- Non-technical users need reliable, predictable behavior — LangChain's abstraction layers add failure modes and make debugging harder.
- The existing analysis logic (`analyze_fb_stats.py`, combo generation, offer comparison) can be exposed as tool functions that an LLM calls directly.
- A custom wrapper with ~200-300 lines of code gives: system prompt management, tool definitions, conversation memory, and streaming — all that's needed.
- Avoids the dependency bloat and breaking-change churn of LangChain.

**Suggested architecture**:
```
User message → API endpoint → LLM (with tool definitions) → calls analysis functions → returns formatted result
```

Tools to expose:
- `analyze_campaigns(country, top_n)` — run full analysis
- `get_combos(country, combo_type)` — get strong/interesting combos
- `compare_offers(country)` — offer comparison
- `get_cm_report(country)` — campaign manager breakdown
- `upload_plan(combos, account)` — generate upload Excel

**Trade-offs**:
- No pre-built agent memory management (implement a simple conversation history store — ~50 lines).
- No pre-built RAG pipeline if knowledge base search becomes complex (can add later with a vector DB).
- Requires understanding the LLM API directly (but this is simpler than understanding LangChain's abstractions).

---

## 4. Containerization

### Approach: **Docker + Docker Compose**

**Confidence: High**

This is straightforward with minimal trade-offs.

### Recommended Setup

```
docker-compose.yml
├── app (FastAPI + web frontend)
│   ├── Dockerfile
│   ├── Python 3.12-slim base
│   ├── uvicorn as ASGI server
│   └── SQLite DB mounted as volume
└── (no other services needed for v1)
```

**Single container for v1**: FastAPI serves both the API and static frontend files. SQLite lives on a mounted volume for persistence.

**Why single container**:
- Single-tenant = single user workload. No need for separate web server, worker, or database containers.
- Simpler deployment for per-customer instances.
- Docker Compose still used for volume management, env vars, and future service addition.

**Provider-agnostic deployment checklist**:
- No cloud-specific services (no AWS S3, no GCP-specific APIs).
- All config via environment variables (12-factor app).
- Data persistence via Docker volumes (works on any host).
- Health check endpoint (`/health`) for any orchestrator.
- `docker-compose.yml` as the single deployment artifact.

**Future scaling path**:
- Add nginx reverse proxy container when TLS termination is needed.
- Add Redis container if background task queuing is required.
- Switch to PostgreSQL container when concurrent writes demand it.

---

## 5. Web Application Approach

### Options Evaluated

| Criterion | Server-Rendered (Jinja2/HTMX) | SPA — React | SPA — Vue | SPA — Svelte |
|-----------|-------------------------------|-------------|-----------|--------------|
| **Simplicity** | Highest — no build step, no JS framework | Complex — JSX, state management, build tooling | Moderate | Moderate |
| **Non-technical user UX** | Good — fast page loads, simple interactions | Excellent — rich interactivity | Excellent | Excellent |
| **Development speed** | Fast for simple UIs | Slower initially, faster for complex UIs | Moderate | Fast |
| **Python-only team** | Perfect — no JS expertise needed | Requires JS/TS expertise | Requires JS expertise | Requires JS expertise |
| **Real-time updates** | HTMX + SSE works well | Native with WebSocket libs | Native | Native |
| **Mobile responsiveness** | CSS frameworks (Tailwind, Pico) | Component libraries (shadcn) | Component libraries (Vuetify) | Component libraries |
| **AI chat interface** | HTMX + SSE for streaming | Native streaming support | Native | Native |

### Recommendation: **Server-rendered with Jinja2 + HTMX + Tailwind CSS**

**Confidence: High**

**Rationale**:
- **Non-technical users** need simple, clean pages — not complex SPAs. The UI is: view reports, upload files, chat with AI agent. This is a document-oriented workflow, not a real-time dashboard.
- **Zero JavaScript build toolchain** — the entire project stays Python-only. No npm, no webpack, no TypeScript config. One language, one team skill set.
- **HTMX** provides the interactivity needed (form submissions without page reload, streaming AI responses via SSE, dynamic table updates) without SPA complexity.
- **Tailwind CSS via CDN** (or standalone CLI) provides clean, responsive design with no build step.
- FastAPI has first-class Jinja2 support via `fastapi.templating`.
- The UI pages are simple:
  - Dashboard: campaign stats summary (tables, key metrics)
  - Analysis: run analysis, view combos (tables)
  - Upload: generate upload Excel (form + file download)
  - AI Chat: conversational interface (message list + input)
  - Settings: file upload, configuration

**Trade-offs**:
- Less suitable for highly interactive real-time dashboards (not needed here).
- If the UI grows complex (drag-and-drop, complex charts), would need to add JS libraries selectively.
- HTMX is less well-known — but its simplicity means the learning curve is ~1 hour.

**Key HTMX patterns for this project**:
- `hx-post="/api/analyze"` — trigger analysis without page reload
- `hx-ext="sse"` + `sse-connect="/api/chat/stream"` — streaming AI responses
- `hx-swap="innerHTML"` — update report tables in-place
- `hx-indicator` — loading spinners during analysis

---

## 6. Summary of Recommendations

| Component | Choice | Key Reason |
|-----------|--------|------------|
| **Web Framework** | FastAPI | API-first, async, auto OpenAPI, Pydantic validation |
| **Database** | SQLite (v1) → PostgreSQL path | Zero-ops for single-tenant, SQLAlchemy for portability |
| **AI Agent** | Custom wrapper (Claude/OpenAI SDK) | Simple tool-use pattern, no framework overhead |
| **Containerization** | Docker + Compose, single container | Simple per-customer deployment, provider-agnostic |
| **Web App** | Jinja2 + HTMX + Tailwind CSS | No JS build chain, Python-only team, clean UX |

### Design Principles

1. **Simplicity over power** — every choice minimizes moving parts for non-technical users and single-tenant deployment.
2. **Python-only stack** — no JavaScript framework, no separate frontend build. One language across the entire project.
3. **Escape hatches preserved** — SQLAlchemy for DB migration, standard HTTP API for future SPA, Docker Compose for adding services.
4. **Progressive complexity** — start with the simplest option that works (SQLite, single container, HTMX), upgrade only when evidence demands it.

---

## Useful Links

- [FastAPI docs](https://fastapi.tiangolo.com/)
- [HTMX docs](https://htmx.org/docs/)
- [SQLAlchemy async](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
- [Tailwind CSS CDN](https://tailwindcss.com/docs/installation/play-cdn)
- [Anthropic tool use](https://docs.anthropic.com/en/docs/build-with-claude/tool-use)

## Tags

#research #tech-stack #fastapi #sqlite #htmx #docker #ai-agent #architecture
