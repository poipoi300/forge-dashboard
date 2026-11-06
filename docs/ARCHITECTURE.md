# Architecture and decisions

The spec (`docs/SPEC.md`) says what to build. This file records how, and every
decision the spec left open, with the reason. Newest decisions are appended
to their section.

## Repository and gates

- **Two-language push gate.** `tools/coverage_gate.sh` stays byte-identical
  to forge's (standards check 5 compares it), but it only knows Python. The
  push hook `tests-and-diff-coverage` therefore runs
  `tools/stack_coverage_gate.sh`, which runs pytest under coverage.py and
  `bun test --coverage`, rewrites Bun's LCOV paths to repo-relative forward
  slashes, and feeds both reports to one diff-cover call so the 90% bar
  spans both languages.
- **Frontend hooks run whole-project.** `biome` and `tsc` use
  `pass_filenames: false` and run from `web/` through `bun run --cwd web`, so
  the hook entry needs no PATH tricks for `node_modules/.bin` on Windows.
- **No `pytest-textual-snapshot`.** Its pinned syrupy requires pytest < 9,
  and forge pins pytest >= 9.1. TUI snapshots are plain-text renders of the
  compositor, compared to committed files.

## Manifest contract

- **Two validation layers, one error format.** Field-level rules live on
  pydantic fields; cross-field rules (placeholders allowed by sibling
  settings, unique ids, references, cycles) live in model validators that
  raise `ManifestRuleViolation` carrying locations relative to the model.
  pydantic keeps the exception in the error context, so the loader joins the
  outer and inner locations and every rule points at an exact key.
- **Line numbers.** `tomllib` has no positions, so `manifest/locator.py` scans
  documents `tomllib` already accepted and records the line of every header,
  key, and array element under the same concrete path pydantic reports.
  Discriminated-union tags in error locations are dropped by matching them
  against the discriminator values present in the document.
- **Placeholders use `string.Formatter`.** The contract's `{name}` with
  `{{`/`}}` escapes is exactly `str.format` syntax, so parsing and rendering
  reuse the standard library. Conversions and format specs are rejected.
- **Clarifications added to the contract (backward compatible).** A portless
  service cannot use `{port}`, `open`, or port/http readiness; `open` and
  readiness paths are URL paths; a metric's HTTP source service must declare a
  port; `github_milestone` and `[[v1.milestones]]` are exclusive by source;
  `ActionResult` keys are all optional; a "Cross-table rules" section lists
  the reference and uniqueness checks. None of these accept less than a
  manifest that could ever have worked.
- **`schema` field alias.** The top-level key `schema` would shadow a pydantic
  `BaseModel` attribute, so the model field is `schema_version` with alias
  `schema`; the generated JSON Schema uses the alias.

## Processes and supervision

- **`mypy_path = "src"`** is added to forge's mypy settings. With
  `explicit_package_bases` and a `src/` layout, mypy otherwise names modules
  `src.forge_dashboard...` and, under `ignore_missing_imports`, silently
  types every cross-module import as `Any`. This tightens the gate.
- **Every subprocess goes through `process/tree.py`.** Windows children start
  suspended in a new process group, join a kill-on-close Job Object, then
  resume; job membership (not the parent chain) defines the tree, so orphaned
  grandchildren of `uv run`/`bun run` are found and killed. POSIX children
  start a new session and the group is signalled. `spawn` is
  cancellation-safe: a cancel during creation still kills the child.
- **Launch plans layer env over ours.** `LaunchPlan.env` and `PreStep.env`
  are overrides; the supervisor merges them with its own environment, since a
  Windows child without `SYSTEMROOT` cannot even open a socket.
- **A start is active immediately.** `Supervisor.start` moves the service to
  `starting` before its task first runs, so a dependent waiting for readiness
  never mistakes a just-started provider for a stopped one.

## Services framework

- **Services are constructed before the event loop starts; storage opens in
  the lifespan.** Constructors do no I/O, so the app (and its OpenAPI
  document) can be built without a database, which is how
  `forge-dashboard openapi` exports the schema the TypeScript types are
  generated from.
- **Every action is a typed route.** Each `@action` becomes
  `POST /api/services/{id}/actions/{action}` with its own request and
  response models (a generated endpoint gets a real `inspect.Signature`), so
  OpenAPI carries every model. WebSocket-only models are merged into the
  OpenAPI components for the same reason.
