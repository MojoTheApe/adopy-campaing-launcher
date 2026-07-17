# Agent 10 RPA Variations Learning Machine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a thin full-path UI variation catalog, a hybrid ranked-strategy learner (Draft-only / never Publish), and an Agent 10 Cursor skill + `/rpa-audit` command so RPA debug sessions follow the approved ground-truth recipe.

**Architecture:** Add `config/rpa_variations/` (per-step YAML catalog + `strategy_ranks.json`) and a small `src/fb_ads_rpa/variations/` package (load catalog, read/write ranks, hybrid promote/demote). Hook AdsManagerFlow to log step outcomes and try strategies in ranked order with fallback to today’s code order. Skill + slash teach the agent how to audit screenshots against the checklist. No Publish path; finish is always Draft/hand-off.

**Tech Stack:** Python 3, PyYAML (already used for selectors), JSON, pytest, existing Playwright RPA (`src/fb_ads_rpa/`), Cursor skills/commands under `.cursor/`.

## Global Constraints

- Campaigns stay **Draft / hand-off only** — never click Publish; do not rank Publish strategies.
- Ground truth recipe: Create → Manual → Leads → Website → Lead → audience → ads → Draft.
- Learning **hybrid C**: always log; safe auto-promote only after DOM success **and** next major checklist step progresses; risky (conversion location, conversion event) → `pending_approval` until teach/human approve.
- Prefer Multilogin Desktop / DirectCDP; do not use Playwright Chromium against MLX Chrome 149+ profiles as the primary path.
- Phase 1: thin catalog for **all** checklist steps; deepen conversion in a later plan.
- Spec: `docs/superpowers/specs/2026-07-15-agent10-rpa-variations-design.md`.

---

## File Structure

- Create `config/rpa_variations/checklist.md` — human-readable ground-truth steps 1–10.
- Create `config/rpa_variations/steps.yaml` — single catalog file with all phase-1 steps (thin).
- Create `config/rpa_variations/strategy_ranks.json` — empty/default ranks store (committed starter).
- Create `src/fb_ads_rpa/variations/__init__.py` — public exports.
- Create `src/fb_ads_rpa/variations/catalog.py` — load `steps.yaml`, risk lookup, default strategy order.
- Create `src/fb_ads_rpa/variations/ranks.py` — load/save ranks, log outcome, hybrid promote/demote, approve pending.
- Create `src/fb_ads_rpa/variations/paths.py` — resolve catalog/ranks paths via repo root.
- Modify `src/fb_ads_rpa/ads_manager_flow.py` — call ranks logger around key steps; use ordered strategies for conversion Website pick path; ensure `_finish_publish` never learns Publish.
- Modify `src/web/app.py` — optional `POST /rpa-teach/approve-strategy` to approve risky pending.
- Create `.cursor/skills/agent10-rpa-variations/SKILL.md` — auto + audit instructions + task prompts.
- Create `.cursor/commands/rpa-audit.md` — slash `/rpa-audit`.
- Modify `docs/agent10-fb-ads-rpa.md` — link to variations catalog + ranks.
- Create `tests/test_rpa_variations_catalog.py`
- Create `tests/test_rpa_variations_ranks.py`

---

### Task 1: Variation catalog + checklist (seed data)

**Files:**
- Create: `config/rpa_variations/checklist.md`
- Create: `config/rpa_variations/steps.yaml`
- Create: `config/rpa_variations/strategy_ranks.json`
- Test: `tests/test_rpa_variations_catalog.py` (written in Task 2; this task is data-only)

**Interfaces:**
- Produces: YAML steps with ids used by ranks: `open_campaigns`, `create_manual`, `objective_leads`, `campaign_name_budget`, `nav_ad_set`, `conversion_location_website`, `conversion_event_lead`, `audience_targeting`, `ad_creative_copy`, `finish_draft`

- [ ] **Step 1: Write checklist.md**

Create `config/rpa_variations/checklist.md` with the approved 10-step table (Draft-only). Include note: Publish is out of scope.

- [ ] **Step 2: Write steps.yaml (thin full path)**

