# Product

<!-- impeccable:product-schema 1 -->

Recorded from the maintainer's brief and `docs/SPEC.md` without a live
interview (the build ran unattended, with no answer mechanism). Facts marked
*(inferred)* come from the spec's intent rather than an explicit statement.

## Platform

web

## Users

One maintainer who runs many agent-built projects created with forge on one
Windows workstation, often with several coding agents working at once. They
open the dashboard between agent sessions to see which projects need them,
then act: launch a project, run gates, sync a fork, bring a repo up to forge's
standards, triage issues, or delegate work to an agent. They glance at it on a
phone occasionally, read-only *(inferred from "readable on a phone for a quick
glance")*.

## Product Purpose

A local control room for every forge project. One glance answers: which
projects are healthy and which moved recently, what is running and where,
what is failing, how close each project is to V1, and what needs attention.
One click does the routine management. Success is the maintainer never
having to open a terminal to learn a project's state or to perform a routine
action, and never being surprised by automation touching their working tree.

## Positioning

It reads each project's own self-description (`forge.project.toml`) and
forge's standards live from the forge repo, so it measures every project
against the same tightening bar the projects were built under, and it acts on
projects only inside isolated worktrees. A generic repo dashboard cannot do
either.

## Operating Context

- Launched from a TUI runner (`run.ps1`) that supervises the API and opens the
  browser; it runs on loopback only.
- Projects live anywhere under the home directory; discovery finds them. On
  this machine today: Reelcut (being built concurrently by another agent),
  the dashboard itself, and the forge repo as the standards source.
- None of the repos has a GitHub remote yet; "not on GitHub" is a normal state.
- Agents (`claude -p`) run fork-conflict resolution, bring-ups, adoption,
  issue delegation, and dependency updates on dashboard-owned branches.
- Ports 8000 and 8317 belong to other software.

## Capabilities and Constraints

- Overview of all projects as a card grid or a dense table, built from the
  `ProjectSummary` schema; search, filters, sort, grouping, saved views,
  favorites, keyboard navigation, a command palette, density, light and dark.
- Project page: launch and interface, quality (checks, gates, coverage, CI),
  issues and PRs, git, fork, standards, metrics, actions, agent sessions,
  activity, notes, settings.
- Every form, action dialog, and state panel is generated from service JSON
  Schemas with `x-ui` hints; adding a backend field must surface in the UI
  without frontend edits.
- Status is never encoded by color alone. Designed empty, loading, error, and
  degraded states everywhere (no projects yet, scan in progress, GitHub
  unauthenticated, forge repo not found, invalid manifest, harness missing).
- Stack is fixed by the spec: React 19, TypeScript, Bun, Tailwind v4,
  shadcn/ui as the component base.

## Brand Commitments

- Name: "forge dashboard" (lowercase in running text, as the spec writes it).
- The surface mode is Operate. The maintainer's density bar: Linear and
  Vercel's dashboard, without Grafana's clutter. Dense, calm, precise, with a
  visual world of its own.

## Evidence on Hand

Real data only: the projects on this machine, their manifests, git state,
check results, metrics, and forge's own repository. No testimonials,
customers, or usage figures exist and none may be invented.

## Product Principles

1. Glance first: the overview answers the five questions without a click.
2. Never surprise a working tree: automation acts in worktrees; the user's
   checkout changes only by explicit action or policy, and only by
   fast-forward.
3. Self-description over special cases: what a project declares is what the
   dashboard shows; nothing is assumed.
4. Calm under many projects: attention is reserved for what needs it.
5. Keyboard-first and honest about freshness: every relative time ticks from
   one clock, and stale data says so.

## Accessibility & Inclusion

Status always pairs a word or glyph with color; keyboard operation for every
action; WCAG AA contrast in both themes *(inferred from "status never encoded
by color alone" and "keyboard-first")*.
