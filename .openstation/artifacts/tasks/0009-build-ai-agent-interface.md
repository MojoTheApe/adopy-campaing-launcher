---
kind: task
name: 0009-build-ai-agent-interface
type: feature
status: backlog
assignee: developer
owner: user
parent: "[[0002-refactor-fb-agents-mvp-into]]"
created: 2026-04-02
---

# Build Ai Agent Interface

## Requirements

1. Implement conversational AI agent based on service architecture spec (0004-design-service-architecture-spec).
2. Agent can answer questions about campaign performance ("which country has best ROI?", "top offers in Ecuador").
3. Agent can trigger analysis runs and present results conversationally.
4. Agent can initiate upload generation from selected combos.
5. Agent can query and update Knowledge Base (targets, offers).
6. Agent consumes the REST API (0007-build-rest-api-layer) as its tool layer.
7. Based on AI agent framework chosen in tech stack research (0003-research-tech-stack-for-service).

## Verification

- [ ] Agent responds to natural language queries about campaign data
- [ ] Agent can trigger analysis and summarize results
- [ ] Agent can generate upload files from conversation
- [ ] Agent can query KB data
- [ ] Agent uses REST API (no direct DB access)
- [ ] Agent framework matches tech stack recommendation
