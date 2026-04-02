---
kind: spec
name: service-architecture
agent: architect
task: "[[0004-design-service-architecture-spec]]"
created: 2026-04-02
status: draft
version: 1
tags:
  - architecture
  - service
  - fastapi
  - sqlite
  - htmx
---

# Service Architecture Spec

Architecture for transforming fb_agents from a local Excel-based
MVP into a per-customer deployable service with three interfaces:
REST API, web application, and AI agent.

All technology choices follow the tech stack research
(`artifacts/research/tech-stack-for-service.md`): FastAPI,
SQLite+SQLAlchemy (async), custom AI wrapper, Docker+Compose,
Jinja2+HTMX+Tailwind.

---

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CLIENT LAYER                                  │
│                                                                      │
│  Browser (HTMX)          REST Client          AI Chat (SSE)         │
│  ─ Dashboard             ─ API consumers       ─ Natural language    │
│  ─ Analysis view         ─ Automation scripts   ─ Streaming response │
│  ─ Upload form           ─ External tools       ─ Tool-use results   │
│  ─ KB management                                                     │
└────────┬──────────────────────┬──────────────────┬──────────────────┘
         │ HTTP/HTML            │ HTTP/JSON         │ HTTP/SSE
         ▼                     ▼                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     FastAPI APPLICATION                               │
