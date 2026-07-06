---
name: fb_agents Morning Dashboard
description: Light, concise CM morning report with sidebar navigation
colors:
  bg: "#f4f5f7"
  bg-elev: "#ffffff"
  bg-hover: "#eef0f4"
  bg-subtle: "#e8ebf0"
  border: "#dfe3ea"
  text: "#1a1d23"
  muted: "#647084"
  accent: "#2563eb"
  accent-soft: "#eff4ff"
  good: "#059669"
  bad: "#dc2626"
  warn: "#d97706"
typography:
  body:
    fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.55
  title:
    fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.3
  label:
    fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
    fontSize: "0.75rem"
    fontWeight: 600
rounded:
  sm: "6px"
  md: "8px"
  lg: "10px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  nav-link-active:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.accent}"
---

# Design System: fb_agents Morning Dashboard

## 1. Overview

**Creative North Star: "Calm Workbench"**

A CM opens the dashboard each morning to scan numbers and act — not to admire chrome. Light surfaces, clear hierarchy, sans-serif UI type. Data stays dense but readable; color is semantic, not decorative.

**Key Characteristics:**

- Soft gray page shell (`#f4f5f7`) with white elevated panels
- System sans-serif for UI; monospace only for code blocks and IDs
- Sidebar navigation: Home → countries → sections
- Tables in bordered cards with subtle shadow
- Blue accent (`#2563eb`) for links, active nav, primary actions

## 2. Colors

### Primary
- **Signal Blue** (`#2563eb`): active nav, links, primary buttons, focus rings

### Neutral
- **Page Gray** (`#f4f5f7`): page background
- **Surface White** (`#ffffff`): tables, sidebar, panels
- **Hover Gray** (`#eef0f4`): row hover, inactive hover states
- **Ink** (`#1a1d23`): primary text
- **Muted** (`#647084`): meta, column headers, hints

### Semantic
- **Good Green** (`#059669`): on-target metrics
- **Bad Red** (`#dc2626`): misses, errors
- **Warn Amber** (`#d97706`): warnings banner

**Restrained accent rule.** Accent on ≤10% of screen. Status colors on values and badges, not large fills.

## 3. Typography

**UI font:** system sans-serif stack

**Numeric columns:** `font-variant-numeric: tabular-nums` (same font, not monospace body)

### Hierarchy
- **Page title** (1.125rem, semibold): report date in header
- **View title** (1.125rem, semibold, ink): section heading
- **Section label** (0.75rem, semibold, muted): sidebar groups, metric labels
- **Body** (14px): tables and forms
- **Table header** (0.75rem, semibold, muted) — sentence case, not ALL CAPS

## 4. Elevation

Depth via white surfaces + 1px borders + `--shadow-sm` on cards/tables. No heavy drop shadows. No glassmorphism.

## 5. Motion (Emil Kowalski)

- **Sidebar / view switch:** no animation (high frequency)
- **Buttons:** 150ms ease-out background; `:active` `scale(0.97)`
- **Progress bars:** `transform: scaleX`, not width
- **`prefers-reduced-motion`:** disable transitions

Custom easing: `cubic-bezier(0.23, 1, 0.32, 1)`

## 6. Components

### Sidebar
- Width 248px, sticky, white background
- Active: `accent-soft` fill + accent text (no inset stripe)
- Nested section links indented 1.25rem

### Tables
- White card, 8px radius, light border, sticky header on `--bg`

### Goals panel
- White card, rounded progress tracks (`--bg-subtle`)

### Buttons
- Primary: accent fill, white label
- Secondary: white/elevated with border

## 7. Do's and Don'ts

### Do:
- Colon separators in view titles (`Mexico: CM Comparison`)
- One active view in main pane
- Explicit empty states for missing GSheet data
- Focus-visible rings on interactive controls

### Don't:
- Terminal monospace as default UI font
- ALL CAPS label stacks
- Inset side-stripe active nav (IDE/terminal tell)
- Decorative page-load animations
- Purple gradients or gradient text
