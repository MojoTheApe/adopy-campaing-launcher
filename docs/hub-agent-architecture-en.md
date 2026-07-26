---
kind: spec
name: hub-agent-architecture-en
agent: architect
task: "[[global-rollout-rpa-agent]]"
created: 2026-07-22
status: draft
version: 1
tags:
  - architecture
  - spec
  - rpa
  - api
  - data-model
  - security
related:
  - global-rollout-overview.md
  - agent-update-strategy-en.md
  - hub-agent-architecture.md
---

# Hub + Local Agent — Technical Spec

English version of `hub-agent-architecture.md`. Detailed technical
design for developers. Read `global-rollout-overview.md` first (business
context, and what this is *not* — don't confuse it with
`service-architecture.md`).

## 1. Goal and scope

Let any employee at the company launch a campaign creation job
(Agent 10, `run_recorded_campaign_flow` in `src/fb_ads_rpa/meta_dom.py`)
from their own computer, through their own already-open Multilogin
profile, while the dashboard and all data (accounts, results, run
history) stay shared on one hub.

**In scope:** job queue data model, hub↔agent API contract, security
model, job lifecycle, observability.

**Out of scope:** the RPA engine itself (`run_recorded_campaign_flow`,
`ads_manager_flow.py`, `RpaPublisher`) — reused as-is, not rewritten.
The agent is the existing `scripts/run_rpa_publish.py` code, wrapped
in a queue-polling loop instead of CLI arguments.

## 2. Component architecture

```
┌────────────────────────────────────────────────────────────┐
│ HUB (domain, shared by everyone)                              │
│                                                              │
│  ┌────────────┐  ┌────────────┐  ┌─────────────────────┐    │
│  │ Web Dashboard│  │ Job Queue  │  │ Shared DB           │    │
│  │ (FastAPI+   │  │ API        │  │ (see §3)            │    │
│  │  Jinja2 —   │  │ /api/v1/   │  │ users, accounts,    │    │
│  │  reuses     │  │ jobs/*     │  │ jobs, artifacts,     │    │
│  │  src/web/)  │  │ /api/v1/   │  │ campaign_stats/combo │    │
│  │             │  │ agent/*    │  │ (from service-arch.) │    │
│  └────────────┘  └────────────┘  └─────────────────────┘    │
└──────────────────────────┬───────────────────────────────────┘
                           │ HTTPS, the agent initiates (poll)
        ┌──────────────────┼──────────────────┐
        ▼                                     ▼
┌───────────────────┐               ┌───────────────────┐
│ Local Agent (user A)│               │ Local Agent (user B)│
│ ─ poll loop         │               │ ─ poll loop         │
│ ─ Multilogin        │               │ ─ Multilogin        │
│ ─ Playwright        │               │ ─ Playwright        │
│   (run_recorded_    │               │   (run_recorded_    │
│    campaign_flow)   │               │    campaign_flow)   │
│ ─ screenshot upload │               │ ─ screenshot upload │
└───────────────────┘               └───────────────────┘
```

## 3. Data model (shared DB)

New tables specific to this initiative. Existing tables from
`service-architecture.md` (`campaign_stats`, `combo`, `kb_*`, etc.)
can coexist in the same DB if the analysis side moves there too —
that's a separate decision and doesn't block this spec.