- **Query actions.** The descriptor adds `query: bool` to the spec's action
  fields: read-only requests (job logs, diffs, issue lists) are actions too,
  so they share validation and typing, but the generic UI never renders them
  as buttons.
- **State patches are RFC 6902.** The state hub coalesces changes for 50 ms,
  diffs with `jsonpatch`, and numbers revisions per service; a client that
  falls behind gets a fresh hello with snapshots instead of a gap.
- **The WebSocket handler uses an anyio task group.** Raw asyncio tasks inside
  a connection made the ASGI server's anyio cancel scope leak a
  `CancelledError` on roughly one close in seven (reproduced in a loop,
  fixed, then 40 clean runs).
- **Jobs own concurrency.** A job names resources (`agent`, `checks`,
  `repo:<project>`) and is queued until it holds all of them, acquired in
  sorted order so two jobs can never deadlock. "One agent run at a time" and
  "never two checks in one repo" are both this mechanism. `ATTENTION` is a
  terminal job status for work that finished but parked for the user.
- **x-ui extensions** beyond the spec vocabulary: `bind` (fill from page
  context, e.g. the current project), `tones` (per-enum tone for badges and
  boolean flags, always paired with text), `min`/`max`/`step` (sliders),
  `short_label` (dense headers; `""` leaves a value unlabelled), `slot`
  (overview card placement), `hide_zero` (leave zero counts out of dense
  views), and the `markdown` display format. `columns` also applies to an
  object-valued field: it picks the sub-fields a dense view shows.
- **Two composite object formats.** `format: "progress"` reads `done`,
  `total`, `caption`, and an optional `detail` line; `format: "badge"` on an
  object reads `level` and lists the remaining fields on hover. A dense
  boolean reads as a flag: its label when true, nothing when false.
- **Output-only OpenAPI schemas require every field.** pydantic serializes
  every field, but its serialization-mode schema leaves defaulted ones
  optional, so generated TypeScript guarded values that are always present.
  `require_output_fields` marks every property required on schemas reachable
  only from responses and the stream; schemas shared with request bodies keep
  their optional fields.
- **Generated artifacts are pinned by tests.** `web/openapi.json` and the
  descriptor fixture `web/test/fixtures/services.json` are written by
  `bun run gen:api` (LF endings) and compared to the live app in pytest, so a
  backend change that forgets to regenerate fails the suite.
- **The events service registers first.** It subscribes to the bus in
  `start()`, so registering it first means no later service can publish
  before the subscription exists, and it stops last. A notification's kind is
  derived from the declared event classes' `notify_kind` rather than stored.
- **Restore is phased.** Stop every service, restore the database, reload
  every config, start every service, after writing a safety backup; this
  keeps `Runtime`'s rule that all configs load before any `start()`.
- **Build output is offered for cleanup only when git ignores everything in
  it.** "Git tracks nothing inside" still offered an untracked `build/` holding
  new source; now every file must be ignored, nested repositories are skipped,
  and a project without git offers nothing unless the request opts in.
- **Services restart.** `start()` after `stop()` is part of the `Service`
  contract, because restore stops and starts every service; services that
  register hooks do it once in `__init__`, and loops run under
  `core.tasks.repeat`, which logs a failing round and keeps going.
- **Scheduled catch-up waits for projects.** A schedule that came due while
  the dashboard was off runs once the project registry has loaded
  (`targets_ready`), not before, when its project does not exist yet.
- **The database serializes writes.** A write is its statement plus its
  commit under one lock, and snapshots (`VACUUM INTO`) and restores take the
  same lock: a snapshot landing between another task's statement and commit
  deadlocked the shared connection.
- **Per-project overrides live in dashboard state.** Check schedules, fork
  policies, editor commands, hidden flags, and metric interval overrides are
  kv entries keyed by project id; the manifest stays the default and is never
  written by these settings.

## Projects, launch, and logs

- **Launch instructions.** `projects.launch-guide` returns
  `[launch].instructions` (inline markdown, or a repo-relative `.md` path
  that must resolve inside the project), else the README's first setup or run
  heading outside code fences up to the next heading of the same level. The
  standards check "README has a setup or run section" uses the same rule.
- **Service logs read by absolute line number.** Offsets into the 2000-line
  ring buffer stopped advancing once it filled; the supervisor counts lines
  ever written, and readers pass that cursor back.
- **Check facts carry one result.** `not run yet`, `running`, `passing`, or
  `failing`, so a card can say "tests passing" without reading counts.
