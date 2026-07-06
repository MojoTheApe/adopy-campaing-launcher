# Product

## Register

product

## Users

Campaign managers (CMs) running Facebook acquisition, starting with Evgeni Sedin. They open the dashboard each morning with fresh Excel stats and GSheet account statuses. Context: time-pressed, scanning many countries, deciding what to scale, pause, or launch.

## Product Purpose

Morning Report dashboard for fb_agents: surface FTD progress vs goals, country performance, account health, optimization candidates, and campaign builder actions. Success = CM finds the right country and section in under 10 seconds and trusts the numbers.

## Brand Personality

Terminal-native, data-first, calm urgency. Three words: **dense, legible, honest**. Not a marketing site; a working instrument.

## Anti-references

- Generic SaaS landing aesthetics (purple gradients, hero metrics, glass cards)
- Card soup and nested cards for every table row
- Decorative motion and bounce easing
- Inter/system-ui re-skin that hides the data
- Purple/blue gradient accents and gradient text
- Side-stripe accent borders on content panels

## Design Principles

1. **Numbers first** — hierarchy serves scan speed, not decoration.
2. **One task per view** — sidebar selects a single country section; no infinite scroll of all geos.
3. **Honest empty states** — when GSheet or Excel is missing, say why and how to fix (Refresh), not blank silence.
4. **Consistent shell** — same nav, tokens, and table patterns across all countries.
5. **Preserve terminal identity** — monospace, dark shell, tabular nums; do not "prettify" into generic admin UI.

## Accessibility & Inclusion

- WCAG AA contrast for body text and table data on dark backgrounds.
- Keyboard focus visible on sidebar controls and interactive rows.
- `prefers-reduced-motion`: disable non-essential transitions.
- Touch targets ≥ 44px for primary actions (Refresh, sidebar country buttons).