```python
class User(Base):
    """A company employee with access to the hub."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # cm | admin
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentToken(Base):
    """Token the local agent uses to authenticate to the hub.

    One active token per user. Not the same as dashboard auth
    (SSO/login) -- this is a separate secret that lives in the
    agent's local config (%APPDATA%\\fb_agents\\local.env), never in git.
    """
    __tablename__ = "agent_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(128))  # store only the hash
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class AdAccountAssignment(Base):
    """Who currently owns which ad account.

    Replaces the flat config/ad_account_map.csv -- same information
    (multilogin_instance, folder_id, account_label, country), plus a
    link to a specific user_id, which the CSV has no concept of today.
    """
    __tablename__ = "ad_account_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    account_label: Mapped[str] = mapped_column(String(200))  # e.g. "FacebookUL921 CR Ait"
    ad_account_id: Mapped[str] = mapped_column(String(50))
    country: Mapped[str] = mapped_column(String(10))
    multilogin_instance: Mapped[str] = mapped_column(String(10))
    multilogin_folder_id: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(default=True)


class Job(Base):
    """One 'publish this campaign' task in the queue."""
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assigned_to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    ad_account_assignment_id: Mapped[int] = mapped_column(
        ForeignKey("ad_account_assignments.id")
    )
    campaign_payload: Mapped[dict] = mapped_column(JSON)  # slots-json equivalent
    status: Mapped[str] = mapped_column(String(20), default="queued")
    # queued | claimed | running | succeeded | failed | cancelled | expired
    result: Mapped[dict | None] = mapped_column(JSON)  # RpaPublishResult.to_job_dict()
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class JobArtifact(Base):
    """A screenshot/log uploaded by the agent while running a job."""
    __tablename__ = "job_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    label: Mapped[str] = mapped_column(String(200))  # e.g. "recorded_success_website_selected"
    file_path: Mapped[str] = mapped_column(String(500))  # path in shared storage, not the user's machine
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
```

Note: `campaign_payload` and `result` are the same structures already
in use today (the `slots-json` format from `scripts/run_rpa_publish.py`
and `RpaPublishResult.to_job_dict()` from `src/fb_ads_rpa/draft.py`).
The schema doesn't reinvent the data format, it just wraps it in a
queue with a user link.

## 4. API contract

### 4.1 From the dashboard side (a user creates a job)

```
POST /api/v1/jobs
Body: {
  "ad_account_assignment_id": 12,
  "campaign_payload": { ...same slots-json as today... }
}
→ 201 { "job_id": "uuid", "status": "queued" }

GET /api/v1/jobs/{job_id}
→ { "job_id", "status", "result", "error", "artifacts": [...] }

GET /api/v1/jobs?assigned_to=me&status=queued
→ list of jobs (for a "my queue" view)
```

On `POST /jobs`, the server checks that `ad_account_assignment.user_id`
matches the currently authenticated user, OR the caller has the
`admin` role — a regular employee cannot queue a job against someone
else's account.

### 4.2 From the local agent side (executing a job)

The agent always **initiates** requests (poll); the hub never reaches
into the agent directly — no inbound ports need to be opened on an
employee's machine.

```
GET /api/v1/agent/jobs/next
Headers: Authorization: Bearer <agent_token>
→ 200 { "job_id", "campaign_payload", "ad_account": {...} }   (a job is available)
→ 204                                                          (no job)
```

The server finds the first `queued` job where
`ad_account_assignment.user_id == the calling agent's user`
(from the token), marks it `claimed`, sets `claimed_at = now()`, and
returns it to the agent. If the agent doesn't report a status within
the timeout (see §6), the job automatically goes back to `queued`
(it never hangs forever).

```
POST /api/v1/agent/jobs/{job_id}/status
Body: { "status": "running" | "succeeded" | "failed", "result": {...}, "error": "..." }
→ 200 OK

POST /api/v1/agent/jobs/{job_id}/artifacts
Body: multipart/form-data { "label": "...", "file": <screenshot.png> }
→ 200 OK
```

### 4.3 Polling vs push

A simple long-poll is recommended (the agent waits up to N seconds
for a response from `GET /jobs/next`; the server holds the connection
open and returns immediately if a job shows up) instead of WebSockets —
fewer moving parts, more resilient to network drops on employee
machines. Default poll interval: 5-10 seconds between attempts.

## 5. Security model

- **Agent auth** — a bearer token per user (`AgentToken`), stored in
  the agent's local config, never in git (see
  `agent-update-strategy-en.md`, the section on keeping personal
  config out of the repo). A token can be revoked individually
  (`revoked_at`) without reissuing tokens to everyone else.
- **Token scope** — the token is tied to a `user_id`, not to a
  specific account; which accounts are actually accessible is
  determined by `AdAccountAssignment`, not by the token. Reassigning
  an account doesn't require reissuing a token.
