---
version: 1
slug: "web-src-app-app-tsx"
primary_target: "web/src/app/App.tsx"
related_targets: ["web/src/features"]
---

## Scope

The whole dashboard WebUI: overview (card grid and dense table), project page,
fork, standards, jobs, machine and services, forge runs, activity, settings.
Visitor mode: Operate. Audience, job, and constraints live in PRODUCT.md.

## Decision record

The run was unattended (no answer mechanism). The direction round was decided
from the spec: the assigned direction was built as dealt, with the dealt
challengers weighed and their verdicts kept below. No comp exists (no image
generation); the build is code-led.

Challenger verdicts:
- Monochrome product marketing canon: competitive on audience identification
  (the maintainer named Linear and Vercel), loses product clarity of the
  attention model. Kept: every rollup number opens its own evidence.
- Timetable slide rack: declined. Kept: rank carried by weight, case, and
  reversal; the overview uses two text sizes plus the numeral size.
- Crouwel grid specimen: declined. Kept: one visible module; the grid repacks
  in whole cards, never fractional ones.
- Jet-age ticket wallet: declined. Kept: nothing disappears, it cancels;
  aborted runs and cancelled jobs stay listed, struck through and marked.
- Doujin event catalog: declined. Kept: every project has a short code (its id)
  used as its address everywhere, including the command palette.
- Ikeda datamatics: declined. Kept: data marks are the only graphics; no
  illustration, every sparkline and meter is drawn at hairline precision in ink.

## Direction contract

THESIS: The dashboard is a station's departure hall for a fleet of agent-built
projects: what leaves next, what is boarding now, what is delayed. It refuses
the grey card wall with one accent and a chart in every tile.

OWN-WORLD: Swiss railway information design. A dark board (deep blue-black)
owns the rail and the departures strip in both themes; the working ground is
paper white in light and near-black in dark. Signal red is reserved for delays
and failures plus the ticking second disc; amber for warnings; green for clear;
every state also reads as a word or glyph. One grotesk family with a width
axis: normal width for interface text, condensed tabular numerals for times,
counts, and columns. Hairline row rules, a heavier rule under headers, square
corners on data, small radius on controls only.

STORY: The maintainer opens the hall, reads the board (running, next, delayed),
scans the timetable of projects by health and last change, opens one, and acts
with one click; anything automation did waits on its own branch.

FIRST VIEWPORT: Left, a 208 px dark rail with the wordmark and sections.
Top of the work area, the departures strip (about 132 px): a live clock with the
red second disc, four condensed counters (projects, failing, services up, need
attention), next scheduled departures, and jobs boarding now. Below it the
toolbar (search, filter chips, sort, group, grid or table, density), then the
project grid. Primary actions sit on each card's footer and the page header.

FORM: Candidate 4 of 7 (Swiss railway timetable and station information
design). Signature move: the departures strip, and every relative time set as a
timetable time in condensed tabular numerals, with lateness shown the way a
board shows delay (red "+n" marks). seed:8ca8e1c954fad9ff

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Unresolved

- Project page uses tabs (routes per tab) rather than one long scroll, because
  sections such as diffs, issues, and CI runs load on demand.