Create `config/rpa_variations/steps.yaml`:

```yaml
version: 1
steps:
  open_campaigns:
    left_tab: null
    section: campaigns_list
    risk: safe
    must: ["Ads Manager ready", "Create visible"]
    shot: "01_campaigns"
    ui_variants:
      - id: loading_overlay
        notes: "Wait for overlay to clear before Create"
    strategies: ["wait_ready", "goto_act_url"]
  create_manual:
    left_tab: null
    section: create_wizard
    risk: safe
    must: ["Manual setup selected", "not stuck in AI-guided"]
    shot: "02_*"
    ui_variants:
      - id: ai_guided_dialog
      - id: legacy_create_menu
    strategies: ["dismiss_ai", "click_manual", "click_create"]
  objective_leads:
    left_tab: null
    section: objective
    risk: safe
    must: ["Leads selected", "Campaign name field visible"]
    shot: "03_wizard_started"
    ui_variants:
      - id: en_es_labels
    strategies: ["select_leads", "continue_when_enabled"]
  campaign_name_budget:
    left_tab: campaign
    section: details
    risk: safe
    must: ["campaign name filled"]
    shot: "04_campaign_level"
    strategies: ["fill_campaign_name", "fill_daily_budget"]
  nav_ad_set:
    left_tab: ad_set
    section: navigation
    risk: safe
    must: ["Ad set level visible"]
    shot: "04b_*"
    ui_variants:
      - id: sidebar_tree
      - id: breadcrumb
    strategies: ["sidebar_ad_set", "breadcrumb_ad_set", "next_step"]
  conversion_location_website:
    left_tab: ad_set
    section: conversion
    risk: risky
    must: ["conversion_location == Website"]
    shot: "04d_*"
    ui_variants:
      - id: hybrid_default
        notes: "Website and instant forms selected by default"
      - id: en_vs_es
    strategies: ["recipe_space", "keyboard_space_enter", "cdp_mouse", "playwright_click", "teach_mode"]
  conversion_event_lead:
    left_tab: ad_set
    section: conversion
    risk: risky
    must: ["conversion_event == Lead"]
    shot: "04e_*"
    strategies: ["select_lead_event", "diagnose_recover"]
  audience_targeting:
    left_tab: ad_set
    section: audience
    risk: safe
    must: ["geo/age set", "targeting if conversion ok"]
    shot: "05_*"
    strategies: ["fill_location", "fill_age", "paste_targeting", "manual_placements"]
  ad_creative_copy:
    left_tab: ad
    section: ad_setup
    risk: safe
    must: ["creative uploaded", "primary text filled"]
    shot: "ad_*"
    strategies: ["upload_creative", "fill_ad_copy"]
  finish_draft:
    left_tab: null
    section: finish
    risk: safe
    must: ["left as Draft", "Publish not clicked"]
    shot: "99_draft"
    strategies: ["hand_off_draft"]
```

- [ ] **Step 3: Seed empty ranks file**

Create `config/rpa_variations/strategy_ranks.json`:

```json
{
  "version": 1,
  "steps": {},
  "pending_approval": []
}
```

- [ ] **Step 4: Commit**

```bash
git add config/rpa_variations/
git commit -m "feat(rpa): seed variation catalog and Draft-only checklist"
```

---

### Task 2: Catalog loader (TDD)

**Files:**
- Create: `src/fb_ads_rpa/variations/paths.py`
- Create: `src/fb_ads_rpa/variations/catalog.py`
- Create: `src/fb_ads_rpa/variations/__init__.py`
- Test: `tests/test_rpa_variations_catalog.py`

**Interfaces:**
- Produces:
  - `get_variations_dir() -> Path`
  - `load_steps_catalog(path: Path | None = None) -> dict[str, Any]`
  - `step_risk(step_id: str, catalog: dict | None = None) -> str`  # `"safe"` | `"risky"`
  - `default_strategies(step_id: str, catalog: dict | None = None) -> list[str]`

- [ ] **Step 1: Write failing tests**

Create `tests/test_rpa_variations_catalog.py`:

```python
from pathlib import Path

from src.fb_ads_rpa.variations.catalog import (
    default_strategies,
    load_steps_catalog,
    step_risk,
)


def test_load_repo_catalog_has_conversion_risky():
    cat = load_steps_catalog()
    assert "conversion_location_website" in cat["steps"]
    assert step_risk("conversion_location_website", cat) == "risky"
    assert step_risk("finish_draft", cat) == "safe"
    assert "Publish" not in str(cat).lower() or "not clicked" in str(cat["steps"]["finish_draft"]).lower()


def test_default_strategies_order():
    cat = load_steps_catalog()
    strats = default_strategies("conversion_location_website", cat)
    assert strats[0] == "recipe_space"
    assert "teach_mode" in strats
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/test_rpa_variations_catalog.py -v`  
Expected: FAIL (module not found)

- [ ] **Step 3: Implement paths + catalog**

`src/fb_ads_rpa/variations/paths.py`:

```python
from __future__ import annotations
from pathlib import Path

def repo_root() -> Path:
    # src/fb_ads_rpa/variations/paths.py -> parents[3] == repo root
    return Path(__file__).resolve().parents[3]

def get_variations_dir() -> Path:
    return repo_root() / "config" / "rpa_variations"

def steps_yaml_path() -> Path:
    return get_variations_dir() / "steps.yaml"

def ranks_json_path() -> Path:
    return get_variations_dir() / "strategy_ranks.json"
```

`src/fb_ads_rpa/variations/catalog.py`: load YAML with `yaml.safe_load`; raise clear error if missing step; `step_risk` defaults to `"safe"` if unknown (and log warning).

`__init__.py` exports: `load_steps_catalog`, `step_risk`, `default_strategies`, `StrategyRanks` (added in Task 3).

- [ ] **Step 4: Run tests — expect PASS**

Run: `pytest tests/test_rpa_variations_catalog.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/fb_ads_rpa/variations/ tests/test_rpa_variations_catalog.py
git commit -m "feat(rpa): load variation steps catalog"
```

---

### Task 3: Strategy ranks + hybrid learning (TDD)

**Files:**
- Create: `src/fb_ads_rpa/variations/ranks.py`
- Modify: `src/fb_ads_rpa/variations/__init__.py`
- Test: `tests/test_rpa_variations_ranks.py`

**Interfaces:**
- Produces class `StrategyRanks`:
  - `StrategyRanks.load(path: Path | None = None) -> StrategyRanks`
  - `ordered_strategies(step_id: str, fallback: list[str]) -> list[str]`
  - `log_outcome(step_id: str, strategy_id: str, *, ok: bool, screenshot: str | None = None, browser_crash: bool = False) -> None`
  - `mark_next_step_ok(step_id: str) -> None`  # hybrid gate: previous pending safe success may promote
  - `approve_pending(step_id: str, strategy_id: str) -> bool`
  - `save() -> None`
- Consumes: `step_risk` from catalog; never promotes when `browser_crash=True`; never accepts `strategy_id` containing `publish` (ignore / raise ValueError).

- [ ] **Step 1: Write failing tests**

Create `tests/test_rpa_variations_ranks.py`:

