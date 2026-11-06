---
name: forge dashboard
description: A departure board for every project forge created
colors:
  paper: "#fafaf9"
  surface: "#ffffff"
  surface-sunk: "#f7f8f9"
  ink: "#10131a"
  ink-2: "#444b59"
  ink-3: "#636a78"
  rule: "#dde0e6"
  rule-strong: "#b9bec8"
  hover: "#eceef2"
  select: "#e3ebfb"
  board: "#101a2f"
  board-2: "#19253f"
  board-3: "#24324f"
  board-ink: "#f3f5f9"
  board-ink-2: "#a9b4ca"
  board-rule: "#2a3755"
  board-signal: "#ff5c66"
  board-amber: "#f5b82e"
  signal: "#d40011"
  signal-ink: "#b5000e"
  signal-wash: "#fde8ea"
  amber: "#f0a500"
  amber-glyph: "#b07600"
  amber-ink: "#855700"
  amber-wash: "#fdf2d8"
  clear: "#0a8a43"
  clear-ink: "#06703a"
  clear-wash: "#e2f4ea"
  info: "#1f56c7"
  info-ink: "#1a49aa"
  info-wash: "#e5ecfb"
  idle: "#8b919e"
  night-paper: "#0b0d11"
  night-surface: "#13161c"
  night-ink: "#eceef2"
typography:
  counter:
    fontFamily: "Archivo Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "1.75rem"
    fontWeight: 600
    lineHeight: "2rem"
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Archivo Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "1.375rem"
    fontWeight: 600
    lineHeight: "1.7rem"
    letterSpacing: "-0.01em"
  section:
    fontFamily: "Archivo Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "0.90625rem"
    fontWeight: 600
    lineHeight: "1.35rem"
  body:
    fontFamily: "Archivo Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "0.84375rem"
    fontWeight: 400
    lineHeight: "1.25rem"
  label:
    fontFamily: "Archivo Variable, Segoe UI, system-ui, sans-serif"
    fontSize: "0.71875rem"
    fontWeight: 400
    lineHeight: "1.05rem"
  code:
    fontFamily: "JetBrains Mono Variable, Cascadia Code, Consolas, monospace"
    fontSize: "0.78125rem"
    fontWeight: 400
    lineHeight: "1.15rem"
rounded:
  sm: "3px"
  md: "4px"
  lg: "6px"
spacing:
  hairline: "1px"
  row: "6px"
  cell: "8px"
  card: "14px"
  page: "20px"
  rail: "208px"
components:
  button-default:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface}"
    rounded: "{rounded.md}"
    height: "32px"
    padding: "0 12px"
  button-outline:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    height: "32px"
    padding: "0 12px"
  button-destructive:
    backgroundColor: "{colors.signal}"
    textColor: "{colors.surface}"
    rounded: "{rounded.md}"
    height: "32px"
  chip:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink-2}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "0 6px"
  card-project:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "{spacing.card}"
  departures-strip:
    backgroundColor: "{colors.board}"
    textColor: "{colors.board-ink}"
    padding: "12px 20px"
  rail:
    backgroundColor: "{colors.board}"
    textColor: "{colors.board-ink-2}"
    width: "{spacing.rail}"
  input-search:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    height: "32px"
---

# Design System: forge dashboard

## Overview

**Creative North Star: "The Departure Hall"**

The dashboard reads like a Swiss station's information system: a dark
departure board, the rail and the strip across the top, sits over a
paper-white working ground where the projects are listed like a timetable. One glance answers what is on time, what is late,
what is running, and what needs the maintainer, and the board is the only
place the interface raises its voice.

It is an Operate surface. Scanning dozens of projects quickly matters more
than expression, so the brand lives in precise details: the station clock's
red second disc stepping on the shared tick, condensed tabular figures for
every time and count, hairline rules instead of boxes, and status written as a
glyph and a word, never as colour alone. The board stays dark in both themes
(a shade deeper at night); the theme switch changes the ground beneath it.

