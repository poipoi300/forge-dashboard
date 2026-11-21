# forge dashboard: specification

## 1. Product

The forge dashboard is a local control room for every project created with
forge. One glance answers these questions:

- Which projects are healthy, and which moved recently?
- What is running, and where?
- What is failing?
- How close is each project to V1?
- What needs the maintainer's attention?

One click does the routine management: run gates, launch a project, sync a
fork, bring a repo up to forge's standards, triage issues, or delegate work to
an agent.

A TUI runner starts it. The runner supervises the dashboard's own services and
opens the WebUI. Every function works with no manual step beyond launching.

forge itself lives at
`C:/Users/Administrator/Documents/Projects/Tests/forge` (the "forge
repo"). Read its `AGENTS.md`, `README.md`, `forge.toml`, `pyproject.toml`,
`.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `src/forge/`,
`test/`, and `tools/` before designing anything. Treat it as read-only during
development; the product only changes it through user-triggered actions.

## 2. Architecture

- **Backend.** Python 3.12, FastAPI, pydantic v2, async throughout. State
  lives in SQLite (WAL). Subprocesses go through one shared process module
  that owns process trees (section 2.1).
- **Frontend.** React 19 + TypeScript + Bun (bundler, dev server, tests) +
  Tailwind v4 + shadcn/ui as the component base, styled to the impeccable
  direction. Realtime arrives over one WebSocket event stream. TypeScript
  types are generated from the OpenAPI document (openapi-typescript) and
  drift-tested.
- **TUI runner.** Textual, built with the building-runner-tuis and
  designing-tuis skills. It supervises:
  - `api`: uvicorn serving the API and the built UI, on a dynamic port with
    preferred 8760;
  - `web`: only with `--dev`, the Bun dev server with HMR proxied to `api`;
  - pre-steps: `bun install` and `bun run build`, skipped by a content-hash
    stamp when nothing changed.

  The runner opens the browser when `api` is ready. It has keys for open,
  restart, logs, stop, and quit, and a plain-log path when stdout is not a
  TTY.
- **Launchers.** `run.ps1` and `run.cmd` (Windows) and `run.sh`
  (macOS/Linux) check for uv and Bun on PATH. If either is missing, they
  report how to install it and exit. A leading `--install-tools` opts into
  upstream installers for missing tools. Once both tools are available, the
  launcher runs `uv run -m forge_dashboard.runner` and forwards runner arguments.
- **One supervisor, two hosts.** The supervisor and process modules serve
  both the TUI runner, for the dashboard's own services, and the API, for
  project services declared in manifests.
- **Agent harness.** Agent invocations go through a configurable harness with
  the same shape as forge.toml's `[harness.*]`: an argv template with
  `{prompt}` and `{prompt_path}`. The default is `claude -p` with
  `--output-format stream-json --verbose`, which yields live transcripts plus
  the result's cost, turns, and duration. Model, permission mode, and allowed
  tools are configurable per agent-run kind.
- Ports 8000 and 8317 on this machine belong to other software. Never use
  them.

### 2.1 Process supervision

Follow building-runner-tuis exactly. The key points:

- Own the process tree. On Windows, use Job Objects with kill-on-close and a
  new process group. On POSIX, use a new session and signal the group.
- Stop in stages, and treat a user stop differently from a crash.
- Use the state machine from the skill, with backoff restarts and pure-function
  restart decisions under unit tests.
- Keep log ring buffers.
- Port policies are `dynamic` or `pinned`, with the listener-table freeness
  check.
- Resolved ports flow to dependents.

Project services declared in a manifest follow the same rules. Detect
already-running instances: a listener on the service's port whose process cwd
or cmdline lies inside the project. Show them as `running (external)`, not
as stopped.

### 2.2 Self-describing services and self-building layouts

The backend is a set of services. A service is a Python class with:

- `id`, `title`, `description`;
- `Config`: a pydantic model, persisted and editable;
- `State`: a pydantic model whose snapshot is pushed to clients on change;
- actions: methods decorated with `@action`, typed pydantic input to typed
  pydantic output. Long-running actions return a `JobRef` and report through
  the jobs service;
- events: typed pydantic models published on the event bus.

A registry mounts every service on FastAPI automatically:

- `GET /api/services` returns a `ServiceDescriptor` per service: id, title,
  description, `config_schema`, `state_schema`, actions (id, title,
  description, `input_schema`, `output_schema`, `long_running`, `danger`,
  `confirm`), and events (type, schema). All schemas are JSON Schema from
  pydantic and carry `x-ui` hints.
- `GET/PUT /api/services/{id}/config`, `GET /api/services/{id}/state`,
  `POST /api/services/{id}/actions/{action}`.
- One multiplexed WebSocket at `/api/events` carries state patches and events.
- `/api/health` returns 200 when ready.

The frontend has a generic schema renderer: `SchemaForm`, `SchemaView`, and
`ActionButton`, which builds its input dialog from `input_schema`. It renders
every config page, action dialog, state panel, and the overview's cards and
table columns, which come from a `ProjectSummary` schema with display hints.
Manifest custom actions render from their `params` JSON Schema the same way,
and custom metrics render by their `display` kind. Bespoke components exist
only where the domain needs them: the overview grid layout, diff viewer, log
viewer, transcript viewer, charts, and activity timeline. **Adding a field to
a pydantic model must make it appear in the UI with no frontend edit.** Pin
this with a test service whose fields cover every widget kind.

`x-ui` hint vocabulary, set through `json_schema_extra` as one object
`"x-ui": {...}` on fields and models:

| Key | Values |
|---|---|
| `widget` | `text`, `textarea`, `number`, `slider`, `toggle`, `select`, `radio-cards`, `multiselect`, `tags`, `path-dir`, `path-file`, `color`, `duration`, `cron`, `secret`, `code`, `json`, `model-picker` |
| `group`, `order` | Section name, position within it |
| `advanced`, `hidden` | Collapse under "Advanced", or omit |
| `unit`, `placeholder`, `help` | Display aids |
| `format` | For display: `relative-time`, `duration`, `bytes`, `percent`, `progress`, `badge`, `code`, `url`, `path` |
| `visible_if` | `{ "field": "...", "equals": value }` |
| `enum_labels`, `enum_descriptions` | Map of enum value to text |
| `columns` | On a list of objects: render as a table with these columns |

Discriminated unions render as a type selector plus the selected variant's
sub-form.

Dashboard services: `projects`, `discovery`, `manifests`, `git`, `github`,
`checks`, `metrics`, `services` (project service supervisor), `forks`,
`scheduler`, `standards`, `agents` (harness runs), `actions`, `jobs`,
`events` (with notifications), `machine`, `forge_runs`, `settings`.

## 3. Project manifest contract

`docs/project-manifest.md` in this repo defines `forge.project.toml`,
schema 1. You own it.

- Implement it exactly as pydantic models in
  `src/forge_dashboard/manifest/`.
- Generate `schema/forge.project.schema.json` from those models and
  drift-test it.
- Ship a CLI: `forge-dashboard manifest validate <path>` prints errors with
  line numbers and exits 1 on failure; `forge-dashboard manifest schema`
  prints the schema.
- You may extend the contract, but only in backward-compatible ways, updating
  the document and the schema together. Another agent is concurrently writing
  Reelcut's manifest
  (`C:/Users/Administrator/Documents/Projects/Reelcut`) against the
  current text.
- The dashboard dogfoods the contract: it carries its own `forge.project.toml`
  with its services, checks, metrics, actions, and V1 milestones. Mark each
  milestone done in the commit that completes it.

## 4. Discovery and registry

- **What counts as a project.**
  - A directory or git repo with `forge.project.toml` is a managed project.
  - A forge-like repo has an AGENTS.md derived from forge's plus forge gate
    configs, but no manifest. It is listed as unmanifested, with an Adopt
    action.
  - The forge repo itself (signature: pyproject name `forge` plus
    `src/forge/gates.py`) is listed as the standards source. Its git and CI
    state still shows.
- **Roots.** By default, scan the user's home directory to depth 6 in the
  background on first launch, with progress. Prune `node_modules`, `.venv`,
  `target`, `.git` internals, caches, `AppData`, `Library`, and hidden
  directories. Cache the results. Roots are editable through a server-side
  folder browser. Also offer "Add project" by path and "Clone from URL"
  (git clone into a chosen root).
- **Watching.** A filesystem watcher (`watchfiles`) on project roots triggers
  incremental refresh on manifest and git changes, with periodic polling as
  fallback.
- **Invalid manifests** show their exact validation errors with line numbers.
  They never hide the project.
- The forge repo path is auto-detected through its signature among the
  discovered repos. A settings field overrides it.

## 5. At a glance: the overview

There are two views, a card grid and a dense table, switchable by the user.
Both are schema-built from `ProjectSummary`. Per project:

- **Identity:** name, id, summary, kind, languages, tags, location (path,
  with copy and open), machine.
- **Time since last change:** the later of the last commit time and the
  newest working-tree change among tracked and untracked non-ignored files.
  It shows which of the two it is. Every relative time on the page updates
  from one shared tick.
- **Status marker:** a user-set lifecycle status (idea, planning, active,
  blocked, paused, maintenance, shipped, archived) with an optional note.
  This is separate from computed health.
- **Health:** a computed rollup (good, warn, failing, unknown) from:
  - CI conclusion on the default branch;
  - the last local checks;
  - manifest validity;
  - standards compliance;
  - crashed services.

  Hover or expand explains the rollup.
- **Tests and CI:**
  - last local check results: pass/fail counts from JUnit, duration, the
    commit it ran on, and staleness against HEAD;
  - the latest GitHub Actions runs per workflow on the default branch:
    status, conclusion, duration, time, link;
  - open PR check status;
  - coverage, when reported.
- **V1 progress bar** from the manifest's `[v1]`: done/total, the next
  milestone's title, and the target date with days left when set.
- **Issues:** open issue count, PR count, top labels, newest issue title.
- **Services:** state, actual bound address:port, machine, open link, and
  deployment health.
- **Git:** branch, dirty count, ahead/behind origin, unpushed commits, stash
  count. For forks: how far behind upstream, last sync result, next scheduled
  sync.
- **Standards:** compliance against forge as a count of passing checks, with a
  drift badge.
- **Pinned custom metrics** rendered by display kind, with sparkline history.
- **Agent activity:**
  - whether Claude Code is active in the project right now (a running
    `claude` process whose cwd is inside it);
  - when the last agent session happened, and how many there were, read-only
    from Claude Code's per-path transcript directory under
    `~/.claude/projects/`. Verify the path-encoding scheme against the
    directories that exist.
- **Quick actions:** Launch, Open UI, Run checks, Sync fork, Bring up to
  standards, Open in editor / terminal / file manager, GitHub.

**Overview controls:**

- search;
- filters: status, health, kind, language, tag, has-issues, failing, fork,
  running;
- sort: last change, name, health, progress, issues;
- group by: status, kind, health, root;
- saved views, and favorites pinned on top;
- keyboard navigation, and a command palette (Ctrl/Cmd+K) that jumps to a
  project or runs any action;
- density toggle, light and dark themes.

A global strip shows the number of projects, failing projects, running
services, and items needing attention, plus the next scheduled jobs and the
running jobs.

## 6. Project page

Clicking a project opens its page. impeccable decides between tabs and a
long scroll with anchored navigation.

- **Header:** name, status-marker editor, health, path, machine, links
  (GitHub, homepage, docs), primary actions.
- **Launch and interface:**
  - launch instructions from `[launch].instructions`, falling back to the
    README's setup or run section;
  - a Launch button for the primary service: starts its dependencies, waits
    for ready, opens it;
  - a services table: state, port, uptime, restarts, an ANSI-aware log
    viewer, open URL, stop and restart;
  - deployments with health and machine.
- **Quality:**
  - manifest checks with run buttons, history charts (pass rate, duration),
    and the latest JUnit details with failing tests and their messages;
  - gates parsed from `.pre-commit-config.yaml` (ids, stages), each runnable;
  - coverage;
  - CI: workflows, recent runs filterable by branch, run details (jobs,
    steps, conclusion, log links), and re-run of failed jobs.
- **Issues and PRs:**
  - list with filters for state, labels, assignee, milestone;
  - detail view with rendered markdown and comments;
  - create issue (schema-built form), comment, close or reopen, label;
  - "Delegate to agent" on an issue (section 8).
- **Git:**
  - branches with ahead/behind, recent commits with diffstat;
  - working-tree changes with a diff viewer;
  - worktrees, stashes, tags and releases with commits since the last tag;
  - actions: fetch, pull (fast-forward only), push (explicit), create branch.
- **Fork** (when the repo is a fork or has an upstream): section 7.
- **Standards:** the compliance checklist against forge with per-check detail
  and diffs, plus Bring up to standards (section 8).
- **Metrics:** every manifest metric with history charts, refresh now, and
  thresholds. Use the dataviz skill for every chart.
- **Actions:** manifest custom actions as schema-built forms, built-in
  actions, and per-project job history.
- **Agent sessions:** recent Claude Code sessions in the project (start, end,
  duration, first prompt line), running agent processes, and dashboard-run
  agent jobs with full transcripts.
- **Activity:** one timeline of commits, CI runs, checks, syncs, actions,
  status changes, and issues.
- **Notes:** free-form markdown, stored in dashboard state.
- **Settings:** per-project overrides: check schedule, metric intervals, fork
  policy, editor command, hidden.

## 7. Fork management

- **Detection.** GitHub API `fork`/`parent`, or an `upstream` remote. The UI
  can set the upstream URL and branch, which adds the remote.
- **Status:**
  - upstream default branch, with behind/ahead counts;
  - the commits to pull (messages, authors) and files changed;
  - a conflict prediction from `git merge-tree --write-tree` (no checkout),
    shown before syncing.
- **Policy per fork.** Stored in dashboard state, edited through a
  schema-built form:
  - target branch (default: the fork's default branch);
  - strategy: merge or rebase;
  - schedule: manual, interval, or cron, with the next run shown;
  - auto-push to origin after a successful sync (default off);
  - conflict handling: agent through the harness (default) or stop and
    notify;
  - max agent attempts (default 2);
  - checks to run after the merge (default: the manifest's commit- and
    push-stage checks);
  - when to notify: success, conflict, failure.
- **A sync run is a job, always in an isolated git worktree** under the
  dashboard's data dir. It never touches the user's working tree and never
  stashes the user's work.
  1. Fetch upstream and origin. Create a worktree on
     `forge-sync/<upstream-branch>-<timestamp>` from the target branch.
  2. Merge or rebase upstream. If it is clean, go to step 4.
  3. On conflicts:
     - Build a conflict brief: each conflicted file with both sides,
       upstream's commits since the merge base, the fork's commits since the
       merge base, and the project's AGENTS.md.
     - Run the harness (`claude -p`) in the worktree with edit-only
       permissions. The agent edits files; the dashboard runs every git
       command itself.
     - Verify: no conflict markers, `git diff --check` clean, and files
       parse (the hygiene checks).
     - If verification fails, retry with the failure output appended, up to
       the attempt limit.
  4. Run the policy's checks in the worktree. A failure parks the run as
     needs-attention and keeps the branch.
  5. On success:
     - Commit, with a message recording the upstream SHA and whether an agent
       resolved conflicts.
     - Update the target branch. Fast-forward it when the target is not
       checked out anywhere, or is checked out in a clean worktree (via
       `git merge --ff-only` there).
     - When the target is checked out dirty, park the run as ready-to-apply,
       to be applied later in one click.
     - Push when the policy says so.
- **Parked runs** show a diff viewer (against the target and against
  upstream), the agent transcript, and check output. Actions:
  - apply;
  - retry the agent with extra instructions from a text box;
  - open the worktree in the editor;
  - abort, which removes the worktree and branch.
- **History per fork:** each run's outcome, duration, and agent cost and
  turns.
- **Scheduler:**
  - persisted in SQLite, so it survives restarts;
  - catches up a missed run once, never in a burst;
  - jitter, a global pause, and a visible queue;
  - a concurrency limit, one agent run at a time by default.

## 8. Standards and agent actions

**Standards source.** The forge repo, read live each time; never copy forge's
content into dashboard code. Compliance checks each have an id, title, result
(pass, fail, n/a), detail, and fix kind (deterministic or agent). Derive the
final list from forge's AGENTS.md and configs; at minimum:

1. `forge.project.toml` present and valid.
2. AGENTS.md present.
3. AGENTS.md's travelling sections ("A note from the maintainer",
   "Verifying", "Commits", "Taste") equal forge's current text, compared
   section by section on headings.
4. CLAUDE.md imports AGENTS.md.
5. `tools/repo_hygiene.py` and `tools/coverage_gate.sh` are byte-identical to
   forge's.
6. `.pre-commit-config.yaml` holds forge's base hook ids with matching
   stages.
7. Each manifest language has its conventional gate hook ids (see the
   contract's hook-id conventions).
8. pre-commit and pre-push hooks are installed in `.git/hooks`.
9. A CI workflow exists and runs the CI-enforced gates.
10. Python projects: ruff and mypy configs are at least as strict as forge's
    (rule selection is a superset; mypy strict).
11. A gate-registry mirror test exists.
12. README exists with a setup or run section.
13. `.gitignore` covers forge's entries.

The standards view shows the compliance score and per-check drift detail.
It also shows the forge revision the project last aligned to
(`[forge].template_revision`) against forge's HEAD, and lists the forge
commits since.

**Bring up to standards.** A job on a worktree branch
`forge-standards/<timestamp>`:

1. Apply deterministic fixes: copy `tools/`, add CLAUDE.md, install hooks.
   Hooks are installed in the main repo, not the worktree.
2. Run the harness with a generated brief:
   - the failing checks with details;
   - forge's AGENTS.md and configs;
   - the project's languages;
   - the rules: rewrite AGENTS.md's project-specific sections, keep the
     working rules verbatim, adapt gates to the project's languages, never
     weaken an existing gate, update the registry and its tests, and set
     `[forge].template_revision`.
3. Re-run compliance and the project checks.
4. Present the diff for a one-click merge, which fast-forwards the default
   branch when clean, or keep the branch.

**Other agent and built-in actions:**

- **Adopt** (unmanifested repo): the harness writes `forge.project.toml` from
  reading the repo. It is validated against the schema with a repair loop,
  then Bring up to standards is offered.
- **Delegate issue:** the harness implements a GitHub issue on a worktree
  branch and runs the checks, then the diff is presented. "Open PR" is an
  explicit button that pushes the branch and creates the PR.
- **Built-ins:**
  - run checks;
  - run all commit gates (`pre-commit run --all-files`);
  - run the push gate;
  - update dependencies (harness, on a branch);
  - clean build artifacts: shows the sizes of `.venv`, `target`,
    `node_modules`, and caches, and deletes only after confirmation;
  - open in editor (auto-detect Zed, VS Code, JetBrains; configurable),
    terminal, folder, GitHub.
- **Every agent run:**
  - live transcript from stream-json;
  - cost, turns, and duration;
  - cancellable and logged;
  - one at a time by default;
  - never on the user's main working tree.

## 9. GitHub integration

- **Auth chain,** with no manual step whenever any source exists:
  1. `GH_TOKEN` or `GITHUB_TOKEN`;
  2. `gh auth token`, when gh is installed (it is not, on this machine);
  3. the git credential helper, non-interactively: `git credential fill`
     with `GIT_TERMINAL_PROMPT=0` and `GCM_INTERACTIVE=never`, so no GUI
     prompt can appear;
  4. unauthenticated, for public repos at 60 requests/hour, with a clear
     degraded banner and a one-time token form stored in the OS keyring.

  Never log or display tokens.
- **Requests.** REST, plus GraphQL where it saves calls. ETag conditional
  requests, and a rate-limit-aware scheduler that shows the remaining budget.
  Per-repo polling intervals, faster while runs are active. Responses are
  cached in SQLite.
- **Data:**
  - repo metadata: fork/parent, default branch, visibility;
  - issues;
  - PRs with check runs;
  - Actions workflows, runs, and jobs;
  - releases and tags;
  - deployments and environments.
- **Writes** happen only on explicit user action: create, comment on, or
  close an issue; re-run jobs; create a PR; push. Tests always mock GitHub
  (respx). Live read-only tests against a public repo self-skip offline.
- None of the repos on this machine has a GitHub remote yet. The UI handles
  "not on GitHub" as a first-class state, not an error.

## 10. Local checks and metrics

- **Checks** run in the working tree by default, which reflects the current
  state. An option runs them on HEAD in a temporary worktree instead.
  - JUnit reports are parsed.
  - Results are stored with the commit SHA and a dirty flag.
  - Schedules: on change (debounced), interval, or manual.
  - A global concurrency limit applies, and never two checks run in the same
    repo at once.
- **Metrics** run on their interval or on demand. Readings are validated as
  `MetricReading` and stored as history. Thresholds set status. Failures show
  stderr.

## 11. Machine and services view

- All services across projects, with state, host:port, machine, uptime, and
  owner (dashboard or external process). A port map shows conflicts. Stop
  all; open any.
- A machine panel: hostname, OS, CPU, RAM, GPU load (when nvidia-smi
  exists), and free disk on each project root.
- Deployments show their machine and HTTP health.

## 12. Events and notifications

- One event log in SQLite feeds every activity view. The UI has a
  notification center with an unread count.
- OS desktop notifications, configurable per kind, fire for:
  - CI failure on a default branch;
  - a fork conflict needing attention;
  - a scheduled run failing;
  - a check regressing from pass to fail;
  - a service crash.

## 13. Forge runs

forge's runner writes per-step manifests (see `src/forge/runner.py`):
`<workspace_root>/<project>/meta/step-NN-<name>.json`, plus stdout and stderr
files. Show these trajectories per project: steps with ok, exit code, or
timeout, durations, workspace links, and stdout/stderr viewers. Read
`workspace_root` from the forge repo's `forge.toml`. Also auto-detect
directories next to the forge repo that follow the same layout, such as
`Tests/forge-runs/hair-brawler/meta/`. Extra run roots are configurable.

## 14. Also include

- Export the overview as a Markdown or JSON report.
- `GET /api/projects` as a scripting API.
- CLI subcommands: `forge-dashboard list`, `status <id>`, `check <id>`,
  `sync <id>`, `manifest validate <path>`, `manifest schema`.
- Backup and restore of dashboard state.
- Anything else that makes managing many agent-built projects faster. Add it
  when its value is clear, and list it in the README.

## 15. UI

- **Design.** Use the impeccable skill end to end: context, init, new-work,
  craft-floor before every UI edit, build, then critique, audit, and polish.
  The surface mode is Operate. Optimize for scanning many projects at once:
  dense, calm, precise, with a visual world of its own. The density bar is
  Linear and Vercel's dashboard, without Grafana's clutter. Status is never
  encoded by color alone.
- Light and dark themes, keyboard-first, responsive down to laptop widths,
  and readable on a phone for a quick glance (read-only is acceptable there).
- Designed empty, loading, error, and degraded states everywhere: no projects
  found yet, scan in progress, GitHub unauthenticated, forge repo not found,
  invalid manifest, agent harness missing.
- Record decisions in PRODUCT.md and DESIGN.md as the skill directs.

## 16. Engineering practices (forge)

- **AGENTS.md** derives from forge's. Rewrite the project-specific sections:
  intro, "What makes the forge dashboard special?", glossary, ways to hurt
  yourself, commands, where code lives. Keep these working-rule sections
  verbatim: "A note from the maintainer", "Verifying", "Commits", "Taste".
  `CLAUDE.md` contains only `@AGENTS.md`.
- Copy `tools/repo_hygiene.py` and `tools/coverage_gate.sh` verbatim. Any
  extra gate script goes in `tools/` under its own name. List each place you
  had to diverge from a forge template artifact in your final report.
- `.pre-commit-config.yaml` uses only local `language: system` hooks. It
  keeps forge's base hooks and adds `biome` and `tsc` for the frontend.
  - Commit-stage gates must run in seconds.
  - The push stage has the single hook `tests-and-diff-coverage`: pytest and
    bun tests under coverage, with ≥90% changed-line coverage across both
    languages through diff-cover (Cobertura and LCOV).
- `.github/workflows/ci.yml` re-runs every CI-enforced gate.
- A gate registry module (`src/forge_dashboard/gates.py`) has tests that
  mirror both configs, as in forge.
- Python 3.12 with forge's ruff selection and strict mypy. Run everything
  through `uv`.
- TypeScript: strict tsconfig, Biome, Bun.
- **Ways to hurt yourself**, seeding the project's AGENTS.md:
  - Manifest-contract drift: models, schema, doc, and tests change in the
    same commit, and only backward-compatibly.
  - Touching a user's working tree from automation: every write happens in a
    worktree.
  - Running git writes or pushes without an explicit user action or policy.
  - Logging secrets.
  - Copying forge content into code instead of reading it live.
- **Tests:**
  - Fixtures build temp git repos with manifests: valid, invalid, forks with
    a bare upstream, deliberately non-compliant repos.
  - A fake harness script resolves conflicts deterministically for fork-sync
    and standards tests.
  - respx mocks for GitHub.
  - Pure-function tests for supervisor decisions, health rollup, progress
    math, and scheduling.
  - Bun tests for the schema renderer, covering every widget kind, and for
    store logic.
- **Live tests** self-skip when their dependency is missing. One runs a real
  `claude -p` conflict resolution on fixture repos with the cheapest model
  (`--model haiku`). One runs Bring up to standards on a fixture repo the
  same way. Keep live agent runs to a handful.

## 17. V1 milestones

1. `scaffold-gates`: repo, forge practices, gates, own manifest.
2. `manifest-contract`: models, schema, validator CLI, doc.
3. `discovery`: roots scan, watcher, registry, unmanifested and forge
   detection.
4. `supervisor-runner`: process module, supervisor, TUI runner, launchers.
5. `services-api`: service registry, descriptors, event stream, jobs,
   scheduler.
6. `git-checks-metrics`: git state, local checks with JUnit, metrics with
   history.
7. `github`: auth chain, issues, PRs, Actions, caching, rate limits.
8. `forks`: detection, prediction, sync worktree flow, agent conflict
   resolution, scheduling, parked runs.
9. `standards`: compliance checks, bring-up flow, adopt, template revision.
10. `agents`: harness, transcripts, delegate issue, run history, Claude Code
    session insight.
11. `web-ui`: impeccable-designed overview, project page, fork, standards,
    jobs, settings, schema renderer.
12. `project-services`: launch and supervise manifest services, external
    detection, machine view.
13. `notifications-runs`: events, OS notifications, forge runs view, reports.
14. `hardening`: empty and error states, performance, docs.

## 18. Definition of done

Every item holds, verified by running it, not by reading the diff.

- All commit-stage gates and the push gate pass (set
  `DIFF_COVER_COMPARE_BRANCH` to the root commit or an equivalent baseline,
  since there is no remote).
- From a fresh clone with uv and Bun on PATH, `run.ps1` starts the TUI runner,
  which opens the WebUI. The WebUI lists the projects on this machine:
  Reelcut, the dashboard itself, and forge as the standards source.
- Every overview element shows real data for those projects. Where data
  cannot exist yet (no GitHub remote), the designed state shows instead.
- Fork sync is verified end to end on fixture repos with a real conflict:
  with the fake harness in tests, and once with the real `claude -p`.
- Bring up to standards is verified on a deliberately non-compliant fixture
  repo, deterministic and agent paths, once live.
- GitHub features are verified with mocks, and live read-only against a
  public repo.
- Launching a manifest-declared service from the dashboard works and shows
  its address and port. Verify with a fixture project's service. Do not run
  Reelcut's own commands: another agent is building it concurrently, and
  its builds would collide with yours. Reading its files is fine.
- impeccable critique, audit, and polish passes on the overview, project
  page, fork view, standards view, and jobs view, with findings fixed. Look at
  screenshots at 1440p and 1080p, in light and dark.
- README, AGENTS.md, `docs/project-manifest.md`, and the JSON Schema are
  accurate. The dashboard's own manifest is valid, and every V1 milestone is
  done.