```python
import json
from pathlib import Path

from src.fb_ads_rpa.variations.ranks import StrategyRanks


def test_safe_success_without_next_step_does_not_promote(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(json.dumps({"version": 1, "steps": {}, "pending_approval": []}), encoding="utf-8")
    ranks = StrategyRanks.load(path)
    ranks.log_outcome("campaign_name_budget", "fill_campaign_name", ok=True)
    ranks.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    # logged but order not yet promoted to top until gate
    assert any(e["strategy_id"] == "fill_campaign_name" for e in data.get("outcome_log", [])[-5:])
    order = ranks.ordered_strategies("campaign_name_budget", ["fill_daily_budget", "fill_campaign_name"])
    # without mark_next_step_ok, keep fallback order
    assert order == ["fill_daily_budget", "fill_campaign_name"]


def test_safe_promote_after_next_step_gate(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(json.dumps({"version": 1, "steps": {}, "pending_approval": []}), encoding="utf-8")
    ranks = StrategyRanks.load(path)
    ranks.log_outcome("nav_ad_set", "sidebar_ad_set", ok=True)
    ranks.mark_next_step_ok("nav_ad_set")  # hybrid gate passed
    ranks.save()
    order = ranks.ordered_strategies("nav_ad_set", ["breadcrumb_ad_set", "sidebar_ad_set"])
    assert order[0] == "sidebar_ad_set"


def test_risky_success_goes_pending_not_promoted(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(json.dumps({"version": 1, "steps": {}, "pending_approval": []}), encoding="utf-8")
    ranks = StrategyRanks.load(path)
    ranks.log_outcome("conversion_location_website", "keyboard_space_enter", ok=True)
    ranks.mark_next_step_ok("conversion_location_website")
    ranks.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert any(p["strategy_id"] == "keyboard_space_enter" for p in data["pending_approval"])
    order = ranks.ordered_strategies(
        "conversion_location_website",
        ["recipe_space", "keyboard_space_enter"],
    )
    assert order[0] == "recipe_space"  # unchanged until approve


def test_approve_pending_promotes_risky(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(json.dumps({"version": 1, "steps": {}, "pending_approval": []}), encoding="utf-8")
    ranks = StrategyRanks.load(path)
    ranks.log_outcome("conversion_location_website", "keyboard_space_enter", ok=True)
    assert ranks.approve_pending("conversion_location_website", "keyboard_space_enter")
    ranks.save()
    order = ranks.ordered_strategies(
        "conversion_location_website",
        ["recipe_space", "keyboard_space_enter"],
    )
    assert order[0] == "keyboard_space_enter"


def test_browser_crash_does_not_learn(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(json.dumps({"version": 1, "steps": {}, "pending_approval": []}), encoding="utf-8")
    ranks = StrategyRanks.load(path)
    ranks.log_outcome("nav_ad_set", "sidebar_ad_set", ok=True, browser_crash=True)
    ranks.mark_next_step_ok("nav_ad_set")
    order = ranks.ordered_strategies("nav_ad_set", ["breadcrumb_ad_set", "sidebar_ad_set"])
    assert order[0] == "breadcrumb_ad_set"


def test_reject_publish_strategy(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(json.dumps({"version": 1, "steps": {}, "pending_approval": []}), encoding="utf-8")
    ranks = StrategyRanks.load(path)
    try:
        ranks.log_outcome("finish_draft", "click_publish", ok=True)
        raised = False
    except ValueError:
        raised = True
    assert raised
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/test_rpa_variations_ranks.py -v`  
Expected: FAIL (import error)

- [ ] **Step 3: Implement StrategyRanks**

Implement in `ranks.py`:
- Persist `outcome_log` (cap last 500 entries), `steps[step_id].strategies[{id, wins, fails, score}]`, `pending_safe` (awaiting gate), `pending_approval`.
- `ordered_strategies`: sort known strategies by score desc, then append any fallback ids not yet scored.
- Safe fail: increment fails, lower score.
- On `log_outcome(ok=True)` for safe: stash in `pending_safe[step_id]`; promote only in `mark_next_step_ok`.
- Risky ok: append to `pending_approval` (dedupe by step+strategy).
- `approve_pending`: move to top score, remove from pending.
- Reject strategy ids matching `(?i)publish`.

- [ ] **Step 4: Run tests — expect PASS**

Run: `pytest tests/test_rpa_variations_ranks.py tests/test_rpa_variations_catalog.py -v`  
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/fb_ads_rpa/variations/ tests/test_rpa_variations_ranks.py
git commit -m "feat(rpa): hybrid strategy ranks learner (Draft-only)"
```

---

### Task 4: Wire ranks into AdsManagerFlow (minimal hooks)

**Files:**
- Modify: `src/fb_ads_rpa/ads_manager_flow.py`
- Modify: `src/fb_ads_rpa/meta_dom.py` (only if Website pick needs strategy id callbacks — prefer thin wrapper in flow)

**Interfaces:**
- Consumes: `StrategyRanks.load()`, `default_strategies()`, `ordered_strategies()`, `log_outcome()`, `mark_next_step_ok()`
- Produces: flow still Draft-only via `_finish_publish`; step ids match catalog

- [ ] **Step 1: Add helper on AdsManagerFlow**

Near top of class / module in `ads_manager_flow.py`:

```python
def _variations_ranks(self) -> "StrategyRanks | None":
    try:
        from src.fb_ads_rpa.variations.ranks import StrategyRanks
        return StrategyRanks.load()
    except Exception:
        return None
