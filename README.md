# forge dashboard

A local control room for every project created with
[forge](../Tests/forge). One glance answers which projects are healthy, what
moved recently, what is running and where, what is failing, how close each
project is to V1, and what needs you. One click runs the routine work: gates,
launches, fork syncs, bringing a repo up to forge's standards, triaging
issues, and delegating work to an agent.

## Run it

```powershell
./run.ps1          # Windows PowerShell
run.cmd            # Windows cmd
./run.sh           # macOS / Linux
```

That is the only step. The launcher installs uv and Bun when they are
missing, then starts the TUI runner, which installs dependencies, builds the
WebUI (skipped when nothing changed), starts the API on a free port
(preferring 8760), and opens the browser once it answers. Without a terminal
it streams prefixed logs instead of drawing the TUI.

| Runner flag | Effect |
|---|---|
| `--dev` | also run the Bun dev server with hot reload, proxied to the API |
| `--check` | check executables and ports, then exit |
| `--no-browser` | do not open the WebUI |
| `--no-autostart` | open the TUI without starting services |
| `--only NAME ...` | manage a subset of services |
| `--plain` | stream plain logs even on a terminal |

The API answers only on this computer: it refuses requests whose `Host` or
`Origin` is not a loopback address, so no web page you visit can drive it.
To serve it to another machine, run the server with a non-loopback `--host`
and name each address clients use with `--allow-host` (see
`uv run -m forge_dashboard.server --help`); anyone who can reach it controls
this machine.

In the TUI: `s` start, `x` stop, `r` restart, `o` open the WebUI, `l` write
the log to `logs/`, `a`/`z` start or stop all, `?` every key, `q` quit (stops
everything it started).

On first launch the dashboard scans your home folder (skipping hidden
folders, caches, `node_modules`, virtualenvs, and `AppData`) for projects:
folders with a `forge.project.toml`, forge itself (the standards source), and
repos whose `AGENTS.md` and gate config derive from forge's but have no
manifest yet. Add any other folder by hand, and change the scanned folders,
in Settings. State lives in the per-user data dir; set
`FORGE_DASHBOARD_DATA_DIR` to use another.

## What it does

- **Overview.** A card or a table row per project: the lifecycle status you
  set, computed health with its reasons, time since the last commit or edit,
  V1 progress with the next milestone and days left, local checks, CI,
  issues and pull requests, git state, fork drift, standards compliance,
  running services with their real addresses, deployments, pinned custom
  metrics with sparklines, and whether an agent is working in it now. Search,
  facet filters, sort, grouping, saved views, favourites on top, keyboard
  travel (`j`/`k`, `Enter`, `f`, `/`), density, and light and dark themes.
  Every view is a URL.
- **Departures board.** Project, failing, running-service, and
  needs-attention counts, the next scheduled jobs, and the jobs running now.
- **Project page.** Launch instructions with a Launch button and the
  supervised services' live logs; quality (checks with history charts and
  JUnit failures, pre-commit gates, coverage, GitHub Actions runs with
  re-run); issues and pull requests with comment, label, close, and delegate
  to an agent; git with diffs, branches, commits, worktrees, stashes, tags,
  and releases; fork sync; standards; metrics; custom actions; agent sessions
  and transcripts; an activity timeline; notes; per-project settings.
- **Forks.** Upstream detection, a conflict prediction before syncing, and
  syncs that always run in an isolated worktree: merge or rebase, the agent
  (`claude -p`) resolves conflicts, the checks run, and nothing lands or
  pushes unless the fork's policy says so. Manual or scheduled, per fork.
- **Standards.** Compliance against forge's live templates with per-check
  detail, and Bring up to standards (or Adopt, for a repo without a
  manifest) on a branch you review and merge.
- **Jobs, services, and the machine.** Every long action is a job with a live
  ANSI log. Services across projects, CPU, memory, GPUs, disks, and the port
  map with conflicts.
- **Forge runs, activity, notifications.** forge's evaluation runs step by
  step with their output, one timeline of everything, and desktop
  notifications for CI failures, fork conflicts, crashes, and check
  regressions.
- **Command palette.** `Ctrl+K` jumps to any project or page and runs any
  action.

Beyond the brief: the departures board, saved views, Claude Code session
insight per project, delegating an issue to an agent on a branch, dependency
updates by agent, build-artifact cleanup, backup and restore, report export
(`/api/report.md`), a scripting API (`/api/projects`), and a CLI
(`uv run forge-dashboard list | status | check | sync`).

## Make a project dashboard-aware

Add a `forge.project.toml` at the repo root. It declares V1 milestones,
checks, services, deployments, custom metrics, custom actions, and launch
instructions; beyond `[project]` and `[v1]` everything is optional.
[`docs/project-manifest.md`](docs/project-manifest.md) is the contract and
[`schema/forge.project.schema.json`](schema/forge.project.schema.json) its
JSON Schema. Validate one with:

```sh
uv run forge-dashboard manifest validate path/to/repo
```

This repo's own [`forge.project.toml`](forge.project.toml) is a working
example: a dev service, two pinned metrics, and three custom actions.

## Develop

```sh
uv sync --group dev
bun install --cwd web
uv run pre-commit install --install-hooks -t pre-commit -t pre-push
```

| Task | Command |
|---|---|
| Python tests | `uv run pytest -q` |
| Frontend tests | `bun test` (in `web/`) |
| Commit-stage gates | `uv run pre-commit run --all-files` |
| Push gate | `bash tools/stack_coverage_gate.sh` (no remote: `DIFF_COVER_COMPARE_BRANCH=<root commit>`) |
| Regenerate API types and fixtures | `bun run gen:api` (in `web/`) |
| Live agent tests (spend tokens) | `FORGE_DASHBOARD_LIVE_AGENT=1 uv run pytest -m live` |

[`AGENTS.md`](AGENTS.md) holds the working rules,
[`docs/SPEC.md`](docs/SPEC.md) the specification, and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) every decision the spec left
open.