- **Attention means a decision.** Failing health reasons, parked fork syncs,
  and a blocked status count; standards drift and checks stale against HEAD
  stay in health and their own signals. Counting the backlog flagged nearly
  every project and ranked nothing.

## Safety

- **The API answers only to loopback.** It runs code on this machine
  (launchers, merges, pushes, deletions) and streams every log, so
  `api/access.py` serves only loopback `Host` names (DNS rebinding carries
  the attacker's name) and refuses any request or WebSocket whose `Origin` is
  present and not loopback (a cross-site page). No `Origin` (the CLI, curl,
  the runner's probe) passes, and the Bun dev server on another loopback port
  keeps working. Binding another address requires naming it with
  `--allow-host`. `server.json` carries only the URL the CLI needs.
- **Issue-driven agent runs get project tools, not a shell.** Delegated
  issues and dependency updates read text anyone can write, so those runs may
  edit files and run the project's own `uv`, `bun`, and `cargo` commands,
  never arbitrary shell, git, or the network.
- **Landing is fast-forward or nothing.** A merged sync moves a checked-out
  branch only by `merge --ff-only` in a clean checkout; a rebased sync never
  lands into a checked-out branch, because that would reset the user's tree,
  and parks as ready to apply until the branch is checked out nowhere. A
  refusal carries git's own message instead of "commit or stash". Before
  anything lands, upstream must be an ancestor of the result.
- **Fork preview is a pure read.** `forks.preview` neither fetches nor
  touches state (it re-stamped the prediction, so a view following the forks
  state refetched it forever); `forks.refresh-status` is the explicit fetch.
- **Gates run in the checkout, behind a confirmation.** The point is to judge
  the working tree as it is, uncommitted edits included, and fixers rewrite
  it, so the click that starts them states that.
- **Pinned ports are reclaimed only from ourselves.** A listener on a pinned
  port is killed without asking only when it is inside the project and its
  `(pid, create time)` was recorded from this supervisor's own process tree;
  anything else blocks the service until the confirmed release action.
- **The default branch is resolved, not assumed.** The manifest's
  `default_branch` wins only when it is set explicitly; otherwise work starts
  from origin's HEAD, else the checked-out branch, and fails loudly when none
  exists.
- **Every agent or sync run is guarded.** An exception or cancellation after
  a run is recorded marks it failed (or parked) with the error instead of
  leaving it `running`; startup reconciles stored runs and removes worktree
  directories no run references.
- **Remote URLs are redacted where they are published.** Credentials in a
  remote's userinfo never reach state, the API, reports, or job logs; git
  commands keep using the remote by name.
- **User strings never become git options.** Revisions are verified with
  `rev-parse --verify --end-of-options`, remotes are checked against
  `git remote`, and every user value follows `--end-of-options` or `--`.

## WebUI

- **Design direction.** Swiss railway station information design (direction
  contract in `.impeccable/surfaces/`, system in `DESIGN.md`): a dark
  departure board owns the rail and the departures strip in both themes,
  signal red marks only failures and delays, and Archivo's width axis sets
  every time and count condensed and tabular. impeccable's interactive steps
  (picking a direction, approving comps) were answered from the spec, and the
  build is code-led: no generated imagery.
- **Self-building layouts.** The overview card places `ProjectSummary`
  fields by their `slot` hint and renders each through the schema renderer,
  and the table takes its default columns from `ProjectsState.projects`;
  settings forms come from each service's config schema; every action dialog
  comes from the action's input schema. A field added on the backend appears
  without a frontend edit.
- **One action dialog host.** Buttons, menus, and the command palette all call
  `useActionRunner`; a single `ActionDialogHost` owns the dialog, so a dialog
  outlives the menu that opened it. `input` fixes fields; `defaults` prefills
  fields the user may still edit (current settings).
- **Live queries follow service revisions.** `useAction(path, input,
  {live, follow})` keys react-query on the owning services' state revisions,
  so read-only views refetch when the server changes, with no polling and no
  second cache. The previous answer is kept while refetching, but never
  across a different input.
- **The URL is the view.** Overview search, filters, sort, and grouping live
  in the query string; a project's tab and a job's id live in the path; the
  production build uses absolute asset URLs so deep links load.
- **Preferences stay in the browser.** Theme, density, layout, saved views,
  and table columns persist in `localStorage`; favourites and status markers
  are server state because other surfaces (the CLI, reports) read them.