```

- [ ] **Step 2: Hook conversion Website**

In `_select_conversion_location_website`:
1. Load ranks + `ordered_strategies("conversion_location_website", default_strategies(...))`.
2. After success/fail of the existing `select_conversion_location_website` / teach path, call `log_outcome(...)` with the strategy that worked (map recipe/keyboard/cdp from `steps_completed` / return metadata if available; if unknown use `"playwright_click"`).
3. Do **not** auto-promote (risky).
4. On success of a later major step (`audience_targeting` start or `nav` confirmed), call `mark_next_step_ok` only for **safe** prior steps — for conversion, gate is irrelevant for promote (still pending).

Keep existing multi-strategy logic in `meta_dom`; ranks only reorder which family to try first if/when flow already loops strategies. If current code is a single combined function, log one outcome id inferred from `result.steps_completed` (e.g. contains `recipe` → `recipe_space`).

- [ ] **Step 3: Hook one safe step + finish_draft**

- After successful `_ensure_ad_set_level` / nav: `log_outcome("nav_ad_set", strategy, ok=True)` then when conversion section starts successfully, `mark_next_step_ok("nav_ad_set")`.
- In `_finish_publish`: if Draft/dry_run path taken, `log_outcome("finish_draft", "hand_off_draft", ok=True)` and `mark_next_step_ok("finish_draft")` is N/A (terminal). Never log publish.

- [ ] **Step 4: Persist ranks after campaign**

At end of `publish_campaign` `finally`/success path: `ranks.save()` if ranks object exists.

- [ ] **Step 5: Manual sanity (no full browser required)**

Run: `pytest tests/test_rpa_variations_ranks.py tests/test_rpa_variations_catalog.py tests/test_fb_ads_rpa_browser_profile.py -v`  
Expected: PASS  
Import check: `python -c "from src.fb_ads_rpa.ads_manager_flow import AdsManagerFlow; print('ok')"`

- [ ] **Step 6: Commit**

```bash
git add src/fb_ads_rpa/ads_manager_flow.py src/fb_ads_rpa/meta_dom.py
git commit -m "feat(rpa): log variation outcomes from AdsManagerFlow"
```

---

### Task 5: Approve risky pending (teach / API)

**Files:**
- Modify: `src/web/app.py` — add approve endpoint
- Modify: `src/fb_ads_rpa/variations/ranks.py` if needed for list_pending helper
- Test: extend `tests/test_rpa_variations_ranks.py`

**Interfaces:**
- Produces: `StrategyRanks.list_pending() -> list[dict]`
- HTTP: `POST /rpa-teach/approve-strategy` JSON `{"step_id": "...", "strategy_id": "..."}` → `{ok: true}`

- [ ] **Step 1: Test list_pending + approve already covered; add list test**

```python
def test_list_pending(tmp_path: Path):
    path = tmp_path / "ranks.json"
    path.write_text(json.dumps({"version": 1, "steps": {}, "pending_approval": []}), encoding="utf-8")
    ranks = StrategyRanks.load(path)
    ranks.log_outcome("conversion_event_lead", "select_lead_event", ok=True)
    pending = ranks.list_pending()
    assert pending[0]["step_id"] == "conversion_event_lead"
```

- [ ] **Step 2: Implement `list_pending` + endpoint**

```python
@app.post("/rpa-teach/approve-strategy")
async def rpa_teach_approve_strategy(request: Request) -> JSONResponse:
    body = await request.json()
    step_id = str(body.get("step_id") or "")
    strategy_id = str(body.get("strategy_id") or "")
    from src.fb_ads_rpa.variations.ranks import StrategyRanks
    ranks = StrategyRanks.load()
    ok = ranks.approve_pending(step_id, strategy_id)
    if ok:
        ranks.save()
    return JSONResponse({"ok": ok})