- **LLM secrets (Anthropic/OpenAI/Gemini) stay on the hub only.** The
  agent doesn't need them — all of the agent's work is Multilogin +
  Playwright, no LLM API calls. If a future step inside the agent
  needs an LLM call (like `llm_agent.py` today), route it through the
  hub (the agent asks the hub "analyze this screenshot for me" rather
  than holding its own key) so the key isn't copied onto N machines.
- **Dashboard auth** (who logs in to view/queue jobs) is a separate
  question from agent auth — see the open question in
  `global-rollout-overview.md` (SSO vs a custom login system).

## 6. Job lifecycle

```
queued ──(agent claims)──► claimed ──(agent starts)──► running
                                                          │
                            ┌─────────────────────────────┤
                            ▼                             ▼
                       succeeded                       failed
```

- `queued → claimed`: happens on `GET /agent/jobs/next`.
- `claimed → running`: the agent sends
  `POST .../status {"status":"running"}` right after starting (before
  opening the browser) — if the agent crashes between claim and this
  step, the hub can see "claimed a while ago, never went running" and
  put the job back in the queue.
- Timeout: if `claimed_at` is older than N minutes (10-15 recommended,
  given today's live runs sometimes sat on a "Loading..." screen for
  tens of seconds) without transitioning to `running`, the job
  automatically returns to `queued` (it can be picked up again by the
  same or another agent with access to that account).
- `running → succeeded/failed`: final status; `result`/`error` are
  filled from the same `RpaPublishResult` that
  `ads_manager_flow.py::publish_campaign` already returns today.
- Manual cancellation (`cancelled`) — a user can cancel their own
  `queued` job from the dashboard; `claimed`/`running` jobs cannot be
  cancelled (the RPA has already started clicking through the real
  Ads Manager — stopping mid-way is riskier than letting it finish
  into a draft).

## 7. Observability

- Every `JobArtifact` (screenshot) is uploaded to the hub as it
  happens — the same `_shot()` logic already in
  `run_recorded_campaign_flow`, just replacing a write to the local
  `output/rpa_screenshots/` with
  `POST /agent/jobs/{id}/artifacts`.
- The dashboard shows live job status (polling `GET /jobs/{id}` every
  few seconds, the same pattern already implemented for
  `/publish-jobs/{id}` in the current `src/web/app.py`).
- Monitoring metric: share of `failed`/`expired` jobs in a rolling
  window, per account. A sudden spike is a signal that a specific
  account has "gotten tired" of frequent RPA attempts (see the
  operational note in `global-rollout-overview.md`) and deserves a
  pause on that account specifically, not a system-wide one.

## 8. What's reused as-is (do not rewrite)

| Existing code | Role in the new architecture |
|---|---|
| `src/fb_ads_rpa/meta_dom.py` (`run_recorded_campaign_flow` and helpers) | The RPA engine inside the local agent, unchanged |
| `src/fb_ads_rpa/ads_manager_flow.py` (`publish_campaign`) | Called by the agent the same way `RpaPublisher` calls it today |
| `src/fb_ads_rpa/draft.py` (`RpaPublishResult`, `CampaignDraft`) | Data format for `campaign_payload`/`result` in the queue — unchanged |
| `scripts/run_rpa_publish.py` | The "build drafts and call publish_campaign" logic — wrapped in a poll loop instead of CLI arguments |
| `src/web/app.py` (`/publish-jobs*`) | A prototype job model already exists locally (`start_job`, `get_job`, polling) — extended to a shared DB and agent assignment, not rewritten from scratch |

## 9. Not covered by this spec (open questions / future work)

- Hub hosting and dashboard auth — see `global-rollout-overview.md`.
- Scheduling campaigns "for a specific time, while the employee isn't
  at their computer" — today's model requires the agent to be running
  (machine on, Multilogin open); unattended/delayed launches are a
  separate future task, not part of this phase.
- Automatic agent selection when an account is accessible to multiple
  people — in the MVP, one account = one assigned employee, no load
  balancing across multiple agents.

#architecture #spec #rpa #api #data-model #security #job-queue
