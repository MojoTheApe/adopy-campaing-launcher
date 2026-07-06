---
name: fb_agents Morning Dashboard
description: Dark terminal-style CM morning report with sidebar navigation
colors:
  bg: "#0e1116"
  bg-elev: "#161b22"
  bg-hover: "#1f2630"
  border: "#2a3140"
  text: "#d8dde3"
  muted: "#8b949e"
  accent: "#58a6ff"
  good: "#3fb950"
  bad: "#f85149"
  warn: "#f0883e"
typography:
  body:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
  title:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "1.2rem"
    fontWeight: 400
    lineHeight: 1.3
  label:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "0.65rem"
    fontWeight: 400
    letterSpacing: "0.06em"
rounded:
  sm: "4px"
  md: "5px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  button-secondary:
    backgroundColor: "{colors.bg-elev}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "6px 14px"
  nav-link-active:
    backgroundColor: "{colors.bg-hover}"
    textColor: "{colors.accent}"
---

# Design System: fb_agents Morning Dashboard

## 1. Overview

**Creative North Star: "The Night Shift Console"**

A single CM's control room after the overnight stats land. Dark, monospace, high information density. Color is semantic (good/bad/warn/accent), not decorative. Layout is a fixed sidebar plus one active content pane — like an IDE, not a brochure.

**Key Characteristics:**

- Dark neutral shell (`#0e1116`) with elevated panels (`#161b22`)
- Monospace everywhere; tabular numerals for metrics
- Sidebar navigation: Home → countries → sections
- Tables as primary content blocks, not cards
- Blue accent (`#58a6ff`) for selection and links only

## 2. Colors

### Primary
- **Signal Blue** (`#58a6ff`): active nav, links, country headings, focus rings. Never for large fills.

### Neutral
- **Console Black** (`#0e1116`): page background
- **Panel Gray** (`#161b22`): tables, sidebar, elevated surfaces
- **Hover Gray** (`#1f2630`): row hover, active nav background
- **Ink** (`#d8dde3`): primary text
- **Muted Label** (`#8b949e`): meta, column headers, hints

### Semantic
- **Good Green** (`#3fb950`): on-target metrics
- **Bad Red** (`#f85149`): misses, errors, optimization flags
- **Warn Amber** (`#f0883e`): warnings banner

**The Restrained Accent Rule.** Accent color on ≤10% of any screen. Status colors only on numbers and badges, not backgrounds.

## 3. Typography

**Body / UI Font:** ui-monospace, SFMono-Regular, Menlo, monospace

**Character:** Terminal legibility over personality. Fixed rem scale; no fluid display type.

### Hierarchy
- **Page title** (1.2rem): report date in header
- **View title** (1.2rem, accent): active section heading (`Country: Section`)
- **Section label** (0.65rem uppercase, muted): sidebar groups
- **Body** (13px): tables and forms
- **Table header** (0.78rem uppercase, muted)

**The Tabular Rule.** All numeric columns use `font-variant-numeric: tabular-nums`.

## 4. Elevation

Flat-by-default. Depth via background step (`bg` → `bg-elev` → `bg-hover`) and 1px borders (`#2a3140`). No drop shadows on tables or panels.

## 5. Components

### Sidebar navigation
- **Width:** 240px sticky
- **Active state:** background `bg-hover`, text accent (no thick side stripe on content panels)
- **Country expand:** nested links indented 1.5rem
- **Focus:** `:focus-visible` outline 2px accent

### Tables
- Full width, `bg-elev`, sticky header row
- Drillable rows: cursor pointer + hover `bg-hover`
- `.me` row highlighted in good green

### Goals panel
- Full-width summary block on Home
- Per-country goals: tinted panel with border (no side-tab stripe)

### Buttons
- Secondary style: `bg-elev`, 1px border, 4px radius
- Hover: `bg-hover`, accent border

## 6. Do's and Don'ts

### Do:
- **Do** use colon separators in view titles (`Mexico: CM Comparison`).
- **Do** keep one active view in the main pane.
- **Do** show explicit empty states for missing GSheet data.

### Don't:
- **Don't** use purple gradients, glassmorphism, or gradient text (PRODUCT anti-references).
- **Don't** use `border-left` > 1px as accent on panels (side-tab tell).
- **Don't** animate `width` on progress bars; use `transform: scaleX`.
- **Don't** replace monospace with Inter for "cleaner" UI.
- **Don't** nest cards inside cards for table data.