│                                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐       │
│  │ Web Routes   │  │ API Routes   │  │ AI Agent Endpoint    │       │
│  │ (Jinja2+HTMX)│  │ (/api/v1/*)  │  │ (/api/v1/chat)       │       │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────┘       │
│         │                 │                      │                   │
│         └─────────┬───────┘──────────────────────┘                   │
│                   ▼                                                  │
│         ┌──────────────────┐                                         │
│         │ Service Layer     │                                         │
│         │ (business logic)  │                                         │
│         └────────┬─────────┘                                         │
│                  │                                                   │
│         ┌────────┴─────────┐                                         │
│         ▼                  ▼                                         │
│  ┌─────────────┐   ┌──────────────┐                                  │
│  │ Repository   │   │ Knowledge    │                                  │
│  │ Layer (DB)   │   │ Base Service │                                  │
│  └──────┬──────┘   └──────┬───────┘                                  │
│         │                 │                                          │
│         ▼                 ▼                                          │
│  ┌─────────────┐   ┌──────────────┐                                  │
│  │ SQLite      │   │ SQLite       │                                  │
│  │ (aiosqlite) │   │ (KB tables)  │                                  │
│  └─────────────┘   └──────────────┘                                  │
└─────────────────────────────────────────────────────────────────────┘
```

**Key principle:** All three interfaces (web, API, AI agent) share
the same service layer. No business logic lives in routes.

---

## 2. Data Model

SQLAlchemy async ORM with aiosqlite. Single SQLite database file
per customer instance.

### 2.1 Entity-Relationship Diagram

```
┌──────────────────┐     ┌──────────────────┐
│ campaign_stats    │     │ analysis_run      │
│──────────────────│     │──────────────────│
│ PK id            │     │ PK id            │
│    country       │◄────│    run_id (UUID)  │
│    account_alias │     │    created_at     │
│    adset_name    │     │    stats_source   │
│    ad_title      │     │    asset_filter   │
│    ad_asset      │     │    status         │
│    cm            │     └────────┬─────────┘
│    leads         │              │
│    ftds          │              │
│    spend         │     ┌────────┴─────────┐
│    net           │     │ combo             │
│    date          │     │──────────────────│
│ FK import_id     │     │ PK id            │
└──────────────────┘     │ FK run_id        │
                         │    country       │
┌──────────────────┐     │    offer         │
│ stats_import      │     │    device        │
│──────────────────│     │    interest      │
│ PK id            │     │    age           │
│    filename      │     │    creative      │
│    imported_at   │     │    text_segment  │
│    row_count     │     │    category      │
│    status        │     │    deposits      │
└──────────────────┘     │    leads         │
                         │    spend         │
                         │    net           │
                         │    cpl           │
                         │    cpa           │
                         │    cr            │
                         │    profit        │
                         │    roi           │
                         │    top_ad_title  │
                         │    top_cm        │
                         │    top_account   │
                         │    country_code  │
                         └─────────────────┘

┌──────────────────┐     ┌──────────────────┐
│ upload_plan       │     │ upload_file       │
│──────────────────│     │──────────────────│
│ PK id            │     │ PK id            │
│ FK run_id        │     │ FK plan_id       │
│    account       │     │    filename      │
│    country       │     │    created_at    │
│    asset         │     │    file_path     │
│    os            │     └──────────────────┘
│    creative      │
│    targeting     │
│    age           │
│    creative_num  │
│    date          │
│    copy          │
└──────────────────┘

┌──────────────────┐     ┌──────────────────┐
│ kb_target         │     │ kb_offer          │
│──────────────────│     │──────────────────│
│ PK id            │     │ PK id            │
│    code (unique) │     │    name (unique) │
│    label         │     │    label         │
│    aliases (JSON)│     │    language (JSON)│
│    fb_interest   │     │    top_countries  │
│    audience      │     │       (JSON)     │
│    top_countries │     │    audience      │
│       (JSON)     │     │    usp           │
│    notes         │     │    notes         │
│    updated_at    │     │    updated_at    │
└──────────────────┘     └──────────────────┘

┌──────────────────┐     ┌──────────────────┐
│ kb_creative       │     │ reference_data    │
│──────────────────│     │──────────────────│
│ PK id            │     │ PK id            │
│    name          │     │    category       │
│    description   │     │    (account|      │
│    updated_at    │     │     ad_copy|      │
│                  │     │     country|      │
│                  │     │     interest|     │
│                  │     │     video|image)  │
│                  │     │    key (unique    │
│                  │     │     per category) │
│                  │     │    data (JSON)    │
│                  │     │    updated_at     │
└──────────────────┘     └──────────────────┘

┌──────────────────┐     ┌──────────────────┐
│ analysis_rules    │     │ chat_conversation │
│──────────────────│     │──────────────────│
│ PK id            │     │ PK id            │
│    rule_type     │     │    created_at    │
│    (device|age|  │     │    title         │
│     text_segment|│     │    updated_at    │
│     creative)    │     └────────┬─────────┘
│    config (JSON) │              │
│    updated_at    │     ┌────────┴─────────┐
└──────────────────┘     │ chat_message      │
                         │──────────────────│
                         │ PK id            │
                         │ FK conversation_id│
                         │    role          │
                         │    content       │
                         │    tool_calls    │
                         │       (JSON)     │
                         │    created_at    │
                         └─────────────────┘
```

### 2.2 SQLAlchemy Model Definitions

All models use `sqlalchemy.ext.asyncio` with `aiosqlite`.

```python
# src/fb_agents/db/models.py
from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    JSON, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class StatsImport(Base):
    """Tracks each Excel/CSV file import."""
    __tablename__ = "stats_import"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(500))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(
        String(20), default="completed"
    )  # completed | failed

    stats: Mapped[list["CampaignStat"]] = relationship(
        back_populates="import_record"
    )


class CampaignStat(Base):
    """Raw campaign statistics — one row per ad-level record."""
    __tablename__ = "campaign_stats"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("stats_import.id")
    )
    country: Mapped[str] = mapped_column(String(100))
    account_alias: Mapped[str] = mapped_column(String(200))
    adset_name: Mapped[str] = mapped_column(String(500))
    ad_title: Mapped[str | None] = mapped_column(String(500))
    ad_asset: Mapped[str | None] = mapped_column(String(200))
    cm: Mapped[str | None] = mapped_column(String(100))
    leads: Mapped[int] = mapped_column(Integer, default=0)
    ftds: Mapped[int] = mapped_column(Integer, default=0)
    spend: Mapped[float] = mapped_column(Float, default=0.0)
    net: Mapped[float] = mapped_column(Float, default=0.0)
    date: Mapped[str | None] = mapped_column(String(20))

    import_record: Mapped["StatsImport"] = relationship(
        back_populates="stats"
    )


class AnalysisRun(Base):
    """Each execution of the analysis pipeline."""
    __tablename__ = "analysis_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid4()), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    stats_source: Mapped[str | None] = mapped_column(
        String(500)
    )  # import_id or "all"
    asset_filter: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(
        String(20), default="running"
    )  # running | completed | failed

    combos: Mapped[list["Combo"]] = relationship(
        back_populates="analysis_run"
    )


class Combo(Base):
    """Aggregated combo from an analysis run (Strong/Interesting)."""
    __tablename__ = "combo"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_run.id")
    )
    country: Mapped[str] = mapped_column(String(100))
    country_code: Mapped[str | None] = mapped_column(String(10))
    offer: Mapped[str | None] = mapped_column(String(200))
    device: Mapped[str | None] = mapped_column(String(20))
    interest: Mapped[str | None] = mapped_column(String(200))
    age: Mapped[str | None] = mapped_column(String(10))
    creative: Mapped[str | None] = mapped_column(String(500))
    text_segment: Mapped[str | None] = mapped_column(String(50))
    category: Mapped[str] = mapped_column(
        String(20)
    )  # Strong | Interesting
    deposits: Mapped[int] = mapped_column(Integer, default=0)
    leads: Mapped[int] = mapped_column(Integer, default=0)
    spend: Mapped[float] = mapped_column(Float, default=0.0)
    net: Mapped[float] = mapped_column(Float, default=0.0)
    cpl: Mapped[float | None] = mapped_column(Float)
    cpa: Mapped[float | None] = mapped_column(Float)
    cr: Mapped[float | None] = mapped_column(Float)
    profit: Mapped[float | None] = mapped_column(Float)
    roi: Mapped[float | None] = mapped_column(Float)
    top_ad_title: Mapped[str | None] = mapped_column(String(500))
    top_cm: Mapped[str | None] = mapped_column(String(100))
    top_account: Mapped[str | None] = mapped_column(String(200))

    analysis_run: Mapped["AnalysisRun"] = relationship(
        back_populates="combos"
    )


class UploadPlan(Base):
    """Plan row for Agent 8 upload generation."""
    __tablename__ = "upload_plan"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_run.id")
    )
    account: Mapped[str] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(100))
    asset: Mapped[str | None] = mapped_column(String(200))
    os: Mapped[str | None] = mapped_column(String(20))
    creative: Mapped[str | None] = mapped_column(String(20))
    targeting: Mapped[str | None] = mapped_column(String(200))
    age: Mapped[str | None] = mapped_column(String(10))
    creative_num: Mapped[str | None] = mapped_column(String(20))
    date: Mapped[str | None] = mapped_column(String(20))
    copy: Mapped[str | None] = mapped_column(Text)


class UploadFile(Base):
    """Generated upload Excel file."""
    __tablename__ = "upload_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int | None] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    file_path: Mapped[str] = mapped_column(String(1000))


class KBTarget(Base):
    """Knowledge base targeting entry."""
    __tablename__ = "kb_target"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    label: Mapped[str] = mapped_column(String(200))
    aliases: Mapped[list | None] = mapped_column(JSON)
    fb_interest: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str | None] = mapped_column(Text)
    top_countries: Mapped[list | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class KBOffer(Base):
    """Knowledge base offer entry."""
    __tablename__ = "kb_offer"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    label: Mapped[str | None] = mapped_column(String(300))
    language: Mapped[list | None] = mapped_column(JSON)
    top_countries: Mapped[list | None] = mapped_column(JSON)
    audience: Mapped[str | None] = mapped_column(Text)
    usp: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class KBCreative(Base):
    """Knowledge base creative entry."""
    __tablename__ = "kb_creative"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ReferenceData(Base):
    """Upload reference data (replaces Upluad FB.xlsx sheets).

    category: account | ad_copy | country | interest | video | image
    key: row identifier (account name, country code, etc.)
    data: JSON blob with all columns for that row
    """
    __tablename__ = "reference_data"
    __table_args__ = (
        UniqueConstraint("category", "key", name="uq_ref_cat_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(50))
    key: Mapped[str] = mapped_column(String(200))
    data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class AnalysisRules(Base):
    """Configurable analysis rules (replaces analysis_reading_rules.json)."""
    __tablename__ = "analysis_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_type: Mapped[str] = mapped_column(
        String(50), unique=True
    )  # device_keywords | age_range | text_segments | creative_skip_prefixes
    config: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class ChatConversation(Base):
    """AI chat conversation."""
    __tablename__ = "chat_conversation"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    title: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="conversation", order_by="ChatMessage.created_at"
    )


class ChatMessage(Base):
    """Individual message in a chat conversation."""
    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("chat_conversation.id")
    )
    role: Mapped[str] = mapped_column(
        String(20)
    )  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text)
    tool_calls: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    conversation: Mapped["ChatConversation"] = relationship(
        back_populates="messages"
    )
```

### 2.3 Database Engine Setup

```python
# src/fb_agents/db/engine.py
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from fb_agents.config import settings


def get_engine():
    return create_async_engine(
        f"sqlite+aiosqlite:///{settings.DB_PATH}",
        echo=settings.DEBUG,
    )


def get_session_factory(engine=None):
    engine = engine or get_engine()
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
```

### 2.4 Migration from Excel

| Excel source | DB table(s) | Migration strategy |
|---|---|---|
| `cursor example.xlsx` rows | `campaign_stats` + `stats_import` | Upload endpoint parses Excel via pandas, writes rows to DB |
| `Upluad FB.xlsx` → Accounts | `reference_data` (category=account) | One-time import endpoint; each row becomes a JSON blob |
| `Upluad FB.xlsx` → Ad copy | `reference_data` (category=ad_copy) | Same pattern |
| `Upluad FB.xlsx` → Countries | `reference_data` (category=country) | Same pattern |
| `Upluad FB.xlsx` → Interests | `reference_data` (category=interest) | Same pattern |
| `Upluad FB.xlsx` → Video/Image filenames | `reference_data` (category=video/image) | Same pattern |
| `config/knowledge_base.json` → targets | `kb_target` | Seed script reads JSON, upserts rows |
| `config/knowledge_base.json` → offers | `kb_offer` | Seed script reads JSON, upserts rows |
| `config/analysis_reading_rules.json` | `analysis_rules` | Seed script writes each rule type as a row |
| Output: combo DataFrames | `combo` + `analysis_run` | Service writes after each analysis |
| Output: upload .xlsx | `upload_plan` + `upload_file` | Service writes plan rows and records the generated file |
| Output: unknown_targets.log | `kb_target` (with flag) or log table | Service logs to DB instead of flat file |

---

## 3. API Surface

### 3.1 Route Structure

```
/api/v1/
├── stats/
│   ├── POST   /upload              — Upload stats Excel/CSV
│   ├── GET    /imports              — List past imports
│   └── GET    /imports/{id}         — Import detail + row count
│
├── analysis/
│   ├── POST   /run                  — Trigger analysis pipeline
│   ├── GET    /runs                 — List analysis runs
│   ├── GET    /runs/{run_id}        — Run detail + combo summary
│   ├── GET    /runs/{run_id}/combos — Full combo list (paginated)
│   ├── GET    /runs/{run_id}/report — Formatted text report
│   └── GET    /runs/{run_id}/cm-breakdown/{country}
│                                    — CM breakdown for country
│
├── upload/
│   ├── POST   /generate             — Generate upload Excel from combos
│   ├── GET    /files                — List generated files
│   └── GET    /files/{id}/download  — Download generated .xlsx
│
├── kb/
│   ├── GET    /targets              — List all targets
│   ├── GET    /targets/{code}       — Target detail + context
│   ├── PUT    /targets/{code}       — Update target
│   ├── POST   /targets              — Create target
│   ├── GET    /offers               — List all offers
│   ├── GET    /offers/{name}        — Offer detail
│   ├── PUT    /offers/{name}        — Update offer
│   ├── POST   /offers               — Create offer
│   ├── GET    /creatives            — List all creatives
│   └── POST   /sync                 — Trigger KB sync from external source
│
├── reference/
│   ├── POST   /upload               — Upload reference Excel (Upluad FB.xlsx)
│   ├── GET    /accounts             — List accounts
│   ├── GET    /countries            — List countries
│   └── GET    /interests            — List interests
│
├── chat/
│   ├── POST   /conversations        — Start new conversation
│   ├── GET    /conversations        — List conversations
│   ├── GET    /conversations/{id}   — Get conversation history
│   ├── POST   /conversations/{id}/messages
│   │                                — Send message (returns JSON)
│   └── POST   /conversations/{id}/stream
│                                    — Send message (returns SSE stream)
│
├── rules/
│   ├── GET    /                     — Get all analysis rules
│   └── PUT    /{rule_type}          — Update analysis rules
│
└── health                           — Health check
```

### 3.2 Key Request/Response Schemas (Pydantic)

```python
# src/fb_agents/api/schemas.py
from pydantic import BaseModel, Field


# --- Stats ---
class StatsUploadResponse(BaseModel):
    import_id: int
    filename: str
    row_count: int
    status: str


# --- Analysis ---
class AnalysisRunRequest(BaseModel):
    import_id: int | None = None  # None = use latest import
    country: str | None = None
    asset_filter: str | None = None
    top_n: int = Field(default=10, ge=1, le=100)


class ComboResponse(BaseModel):
    id: int
    country: str
    country_code: str | None
    offer: str | None
    device: str | None
    interest: str | None
    age: str | None
    creative: str | None
    text_segment: str | None
    category: str  # Strong | Interesting
    deposits: int
    leads: int
    spend: float
    net: float
    cpl: float | None
    cpa: float | None
    cr: float | None
    profit: float | None
    roi: float | None


class AnalysisRunResponse(BaseModel):
    run_id: str
    status: str
    created_at: str
    combo_count: int
    strong_count: int
    interesting_count: int


# --- Upload Generation ---
class UploadGenerateRequest(BaseModel):
    run_id: str
    account: str
    country: str | None = None
    top_n: int = Field(default=10, ge=1, le=100)


class UploadFileResponse(BaseModel):
    file_id: int
    filename: str
    download_url: str
    row_count: int


# --- Chat ---
class ChatMessageRequest(BaseModel):
    message: str


class ChatMessageResponse(BaseModel):
    role: str
    content: str
    tool_calls: list[dict] | None = None


# --- KB ---
class KBTargetResponse(BaseModel):
    code: str
    label: str
    aliases: list[str]
    fb_interest: str | None
    audience: str | None
    top_countries: list[str]
    notes: str | None


class KBOfferResponse(BaseModel):
    name: str
    label: str | None
    language: list[str]
    top_countries: list[str]
    audience: str | None
    usp: str | None
    notes: str | None
```

### 3.3 OpenAPI Auto-Generation

FastAPI auto-generates OpenAPI 3.x schema from the Pydantic models
and route type hints. Available at:

- `/docs` — Swagger UI (interactive)
- `/redoc` — ReDoc (reference)
- `/openapi.json` — raw schema

---

## 4. AI Agent Interface

### 4.1 Architecture

Custom lightweight wrapper (~300 lines) over the Anthropic
Claude SDK (primary) with OpenAI fallback. Uses the tool-use
pattern to invoke analysis functions.

```
┌──────────────────────────────────────────────────────┐
│                   AI Agent Module                      │
│                                                       │
│  ┌─────────────┐    ┌──────────────────────────────┐ │
│  │ System      │    │ Tool Registry                │ │
│  │ Prompt      │    │                              │ │
│  │ (persona +  │    │  analyze_campaigns()         │ │
│  │  context)   │    │  get_combos()                │ │
│  │             │    │  compare_offers()            │ │
│  │             │    │  get_cm_report()             │ │
│  │             │    │  generate_upload()           │ │
│  │             │    │  search_kb()                 │ │
│  │             │    │  get_campaign_stats()        │ │
│  └─────────────┘    └──────────────────────────────┘ │
│                                                       │
│  ┌─────────────────────────────────────────────────┐  │
│  │ Conversation Manager                            │  │
│  │ - Load/save conversation history (DB)           │  │
│  │ - Token counting + context window management    │  │
│  │ - Tool call execution + result formatting       │  │
│  │ - Streaming response iteration                  │  │
│  └─────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

### 4.2 Tool Definitions

Each tool maps directly to a service-layer function. The LLM
receives JSON schema definitions and calls them by name.

```python
# src/fb_agents/agent/tools.py
TOOLS = [
    {
        "name": "analyze_campaigns",
        "description": (
            "Run full campaign analysis on imported stats data. "
            "Returns Strong and Interesting ad-set combos ranked "
            "by deposits and ROI."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "country": {
                    "type": "string",
                    "description": "Filter by country name, or 'all'",
                },
                "top_n": {
                    "type": "integer",
                    "description": "Max combos to return (default 10)",
                },
                "asset_filter": {
                    "type": "string",
                    "description": "Filter by ad asset/offer name",
                },
            },
        },
    },
    {
        "name": "get_combos",
        "description": (
            "Retrieve combos from a completed analysis run. "
            "Can filter by country and category (Strong/Interesting)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "country": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": ["Strong", "Interesting", "all"],
                },
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "compare_offers",
        "description": (
            "Compare offers for a country: deposits, leads, spend, "
            "net, CPL, CPA, CR, ROI per offer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "country": {"type": "string"},
            },
            "required": ["country"],
        },
    },
    {
        "name": "get_cm_report",
        "description": (
            "Campaign Manager breakdown: performance by CM "
            "for a country."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "country": {"type": "string"},
            },
            "required": ["country"],
        },
    },
    {
        "name": "generate_upload",
        "description": (
            "Generate a Facebook bulk-upload Excel file from "
            "selected combos for a specific account."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "account": {"type": "string"},
                "country": {"type": "string"},
                "top_n": {"type": "integer"},
            },
            "required": ["run_id", "account"],
        },
    },
    {
        "name": "search_kb",
        "description": (
            "Search the knowledge base for targeting, offers, "
            "or creatives. Returns matching entries with context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": ["targets", "offers", "creatives", "all"],
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_campaign_stats",
        "description": (
            "Get summary statistics: total spend, leads, FTDs, "
            "top countries, top offers from imported data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "country": {"type": "string"},
                "offer": {"type": "string"},
            },
        },
    },
]
```

### 4.3 Agent Runner

```python
# src/fb_agents/agent/runner.py  (simplified sketch)
import anthropic

class AgentRunner:
    def __init__(self, service_layer, model="claude-sonnet-4-20250514"):
        self.client = anthropic.AsyncAnthropic()
        self.model = model
        self.service = service_layer
        self.tool_handlers = {
            "analyze_campaigns": service.run_analysis,
            "get_combos": service.get_combos,
            "compare_offers": service.compare_offers,
            "get_cm_report": service.get_cm_report,
            "generate_upload": service.generate_upload,
            "search_kb": service.search_kb,
            "get_campaign_stats": service.get_campaign_stats,
        }

    async def stream_response(self, conversation_id, user_message):
        """Yield SSE chunks: text deltas + final tool results."""
        history = await self.service.get_conversation(conversation_id)
        messages = [*history, {"role": "user", "content": user_message}]

        async with self.client.messages.stream(
            model=self.model,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=TOOLS,
            max_tokens=4096,
        ) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    yield {"event": "delta", "data": event.delta.text}
                elif event.type == "content_block_stop":
                    # Check for tool use, execute, continue
                    pass

        # Save full conversation to DB
        await self.service.save_message(conversation_id, ...)
```

### 4.4 System Prompt

The system prompt gives the agent its persona and available context:

```
You are an FB campaign analysis assistant. You help media buyers
analyze campaign performance, find winning ad-set combinations,
and generate Facebook bulk-upload files.

You have access to the following tools:
- analyze_campaigns: Run analysis on imported stats
- get_combos: Retrieve analysis results
- compare_offers: Compare offer performance by country
- get_cm_report: Campaign manager performance breakdown
- generate_upload: Create FB upload Excel files
- search_kb: Search targeting/offer knowledge base
- get_campaign_stats: Get summary statistics

When users ask about campaign performance, use analyze_campaigns
first, then present results in a clear table format. When users
want to create campaigns, use generate_upload after confirming
the account and country.

Always explain your findings in simple terms — your users are
media buyers, not data scientists.
```

### 4.5 Streaming Protocol

The `/api/v1/chat/conversations/{id}/stream` endpoint uses
Server-Sent Events (SSE):

```
POST /api/v1/chat/conversations/1/stream
Content-Type: application/json
{"message": "Show me top combos for Mexico"}

Response: text/event-stream

event: delta
data: {"text": "I'll analyze the "}

event: delta
data: {"text": "campaign data for Mexico..."}

event: tool_call
data: {"name": "analyze_campaigns", "input": {"country": "Mexico"}}

event: tool_result
data: {"combos": [...], "summary": "Found 5 Strong, 3 Interesting"}

event: delta
data: {"text": "Here are the top combos for Mexico:\n\n"}

event: done
data: {"message_id": 42}
```

---

## 5. Web Application Architecture

### 5.1 Page Structure

```
/ (redirect → /dashboard)
├── /dashboard          — Overview: latest import stats, top combos
├── /analysis           — Run analysis, view results
│   ├── /analysis/run   — Analysis form + results (HTMX partial)
│   └── /analysis/{id}  — Specific run detail
├── /upload             — Generate upload files
│   ├── /upload/generate — Form: select run + account → download
│   └── /upload/files   — List generated files
├── /kb                 — Knowledge base management
│   ├── /kb/targets     — Target list + edit
│   ├── /kb/offers      — Offer list + edit
│   └── /kb/creatives   — Creative list + edit
├── /chat               — AI assistant (full-page chat)
├── /settings           — File upload, rules config
│   ├── /settings/import — Upload stats/reference Excel
│   └── /settings/rules  — Edit analysis rules
└── /health             — Health check (API, no template)
```

### 5.2 Template Structure

```
src/fb_agents/
├── templates/
│   ├── base.html           — Layout: nav, Tailwind CDN, HTMX script
│   ├── components/
│   │   ├── nav.html        — Navigation sidebar
│   │   ├── combo_table.html — Reusable combo results table
│   │   ├── stats_card.html — Summary metric card
│   │   └── chat_message.html — Single chat message bubble
│   ├── dashboard.html
│   ├── analysis/
│   │   ├── index.html      — Analysis page with run form
│   │   ├── results.html    — HTMX partial: combo results
│   │   └── detail.html     — Single run detail
│   ├── upload/
│   │   ├── generate.html   — Upload generation form
│   │   └── files.html      — File list
│   ├── kb/
│   │   ├── targets.html
│   │   ├── offers.html
│   │   └── creatives.html
│   ├── chat.html           — AI chat interface
│   └── settings/
│       ├── import.html
│       └── rules.html
└── static/
    ├── css/
    │   └── app.css         — Minimal custom styles (Tailwind handles most)
    └── js/
        └── chat.js         — SSE handling for chat streaming (~50 lines)
```

### 5.3 HTMX Interaction Patterns

**Analysis trigger (no page reload):**
```html
<form hx-post="/analysis/run"
      hx-target="#results"
      hx-indicator="#spinner">
  <select name="country">...</select>
  <input name="top_n" type="number" value="10">
  <button type="submit">Run Analysis</button>
  <div id="spinner" class="htmx-indicator">Analyzing...</div>
</form>
<div id="results">
  <!-- Server returns rendered combo_table.html partial -->
</div>
```

**AI chat streaming (SSE):**
```html
<div id="chat-messages">
  <!-- Messages rendered server-side -->
</div>
<form hx-post="/chat/send"
      hx-target="#chat-messages"
      hx-swap="beforeend">
  <input name="message" placeholder="Ask about campaigns...">
  <button type="submit">Send</button>
</form>
```

The `chat.js` script (~50 lines) handles SSE streaming for
real-time token display:

```javascript
// static/js/chat.js
async function sendMessage(conversationId, message) {
    const response = await fetch(
        `/api/v1/chat/conversations/${conversationId}/stream`,
        {method: "POST", headers: {"Content-Type": "application/json"},
         body: JSON.stringify({message})}
    );
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const msgEl = createMessageBubble("assistant");

    while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value);
        for (const line of chunk.split("\n")) {
            if (line.startsWith("data: ")) {
                const data = JSON.parse(line.slice(6));
                if (data.text) msgEl.textContent += data.text;
            }
        }
    }
}
```

### 5.4 Design Principles

- **Tailwind CSS via CDN** — no build step. Use `<script src="https://cdn.tailwindcss.com">` in dev,
  standalone CLI for production builds.
- **Mobile-first responsive** — all pages work on tablet/phone
  (media buyers often check on mobile).
- **Zero JavaScript framework** — HTMX for interactions, vanilla
  JS only for SSE streaming.
- **Server-rendered partials** — HTMX swaps HTML fragments
  returned by FastAPI Jinja2 routes. No JSON-to-DOM rendering.

---

## 6. Deployment Model

### 6.1 Container Architecture

Single Docker container for v1. FastAPI serves both API and
static/template content via uvicorn.

```
┌─────────────────────────────────────────┐
│           Docker Container               │
│                                          │
│  ┌────────────────────────────────────┐  │
│  │  uvicorn (ASGI server)            │  │
│  │  ├── FastAPI app                  │  │
│  │  │   ├── API routes (/api/v1/*)   │  │
│  │  │   ├── Web routes (/)           │  │
│  │  │   └── Static files (/static/)  │  │
│  │  └── Workers: 1 (single-tenant)   │  │
│  └────────────────────────────────────┘  │
│                                          │
│  ┌────────────────────────────────────┐  │
│  │  /data/ (mounted volume)          │  │
│  │  ├── db.sqlite3                   │  │
│  │  ├── uploads/  (imported files)   │  │
│  │  └── output/   (generated files)  │  │
│  └────────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

### 6.2 Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application
COPY src/ src/

# Create data directory
RUN mkdir -p /data/uploads /data/output

# Default environment
ENV FB_AGENTS_DB_PATH=/data/db.sqlite3
ENV FB_AGENTS_UPLOAD_DIR=/data/uploads
ENV FB_AGENTS_OUTPUT_DIR=/data/output
ENV FB_AGENTS_HOST=0.0.0.0
ENV FB_AGENTS_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["python", "-m", "uvicorn", "fb_agents.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]
```

### 6.3 Docker Compose

```yaml
# docker-compose.yml
version: "3.8"

services:
  app:
    build: .
    ports:
      - "${FB_AGENTS_PORT:-8000}:8000"
    volumes:
      - app-data:/data
    environment:
      - FB_AGENTS_DB_PATH=/data/db.sqlite3
      - FB_AGENTS_UPLOAD_DIR=/data/uploads
      - FB_AGENTS_OUTPUT_DIR=/data/output
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - FB_AGENTS_DEBUG=${FB_AGENTS_DEBUG:-false}
    restart: unless-stopped

volumes:
  app-data:
```

### 6.4 Configuration (Environment Variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `FB_AGENTS_DB_PATH` | `/data/db.sqlite3` | SQLite database file path |
| `FB_AGENTS_UPLOAD_DIR` | `/data/uploads` | Uploaded files directory |
| `FB_AGENTS_OUTPUT_DIR` | `/data/output` | Generated files directory |
| `FB_AGENTS_HOST` | `0.0.0.0` | Server bind address |
| `FB_AGENTS_PORT` | `8000` | Server port |
| `FB_AGENTS_DEBUG` | `false` | Debug mode (SQL echo, reload) |
| `ANTHROPIC_API_KEY` | — | Claude API key (required for AI agent) |
| `OPENAI_API_KEY` | — | OpenAI fallback key (optional) |
| `FB_AGENTS_AI_MODEL` | `claude-sonnet-4-20250514` | LLM model override |
| `FB_AGENTS_SECRET_KEY` | auto-generated | Secret for sessions (future auth) |

### 6.5 Per-Customer Isolation

Each customer gets their own container instance with its own
volume. Isolation is achieved at the Docker level:

- **Data isolation:** Separate SQLite file per volume
- **Config isolation:** Separate env vars per container
- **Network isolation:** Separate port mappings or reverse proxy routing

No multi-tenant logic in the application code.

---

## 7. Agent Logic Migration

How existing analysis/upload logic adapts to the new data layer.

### 7.1 Agent 1 (Analysis) — Excel → DB

| Current (Excel-based) | New (DB-based) |
|---|---|
| `pd.read_excel(path)` → DataFrame | `SELECT * FROM campaign_stats WHERE import_id = ?` → DataFrame via `pd.read_sql` |
| `compute_metrics(df)` | **Unchanged** — operates on DataFrame |
| `enrich_structure(df)` | **Unchanged** — operates on DataFrame |
| `aggregate_combos(df)` | **Unchanged** — operates on DataFrame |
| Combo results printed to console | Combo results written to `combo` table + returned via API |
| `analysis_reading_rules.json` | `SELECT config FROM analysis_rules WHERE rule_type = ?` |
| `knowledge_base.py` file-based lookup | `SELECT * FROM kb_target WHERE code = ? OR ? = ANY(aliases)` |

**Key insight:** The core pandas pipeline (`compute_metrics` →
`enrich_structure` → `aggregate_combos`) is unchanged. Only the
input source (DB instead of Excel) and output destination (DB
instead of console) change. The functions accept and return
DataFrames — this interface is preserved.

### 7.2 Agent 8 (Upload) — Excel → DB

| Current (Excel-based) | New (DB-based) |
|---|---|
| `load_reference_data(excel_path)` reads Excel sheets | `ReferenceDataRepo.get_all()` returns same dict structure from DB |
| Plan comes from CSV or bridge function | Plan comes from `upload_plan` table or API request |
| `build_row()` constructs upload row | **Unchanged** — takes same dict inputs |
| `write_upload_excel()` writes .xlsx | **Unchanged** — still produces .xlsx file, stored in output dir |

**Migration wrapper:** The `load_reference_data()` function gets
a DB-backed alternative that returns the identical dict structure:

```python
async def load_reference_data_from_db(session) -> dict:
    """Drop-in replacement for load_reference_data(excel_path).

    Returns same dict shape: {accounts, ad_copy, countries,
    interests, video_files, image_files, template_headers}
    """
    rows = await session.execute(
        select(ReferenceData)
    )
    result = {
        "accounts": {}, "ad_copy": {}, "countries": {},
        "interests": {}, "video_files": {}, "image_files": {},
        "template_headers": [],
    }
    for row in rows.scalars():
        if row.category == "account":
            result["accounts"][row.key] = row.data
        elif row.category == "ad_copy":
            result["ad_copy"][row.key] = row.data
        # ... etc
    return result
```

### 7.3 KB Agent — File → DB

| Current (file-based) | New (DB-based) |
|---|---|
| `_load_json()` reads `knowledge_base.json` | `KBTargetRepo.get_by_code()` queries DB |
| `resolve_target_code()` — exact → case-insensitive → aliases | Same logic, but `SELECT` with `LOWER(code)` and JSON aliases search |
| `log_unknown_target()` appends to file | Insert/upsert to an `unknown_targets` log table or flag on `kb_target` |
| `load_markdown_section()` reads `.md` files | Store markdown in DB `description` column or keep files for backward compat |

### 7.4 Service Layer Pattern

All business logic lives in service classes, not in routes or
models. Routes are thin — they parse requests, call services,
format responses.

```python
# src/fb_agents/services/analysis.py
class AnalysisService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def run_analysis(
        self, import_id=None, country=None,
        asset_filter=None, top_n=10,
    ) -> AnalysisRunResponse:
        async with self.session_factory() as session:
            # 1. Load stats from DB into DataFrame
            df = await self._load_stats(session, import_id)
            # 2. Run existing pipeline (unchanged)
            df = compute_metrics(df)
            df = enrich_structure(df)
            combos = aggregate_combos(df)
            # 3. Apply filters
            if country: ...
            if asset_filter: ...
            # 4. Save results to DB
            run = AnalysisRun(...)
            session.add(run)
            for _, row in combos.iterrows():
                session.add(Combo(run_id=run.id, ...))
            await session.commit()
            return AnalysisRunResponse(...)
```

---

## 8. Project Structure

```
fb_agents/
├── pyproject.toml                  — Package metadata, dependencies
├── Dockerfile
├── docker-compose.yml
├── .env.example
│
├── src/
│   └── fb_agents/
│       ├── __init__.py
│       ├── main.py                 — FastAPI app factory
│       ├── config.py               — Settings (pydantic-settings)
│       │
│       ├── db/
│       │   ├── __init__.py
│       │   ├── engine.py           — Async engine + session factory
│       │   ├── models.py           — SQLAlchemy models (§ 2.2)
│       │   └── migrations/         — Alembic migrations
│       │
│       ├── api/
│       │   ├── __init__.py
│       │   ├── schemas.py          — Pydantic request/response models
│       │   ├── deps.py             — FastAPI dependencies (DB session)
│       │   └── routes/
│       │       ├── __init__.py
│       │       ├── stats.py
│       │       ├── analysis.py
│       │       ├── upload.py
│       │       ├── kb.py
│       │       ├── reference.py
│       │       ├── chat.py
│       │       ├── rules.py
│       │       └── health.py
│       │
│       ├── web/
│       │   ├── __init__.py
│       │   └── routes.py           — Jinja2 page routes
│       │
│       ├── services/
│       │   ├── __init__.py
│       │   ├── analysis.py         — Analysis pipeline (wraps Agent 1)
│       │   ├── upload.py           — Upload generation (wraps Agent 8)
│       │   ├── kb.py               — Knowledge base operations
│       │   ├── reference.py        — Reference data management
│       │   ├── stats.py            — Stats import
│       │   └── chat.py             — Chat conversation management
│       │
│       ├── agent/
│       │   ├── __init__.py
│       │   ├── runner.py           — AgentRunner (LLM + tool dispatch)
│       │   ├── tools.py            — Tool definitions
│       │   └── prompts.py          — System prompt
│       │
│       ├── core/
│       │   ├── __init__.py
│       │   ├── analysis_pipeline.py — compute_metrics, enrich, aggregate
│       │   ├── upload_pipeline.py   — build_row, write_upload_excel
│       │   ├── knowledge_base.py    — resolve_target_code, etc.
│       │   └── analysis_rules.py    — Rule loading (from DB)
│       │
│       ├── templates/              — Jinja2 templates (§ 5.2)
│       └── static/                 — CSS + JS (§ 5.2)
│
├── tests/
│   ├── conftest.py                 — Fixtures: async DB, test client
│   ├── test_api/
│   │   ├── test_stats.py
│   │   ├── test_analysis.py
│   │   ├── test_upload.py
│   │   ├── test_kb.py
│   │   └── test_chat.py
│   ├── test_services/
│   │   ├── test_analysis_service.py
│   │   ├── test_upload_service.py
│   │   └── test_kb_service.py
│   └── test_core/
│       ├── test_analysis_pipeline.py
│       ├── test_upload_pipeline.py
│       └── test_knowledge_base.py
│
├── scripts/
│   ├── seed_db.py                  — Seed DB from existing JSON/Excel
│   └── migrate_excel_to_db.py      — One-time migration helper
│
└── config/                         — Legacy config (kept for migration)
    ├── analysis_reading_rules.json
    ├── knowledge_base.json
    └── paths.py
```

### 8.1 Key Design Decisions

1. **`core/` preserves existing logic** — `analysis_pipeline.py`
   contains the existing `compute_metrics`, `enrich_structure`,
   `aggregate_combos` functions extracted from
   `analyze_fb_stats.py`. These operate on DataFrames and are
   unit-testable without a database.

2. **`services/` wraps core with DB** — Service classes handle
   DB reads/writes and call `core/` functions. This separation
   means the pandas pipeline can be tested independently.

3. **`api/routes/` are thin** — Routes parse HTTP, call services,
   format responses. No business logic.

4. **`web/routes.py`** is separate from API routes. Web routes
   return rendered HTML via Jinja2. They call the same services.

---

## 9. Dependency Summary

```toml
# pyproject.toml [project.dependencies]
[project]
name = "fb_agents"
version = "0.1.0"
requires-python = ">=3.11"

dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "sqlalchemy[asyncio]>=2.0",
    "aiosqlite>=0.20",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "jinja2>=3.1",
    "python-multipart>=0.0.9",  # file upload
    "pandas>=2.0",
    "openpyxl>=3.1",
    "anthropic>=0.40",
    "sse-starlette>=2.0",       # SSE support
    "alembic>=1.13",            # DB migrations
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "httpx>=0.27",              # async test client
    "ruff>=0.4",
]
```

---

## 10. Planned Agent Adaptation

### Agent 6 (Jira → Kitaro) — Future

Not in scope for v1. When implemented:
- Add `reference_data` category `kitaro_template`
- Add API route `POST /api/v1/kitaro/create`
- Add tool `create_kitaro_campaign` to AI agent

### Agent 7 (Approved Combos) — Partially Replaced

The web UI's analysis page replaces the "approval UI" concept.
Users view combos in the browser, select which to include, and
trigger upload generation. The `upload_plan` table records
selections.

### Agent 9 (Daily Task Manager) — Future

Not in scope for v1. When implemented:
- Add `daily_task` and `team_member` tables
- Add API routes under `/api/v1/tasks/`
- Add tool `get_daily_tasks` to AI agent

---

## Tags

#architecture #spec #service #fastapi #sqlite #htmx #docker #data-model #api
