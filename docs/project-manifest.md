# forge project manifest: `forge.project.toml` (schema 1)

Every forge project carries `forge.project.toml` at its repository root. It is
the project's self-description: identity, V1 roadmap, local checks, services,
deployments, custom metrics, and custom actions. The forge dashboard reads it
and assumes nothing else about a project.

The pydantic models in the dashboard (`src/forge_dashboard/manifest/`) are the
source of truth for this contract. `schema/forge.project.schema.json` is
generated from them and drift-tested. This document describes schema 1; any
extension must stay backward compatible with every manifest valid under the
text below, because other projects are written against it concurrently.

## General rules

- TOML, UTF-8. Unknown keys are a validation error, so typos fail loudly.
- Every `id` matches `^[a-z0-9][a-z0-9-]*$` and is unique within its table.
  `project.id` is unique across all projects the dashboard knows.
- Paths are repo-relative with forward slashes. Absolute paths and `..`
  segments are rejected.
- Commands are argv arrays, never shell strings. `argv[0]` is resolved on PATH
  (on Windows with PATHEXT). Commands run with cwd = repo root unless `cwd` is
  given.
- Placeholders allowed in argv and env values: `{project_root}` everywhere;
  `{port}` and `{host}` in `[[services]]`; `{report_path}` in `[[checks]]`
  with `report = "junit"`. Any other `{...}` is a validation error. Literal
  braces are written `{{` and `}}`.
- Every command started by the dashboard receives these env vars:
  `FORGE_PROJECT_ROOT`, `FORGE_PROJECT_ID`, `FORGE_DASHBOARD_URL`.

## Top level

| Key | Type | Required | Meaning |
|---|---|---|---|
| `schema` | integer | yes | Manifest schema version. Must be `1`. |

## `[project]` (required)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Display name. |
| `id` | slug | required | Stable identifier. |
| `summary` | string, ≤140 chars | required | One-line description. |
| `kind` | `app` \| `service` \| `library` \| `cli` \| `tool` \| `game` \| `research` | required | What the project is. |
| `languages` | list of strings | required | Lowercase language names, e.g. `python`, `rust`, `typescript`. |
| `tags` | list of strings | `[]` | Free-form tags for filtering. |
| `default_branch` | string | `"main"` | Branch that CI status and standards refer to. |
| `homepage` | URL | none | Public page. |
| `docs` | URL or repo path | none | Documentation entry point. |

## `[v1]` (required)

V1 progress. Exactly one source.

| Key | Type | Meaning |
|---|---|---|
| `source` | `"milestones"` \| `"github-milestone"` | Where progress comes from. |
| `target_date` | date, optional | Planned V1 date. |
| `github_milestone` | string | Required when `source = "github-milestone"`. Progress = closed / (open + closed) issues in that GitHub milestone. |

With `source = "milestones"`, one or more `[[v1.milestones]]`:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | slug | required | |
| `title` | string | required | |
| `description` | string | none | |
| `weight` | integer ≥ 1 | `1` | Relative size. |
| `done` | boolean | `false` | |

Progress = sum of `weight` where `done` / sum of all `weight`. The "next
milestone" is the first not-done entry in file order.

## `[[checks]]` (0..n)

Local quality checks the dashboard can run and report.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | slug | required | |
| `label` | string | required | |
| `argv` | argv | required | |
| `cwd` | repo path | `"."` | |
| `env` | table of strings | `{}` | |
| `timeout_s` | integer > 0 | `900` | |
| `stage` | `commit` \| `push` \| `ci` \| `manual` | `"manual"` | Where this check lives in the project's gate chain (informational). |
| `report` | `"exit-code"` \| `"junit"` | `"exit-code"` | With `junit`, argv must contain `{report_path}`; the dashboard parses tests, failures, errors, and skipped from the JUnit XML written there. |

## `[[services]]` (0..n)

Long-running processes the dashboard can launch and supervise locally.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | slug | required | |
| `label` | string | required | |
| `description` | string | none | |
| `argv` | argv | required | May use `{port}`, `{host}`. |
| `cwd` | repo path | `"."` | |
| `env` | table of strings | `{}` | May use `{port}`, `{host}`. |
| `host` | string | `"127.0.0.1"` | Bind address passed as `{host}`. |
| `port` | table, optional | none | Port policy, below. Omit for portless services. |
| `ready` | table | `{ kind = "port" }` when `port` is set, else `{ kind = "none" }` | Readiness signal, below. |
| `open` | string, optional | none | URL path appended to `http://{host}:{port}` by the Open action. |
| `depends_on` | list of service ids | `[]` | Started first; their resolved ports are exported to this service as `FORGE_SERVICE_PORT_<ID>` and `FORGE_SERVICE_URL_<ID>` (id upper-cased, `-` → `_`). |

Port policy, discriminated by `policy`:

- `{ policy = "dynamic", preferred = 8740 }`: the dashboard picks a free port,
  trying `preferred` first, and substitutes it as `{port}`. `preferred` is
  optional.
- `{ policy = "pinned", port = 5173, evidence = "..." }`: fixed port.
  `evidence` is required and names where something outside the project
  hardcodes it (OAuth callback, external client config). Convention is not
  evidence.

Every service also receives `FORGE_SERVICE_PORT` and `FORGE_SERVICE_HOST`.

Readiness, discriminated by `kind`:

- `{ kind = "port" }`: every declared port is listening under a process in
  the service's tree.
- `{ kind = "http", path = "/api/health", expect_status = 200 }`:
  `expect_status` defaults to 200.
- `{ kind = "log", pattern = "regex" }`: a stdout/stderr line matches.
- `{ kind = "none" }`: ready as soon as it starts.

## `[[deployments]]` (0..n)