**Key Characteristics:**
- A dark board (rail and departures strip) over a paper or night ground.
- Signal red is reserved for failures and lateness; everything that only waits
  for a decision is amber.
- One grotesk with a width axis; numbers condense, words do not.
- Hairline-ruled rows, small radii, no shadows on content.
- Every view is built from the backend's own schemas, so the system is the
  renderer's vocabulary, not a set of one-off screens.

## Colors

The palette is a timetable's: ink on paper, one dark board, and a small set of
signal colours that each mean exactly one thing.

### Primary
- **Board** (#101a2f, #0d1526 at night): the rail and the departures strip,
  with board ink (#f3f5f9) and its secondary (#a9b4ca) for text on it.
- **Ink** (#10131a) on **Paper** (#fafaf9) and **Surface** (#ffffff): the
  working ground. Ink-2 (#444b59) and ink-3 (#636a78) step text down.

### Secondary
- **Signal** (#d40011, ink #b5000e, wash #fde8ea): failures and lateness only.
  On the board it becomes **board signal** (#ff5c66), which reads at 5.9:1
  where the full red would read at 3.1:1.
- **Amber** (#f0a500, glyph #b07600, ink #855700, wash #fdf2d8): warnings and
  anything waiting on a decision. **Board amber** (#f5b82e) carries it on the
  board.

### Tertiary
- **Clear** (#0a8a43, ink #06703a, wash #e2f4ea): passing, running, healthy.
- **Info** (#1f56c7, ink #1a49aa, wash #e5ecfb): in progress, selection,
  focus rings, links.

### Neutral
- **Rule** (#dde0e6) and **rule strong** (#b9bec8) draw every separation.
- **Hover** (#eceef2) and **select** (#e3ebfb) mark rows under the pointer
  and the chosen row.
- **Idle** (#8b919e) is the glyph colour for neutral states.
- **Night** theme: paper #0b0d11, surface #13161c, ink #eceef2, with each
  signal colour stepped lighter (signal #ff4a52, amber #f5b82e, clear
  #34c26f, info #7aa2ff) and validated against the dark ground.

### Named Rules
- **The Red Reservation Rule.** Red means failed or late, nothing else. A parked
  conflict, an unread notification, or drift from forge is amber.
- **The Board Rule.** Red and amber text on the board always use the board
  variants; the working-ground reds are for the paper.

## Typography

**Display and body font:** Archivo Variable (self-hosted, width axis), with
Segoe UI and system-ui as fallbacks. **Code font:** JetBrains Mono Variable,
for ids, paths, hashes, and logs.

**Character:** a plain, slightly technical grotesk at normal width for words,
and the same family condensed (`font-stretch` 72%, tabular figures) for every
count and time, the way a departure board sets its numerals.

### Hierarchy
- **Counter** (600, 1.75rem / 2rem, 78% width): the departures strip's four
  numbers and the clock.
- **Title** (600, 1.375rem / 1.7rem): a project's name on its page.
- **Section** (600, 0.90625rem / 1.35rem): section headings above a heavier
  rule.
- **Body** (400, 0.84375rem / 1.25rem): tables, cards, prose.
- **Label** (400, 0.71875rem / 1.05rem): field labels, captions, chips.
- **Code** (400, 0.78125rem): ids, branches, hashes.

At 2200px and wider the root size grows from 16px to 18px, so the whole scale
grows by an eighth on a desk-distance 2560 screen.

### Named Rules
- **The Condensed Digits Rule.** `.figures` applies only to the number in
  "20 min ago"; the unit stays at normal width so it never reads as one word.
- **The Mono Id Rule.** A project's id, a branch, a hash, or a path is always set
  in the code face, so it can be read and copied exactly.

## Layout

A fixed 208px board rail on the left, the departures strip across the top
(about 80px on the overview, a 40px single line on every other page), and the
working area beneath. Content is left-aligned and capped at 100rem so tables
never stretch into gaps at 2560.

- The overview is a grid of 21rem project cards (18rem when dense), each its
  own height, or a timetable table with the same fields as columns.
- Project pages are a header (name, id, health, status, path, a one-line
  state summary, quick actions) over tabs; each tab is one concern.
- Spacing is small and regular: 6px row padding, 8px cell gaps, 14px card
  padding, 20px page gutters.
- Below the `md` breakpoint the rail becomes a sheet behind a menu button, the
  strip drops next departures and arrivals, and every grid falls to one
  shrinkable column (`minmax(0, 1fr)`) so nothing scrolls sideways.

## Elevation & Depth

**The Flat Ground Rule.** Depth is drawn with rules and fills, not shadows: a 1px rule
separates rows and cards, the heavier rule-strong underlines section headings
and table heads, and a selected card gets a 1px info ring. Cards, tables, and
the board carry no shadow. Form controls keep the component library's
hairline `shadow-xs`; popovers and menus use `shadow-md`, and dialogs and
sheets `shadow-lg`, because they genuinely sit above the page.

### Shadow Vocabulary
- **Control** (`0 1px 2px 0 rgb(0 0 0 / 0.05)`): outline buttons, inputs,
  toggles.
- **Floating** (`0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 /
  0.1)`): popovers, menus, selects.
- **Overlay** (`0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 /
  0.1)`): dialogs and sheets.

## Shapes

Small radii throughout: 3px for chips and inline controls, 4px for buttons,
inputs, and cards, 6px at most. Data marks are square except where the
dataviz rules round them: 4px rounded bar ends, 8px markers with a 2px surface
ring. Status glyphs are lucide's circle family (check, dashed, dot, octagon,
triangle), always beside their word.

## Components

### Buttons
Outline is the default (surface fill, ink text, 1px rule border, 32px tall);
the solid ink button marks the one primary action in a region (Launch,
Apply); destructive is the signal fill and always sits behind a typed
confirmation. Icon buttons are 24px squares. Every action button comes from
one action runner, so its dialog, confirmation, and job toast look the same
everywhere.

### Chips
A 1px rule border, label size, ink-2 text, 3px radius. Chips hold categorical
facts (kind, language, stage) and the lifecycle marker; a lifecycle chip turns
signal or amber only when it says "blocked" or "paused".

### Cards / Containers
The project card is a white surface with a 1px rule border and 4px radius,
laid out by the summary schema's slots: name and id with health, subtitle,
status line, chips, V1 progress, a ruled signals list, services and metrics,
attention lines, then the path, machine, agent, and quick actions. A failing
project gets a 3px signal rule on its left edge.

### Inputs / Fields
32px tall, 1px input border (rule-strong), 4px radius, info focus ring. Every
form is built from an action's input schema by the shared renderer, with
labels tied to their controls and block fields captioned by a fieldset legend.

### Navigation
The board rail: section links with lucide icons at 1.75 stroke, the current
section on board-3, and counts at the right edge (amber when something waits
on the maintainer). The theme switch is a three-way radio group at the foot.

### Departures strip
The signature component. A 32px station clock whose red second disc steps on
the shared tick, the time in counter type, four linked counters (projects,
failing, services up, need attention), the next scheduled departures as HH:MM
with a "+n" in board signal once late, and the jobs boarding now, or the
recent arrivals when nothing runs.

### Log view
Logs sit on the board colour in both themes, in the code face, with child
ANSI colours preserved, a filter, copy as plain text, and follow-the-tail.

## Do's and Don'ts

### Do:
- **Do** write every status as a glyph and a word.
- **Do** keep red for failed and late; use amber for anything waiting.
- **Do** set times and counts in condensed tabular figures, and only those.
- **Do** separate with hairline rules and alignment rather than boxes.
- **Do** add a field to the backend model with a `slot` or `columns` hint
  instead of hand-placing it in a component.

### Don't:
- **Don't** put a chart in every tile or a sparkline on a flat series.
- **Don't** use signal red for drift, parked work, or unread counts.
- **Don't** add shadows to cards or tables.
- **Don't** introduce a second display face or decorative imagery; this build
  is code-led and ships no rasters.
- **Don't** let a layout scroll sideways on a phone; give grids a shrinkable
  base column.