```

Also `GET /rpa-teach/pending-strategies` returning `list_pending()`.

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_rpa_variations_ranks.py -v`  
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/web/app.py src/fb_ads_rpa/variations/ranks.py tests/test_rpa_variations_ranks.py
git commit -m "feat(rpa): approve risky strategy ranks via teach API"
```

---

### Task 6: Agent 10 skill + `/rpa-audit` + docs

**Files:**
- Create: `.cursor/skills/agent10-rpa-variations/SKILL.md`
- Create: `.cursor/commands/rpa-audit.md`
- Modify: `docs/agent10-fb-ads-rpa.md`

**Interfaces:**
- Produces: skill auto-description triggers; slash runs full audit using checklist + screenshots + ranks

- [ ] **Step 1: Write SKILL.md**

Frontmatter `name: agent10-rpa-variations`  
`description:` third person, include triggers: RPA publish, Ads Manager selectors, teach-mode, Multilogin, conversion Website, rpa_screenshots, variation catalog, `/rpa-audit`.  
**Do not** set `disable-model-invocation: true` (must auto-load on RPA topics).

Body must include:
1. Ground-truth Draft-only recipe  
2. Left tabs = Campaign / Ad set / Ad + sections  
3. Hybrid C rules + risky list (conversion only)  
4. Paths to `config/rpa_variations/`, design spec, screenshots  
5. Multilogin-first browser rule  
6. Full EN + short RU task prompts from the design spec  

- [ ] **Step 2: Write `.cursor/commands/rpa-audit.md`**

Mirror jenya command style: instruct agent to read skill, latest `output/rpa_screenshots`, `strategy_ranks.json`, job errors; walk checklist; report first broken step; propose minimal fix; show diffs before editing ranks/catalog; never Publish.

- [ ] **Step 3: Update `docs/agent10-fb-ads-rpa.md`**

Add short section “Variations learning” linking checklist, steps.yaml, ranks, skill, `/rpa-audit`.

- [ ] **Step 4: Commit**

```bash
git add .cursor/skills/agent10-rpa-variations/SKILL.md .cursor/commands/rpa-audit.md docs/agent10-fb-ads-rpa.md
git commit -m "feat(rpa): Agent 10 variations skill and /rpa-audit command"
```

---

### Task 7: Verification + push

**Files:** none new

- [ ] **Step 1: Run full related tests**

```bash
pytest tests/test_rpa_variations_catalog.py tests/test_rpa_variations_ranks.py tests/test_fb_ads_rpa_browser_profile.py tests/test_fb_ads_rpa_draft.py -v
```

Expected: all PASS

- [ ] **Step 2: Confirm finish_draft forbids publish in catalog**

```bash
python -c "from src.fb_ads_rpa.variations.catalog import load_steps_catalog; c=load_steps_catalog(); assert c['steps']['finish_draft']['must'][-1].lower().find('publish')>=0 or 'Draft' in str(c['steps']['finish_draft'])"
```

- [ ] **Step 3: Push branch**

```bash
git push -u origin HEAD
```

- [ ] **Step 4: Update PR description** only if one already tracks this branch — mention variations learner + Draft-only.

---

## Spec coverage (self-review)

| Spec item | Task |
|-----------|------|
| Ground-truth checklist | Task 1 |
| Thin full-path variation catalog | Task 1–2 |
| Ranked store + hybrid C | Task 3 |
| Risky = conversion only; no Publish | Task 1, 3, 4 |
| Wire into flow | Task 4 |
| Teach/approve risky | Task 5 |
| Skill auto + slash + prompts | Task 6 |
| Tests | Tasks 2–3, 5, 7 |
| Live Upload FB golden | Out of scope (later) |
| Deep conversion phase 2 | Out of scope (later) |

No TBD placeholders. Types consistent: `StrategyRanks`, step ids shared across catalog/ranks/flow/API.