Endpoints that run elsewhere and are monitored, not launched.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | slug | required | |
| `label` | string | required | |
| `url` | URL | required | |
| `machine` | string | none | Host name or label of the machine it runs on. |
| `environment` | `production` \| `staging` \| `preview` \| `dev` | `"production"` | |
| `health` | table | none | `{ path = "/health", expect_status = 200 }`, polled over HTTP(S). |

## `[[metrics]]` (0..n)

Project-defined values shown on the dashboard with history.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | slug | required | |
| `label` | string | required | |
| `description` | string | none | |
| `unit` | string | none | Display unit, e.g. `"files"`. |
| `display` | `number` \| `percent` \| `duration` \| `bytes` \| `text` \| `status` | `"number"` | How the value renders. `percent` expects 0–100; `duration` expects seconds. |
| `interval_s` | integer ≥ 30 | `900` | Refresh period. |
| `pinned` | boolean | `false` | Shown on the overview card. |
| `thresholds` | table, optional | none | Any of `warn_below`, `warn_above`, `fail_below`, `fail_above` (numbers). Applied when the reading carries no `status`. |
| `source` | table | required | Below. |

Sources, discriminated by `kind`:

- `{ kind = "command", argv = [...], cwd = ".", timeout_s = 60 }`: stdout
  must be exactly one JSON document matching `MetricReading`. Non-zero exit
  means the reading failed; stderr is shown.
- `{ kind = "http", service = "<service id>", path = "/api/metrics/x" }`:
  GET against the running service; the body is a `MetricReading`. Reported as
  unavailable while the service is stopped.
- `{ kind = "file", path = "reports/metric.json" }`: the file holds a
  `MetricReading`.

`MetricReading`:

```json
{ "value": 1234, "status": "ok", "detail": "optional human text" }
```

`value` is a number, string, or boolean. `status` is optional, one of `ok`,
`warn`, `fail`. `detail` is optional.

## `[[actions]]` (0..n)

Project-defined operations, rendered as buttons with self-built forms.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | slug | required | |
| `label` | string | required | |
| `description` | string | none | |
| `argv` | argv | required | |
| `cwd` | repo path | `"."` | |
| `env` | table of strings | `{}` | |
| `timeout_s` | integer > 0 | `3600` | |
| `params` | JSON Schema object, optional | none | `type = "object"` schema for the action's input, written as TOML. The dashboard builds the form from it, validates input against it, and writes the params as one JSON document to the command's stdin. Without `params`, stdin is empty. |
| `confirm` | boolean | `false` | Ask before running. |
| `danger` | boolean | `false` | Destructive styling and typed confirmation. |
| `requires_service` | service id, optional | none | Must be running; its URL is exported as `FORGE_SERVICE_URL_<ID>`. |

Stdout and stderr stream into the job log. The exit code decides success.
Optionally, the action writes an `ActionResult` JSON document to the path in
env `FORGE_ACTION_RESULT`:

```json
{ "summary": "Re-indexed 1,204 files", "links": [{ "label": "Report", "url": "http://..." }], "artifacts": ["reports/index.json"] }
```

## `[launch]` (optional)

| Key | Type | Meaning |
|---|---|---|
| `instructions` | string | Markdown, or a repo-relative path to a `.md` file. Shown as the project's launch and interface instructions. |
| `primary_service` | service id | The service the Launch button starts (with its dependencies) and opens. |

## `[forge]` (optional, written by tools)

| Key | Type | Meaning |
|---|---|---|
| `template_revision` | string | forge commit the repo's standards were last aligned to. |
| `aligned_at` | datetime | When that alignment happened. |

## What the manifest does not hold

User-managed state lives in the dashboard, keyed by `project.id`: status
marker, notes, favorites, schedules, fork-sync policy. Git, fork, CI, and
issue information is derived from the repository and GitHub, never declared.

## Gate hook-id conventions

Standards checks identify gates by `.pre-commit-config.yaml` hook id. Beyond
forge's base hooks, use these ids per language so gates stay identifiable
across projects:

| Language | Commit-stage hook ids |
|---|---|
| python | `ruff-format`, `ruff-check`, `mypy` |
| rust | `cargo-fmt`, `cargo-clippy` |
| typescript | `biome`, `tsc` |

The push stage keeps the single hook id `tests-and-diff-coverage`, covering
every language's tests and the ≥90% changed-line coverage threshold.

## Example

```toml
schema = 1

[project]
name = "Reelcut"
id = "reelcut"
summary = "Turns raw recordings into short edited reels"
kind = "app"
languages = ["python", "rust", "typescript"]
tags = ["video", "ml"]

[v1]
source = "milestones"

[[v1.milestones]]
id = "library"
title = "Library scan, probe, proxies"
weight = 2
done = true

[[checks]]
id = "tests"
label = "Python tests"
argv = ["uv", "run", "pytest", "-q", "--junitxml", "{report_path}"]
report = "junit"
stage = "push"

[[services]]
id = "app"
label = "Reelcut"
argv = ["uv", "run", "reelcut", "serve", "--host", "{host}", "--port", "{port}"]
port = { policy = "dynamic", preferred = 8740 }
ready = { kind = "http", path = "/api/health" }
open = "/"

[[metrics]]
id = "indexed-media"
label = "Indexed media"
unit = "files"
pinned = true
source = { kind = "command", argv = ["uv", "run", "reelcut", "metrics", "indexed-media"] }

[[actions]]
id = "reindex"
label = "Re-index library"
argv = ["uv", "run", "reelcut", "index", "--params-stdin"]

[actions.params]
type = "object"
properties.full = { type = "boolean", default = false, title = "Full rebuild" }

[launch]
instructions = "docs/LAUNCH.md"
primary_service = "app"
```
