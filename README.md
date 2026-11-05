# forge dashboard

A local control room for every project created with
[forge](../Tests/forge). It is under construction; see `docs/SPEC.md` for the
specification and `forge.project.toml` for V1 progress.

## Setup

```sh
uv sync --group dev
bun install --cwd web
uv run pre-commit install --install-hooks -t pre-commit -t pre-push
```

Run all gates manually:

```sh
uv run pre-commit run --all-files
bash tools/stack_coverage_gate.sh
```
