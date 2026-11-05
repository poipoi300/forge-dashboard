# forge dashboard

The forge dashboard is a local control room for every project created with
forge. A TUI runner supervises its API and opens a WebUI that answers, at a
glance, which projects are healthy, what is running, what is failing, how
close each project is to V1, and what needs the maintainer's attention. One
click runs gates, launches a project, syncs a fork, brings a repo up to
forge's standards, or delegates work to an agent.

This file is the contributor instructions for coding agents. CLAUDE.md imports
it. It derives from forge's AGENTS.md: the project-specific sections are
rewritten for the dashboard, and the working rules travel unchanged, because
the dashboard's own standards check compares them against forge's live text.

## What makes the forge dashboard special?

It manages repos it does not own, so every feature is judged by two things:
how little it assumes about a project, and how safely it acts on one.

### 1. Self-description over special cases

A project tells the dashboard what it is through `forge.project.toml`; the
dashboard assumes nothing else. The backend describes itself the same way:
services declare pydantic `Config`, `State`, actions, and events, and the
frontend builds forms, panels, and columns from those schemas. Adding a field
to a model makes it appear in the UI with no frontend edit. A bespoke
component is a defect unless the domain needs one (grid layout, diffs, logs,
transcripts, charts, timelines).

### 2. Never surprise a working tree

Every automated write happens in an isolated git worktree under the
dashboard's data dir. The user's checkout changes only through an explicit
action or a policy they set, and even then only by fast-forward. A sync or
bring-up that cannot land cleanly parks and waits.

### 3. Calm at a glance, fast on the keyboard

The overview is for scanning dozens of projects at once: dense, precise, and
quiet. Status is never color alone. Gates and refreshes stay fast so the page
never lies about freshness.

## A note from the maintainer

I like broad, general solutions — but never at the cost of design,
performance, or flexibility. Write structure, not quick fixes. Never implement
"for simplicity" or "defensively"; handle what the spec requires and fail
loudly otherwise. Use the existing, correct tool instead of hand-rolling one.
Never assume data shape or content — verify. No fragile systems: no reliance
on undefined ordering, and every field is identifiable at every point in the
program.

Treat the rest of this document as good defaults, not hard rules. If a rule
here fights the task in front of you, say so loudly and get sign-off before
breaking it.

## A small glossary

- **you** — the agent reading this file and changing the repo.
- **maintainer** — the human directing the work. That's who you're talking to.
- **forge repo** — the standards source, read live: its AGENTS.md, gate
  configs, and `tools/`. Never copy its content into dashboard code.
- **project** — a directory the dashboard tracks. *Managed* projects carry
  `forge.project.toml`; *unmanifested* ones are forge-like repos without one;
  the forge repo itself is the *standards source*.
- **manifest contract** — `docs/project-manifest.md`, implemented by the
  pydantic models in `src/forge_dashboard/manifest/` and published as
  `schema/forge.project.schema.json`. Other repos are written against it.
- **service** — a backend unit with `Config`, `State`, `@action`s, and events,
  mounted automatically under `/api/services/<id>`.
- **job** — one long-running action, tracked by the `jobs` service with a log,
  a result, and cancellation.
- **harness** — the configurable agent CLI (`claude -p` by default) that
  agent jobs run through.
- **gate** — one automated quality check. **The registry** —
  `src/forge_dashboard/gates.py`, the machine-readable list of this repo's
  deterministic gates.

## The ways to hurt yourself

1. **Manifest-contract drift.** Models, schema, doc, and tests change in the
   same commit, and only backward-compatibly: never rename or remove a key,
   never make an optional key required. Other projects' manifests are
   written against the current text.
2. **Touching a user's working tree from automation.** Every automated write
   happens in a worktree under the data dir. Never stash, reset, or check out
   in a user's checkout.
3. **Running git writes or pushes without an explicit user action or policy.**
   Fetch, pull, push, branch updates, and GitHub writes each need a click or a
   policy the user configured.
4. **Logging secrets.** Tokens never reach logs, events, job output, API
   responses, or test output. The GitHub auth chain reports only which source
   it used.
5. **Copying forge content into code instead of reading it live.** Standards
   checks and briefs read the forge repo at runtime.
6. **Weakening a gate to make your change pass**, or letting the registry
   drift from `.pre-commit-config.yaml` and `.github/workflows/ci.yml`. The
   test suite enforces the mirror.

## Commands

Python runs through `uv`; the frontend through Bun from `web/`. Never invoke
`pip`, bare `python`, `npm`, or a venv directly.

| Task | Command |
|---|---|
| Launch (TUI runner, opens the WebUI) | `./run.ps1` · `run.cmd` · `./run.sh` |
| Launch with frontend HMR | `./run.ps1 --dev` |
| Install / sync deps | `uv sync --group dev` and `bun install --cwd web` |
| Python tests | `uv run pytest -q` |
| Frontend tests | `bun test` (from `web/`) |
| Format | `uv run ruff format .` · `bun run --cwd web biome check --write .` |
| Lint | `uv run ruff check .` · `bun run --cwd web biome check .` |
| Types | `uv run mypy .` · `bun run --cwd web tsc --noEmit -p .` |
| Regenerate API types and web fixtures | `bun run --cwd web gen:api` (writes `web/openapi.json`, `web/test/fixtures/services.json`, `web/src/api/schema.d.ts`; tests fail when they are stale) |
| Regenerate manifest schema | `uv run forge-dashboard manifest schema > schema/forge.project.schema.json` |
| All commit-stage gates | `uv run pre-commit run --all-files` |
| Push-stage gate | `bash tools/stack_coverage_gate.sh` (needs `origin/main`; locally override with `DIFF_COVER_COMPARE_BRANCH=<root commit>`) |

Fast gates block commits: hygiene, `ruff format`, `ruff check`,
`mypy --strict`, `biome`, `tsc`. Slow gates block pushes: pytest and
`bun test` under coverage plus diff-cover requiring **≥90% coverage on changed
lines** across both languages. CI re-runs everything on push/PR, so bypassing
hooks locally never lands unchecked code. Write code that passes on the first
run instead of iterating against the gates.

## Where code lives

- `src/forge_dashboard/` — the backend package.
  - `core/` — service base class, `@action`, registry, event bus, SQLite.
  - `manifest/` — the manifest contract models, loader, and line locator.
  - `process/` and `supervisor/` — process-tree ownership, ports, and the
    restart state machine shared by the runner and project services.
  - `services/` — one module per dashboard service (`projects`, `git`,
    `forks`, `standards`, ...).
  - `domain/` — pure logic with no I/O: health rollup, progress math,
    scheduling, compliance rules, parsers.
  - `runner/` — the Textual TUI runner; `api/` — the FastAPI app factory.
  - `gates.py` — the registry.
- `web/` — the React + TypeScript frontend (Bun, Tailwind v4, shadcn/ui).
  - `src/api/` — typed client, the live state stream, react-query hooks,
    generated `schema.d.ts`.
  - `src/schema/` — the generic renderer: views, forms, widgets, and the one
    action dialog every button and the command palette share.
  - `src/features/` — pages: `overview/`, `project/` (one file per tab),
    `jobs/`, `fleet/`, `activity/`, `settings/`.
  - `src/app/` — shell, departures board, routes, palette, preferences.
  - `src/components/` — shared primitives; `components/ui/` is shadcn.
  - `test/` — Bun tests; `test/fixtures/` holds generated descriptors.
- `scripts/` — this repo's own manifest metrics and custom action (not
  template material).
- `schema/forge.project.schema.json` — generated manifest schema.
- `tools/` — gate scripts. `repo_hygiene.py` and `coverage_gate.sh` are
  verbatim from forge; `stack_coverage_gate.sh` extends the push gate to both
  languages.
- `test/` — pytest suite, including fixture repos and the fake harness.
- `docs/` — the spec, the manifest contract, and `ARCHITECTURE.md`.

## Verifying

- A task is done when the gates pass, not when the edit is written. Run the
  relevant commands and report their real output — never declare success from
  the diff.
- Smallest proof first: the tests you touched, then the commit-stage gates.
- Bug fixes ship with the regression test that would have caught them.
- Test names state the behavior pinned, not the function called.

## Commits

- Small, complete increments: each commit passes all commit-stage gates and
  contains one coherent change with its tests. No WIP commits, no giant final
  commit.
- Imperative mood, capitalized subject, ≤50 chars, no trailing punctuation.
  Body only when it adds what the subject can't: wrap at 72, blank line after
  the subject, explain why rather than what.

## Taste

- Guard clauses first; handle edge cases early and exit.
- Abstract only when there is churn, or when one logical change would
  otherwise require edits in multiple files. Premature abstraction is a
  defect.
- Prefer renaming to explaining. Comments carry constraints the code can't
  say — invariants, external contracts, non-obvious "why". Never narrate the
  change you just made or reference the session that produced it.
- Types exhaustive but non-restrictive: `Iterable` over `list` for
  parameters, Python 3.12+ syntax.
- No `try/except` that swallows errors, no dead config knobs, no
  commented-out code, no debug statements.
