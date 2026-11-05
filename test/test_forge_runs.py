"""forge runs: root discovery, step manifests, ordering, output windows, and path safety."""

import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pytest
from pydantic import ValidationError
from testing_support import wait_for

from forge_dashboard.core.paths import DataPaths
from forge_dashboard.core.runtime import Runtime
from forge_dashboard.core.service import ActionError, NoInput
from forge_dashboard.services import forge_runs as forge_runs_module
from forge_dashboard.services.discovery import DiscoveryService
from forge_dashboard.services.forge_runs import (
    LONGEST_COMMAND_CHARS,
    ForgeRunsConfig,
    ForgeRunsService,
    ForgeRunsState,
    ForgeTomlError,
    StepManifest,
    StepOutputRequest,
    StepRef,
    locate_run_roots,
    read_output_lines,
    run_project_dirs,
    scan_runs,
    summarize_command,
    workspace_root,
)
from forge_dashboard.services.jobs import JobsService
from forge_dashboard.services.manifests import ManifestsService
from forge_dashboard.services.projects import ProjectsService

ORIGIN = datetime(2026, 9, 5, 7, 0, tzinfo=UTC)
PROMPT = "# Anime hair brawler\n\nI want to make a small 3D action game in the browser. " * 40


def epoch(minutes: float) -> float:
    return (ORIGIN + timedelta(minutes=minutes)).timestamp()


def write_step(
    runs_root: Path,
    project: str,
    index: int,
    name: str,
    *,
    minutes: float = 0.0,
    duration_s: float = 60.0,
    exit_code: int | None = 0,
    timed_out: bool = False,
    stdout: bytes | None = b"built it\n",
    stderr: bytes | None = b"",
    workspace_exists: bool = True,
) -> str:
    """Write a step the way forge's runner does and return its file stem."""
    stem = f"step-{index:02d}-{name}"
    meta = runs_root / project / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    workspace = runs_root / project / stem
    if workspace_exists:
        workspace.mkdir(exist_ok=True)
    manifest = {
        "project": project,
        "step_index": index,
        "step_name": name,
        "prompt_file": str(runs_root.parent / "prompts" / f"{project}.md"),
        "harness": "astra",
        "command": ["claude", "--model", "gpt-6-astra(high)", "-p", PROMPT, "--permission-mode", "acceptEdits"],
        "workspace": str(workspace),
        "started_at": (ORIGIN + timedelta(minutes=minutes)).isoformat(),
        "duration_s": duration_s,
        "exit_code": exit_code,
        "timed_out": timed_out,
    }
    (meta / f"{stem}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if stdout is not None:
        (meta / f"{stem}.stdout.txt").write_bytes(stdout)
    if stderr is not None:
        (meta / f"{stem}.stderr.txt").write_bytes(stderr)
    return stem


class Stack(NamedTuple):
    runs: ForgeRunsService
    projects: ProjectsService
    runtime: Runtime


async def open_stack(data_paths: DataPaths, tmp_path: Path, *, runs_before_start: bool) -> Stack:
    runtime = Runtime(data_paths)
    jobs = JobsService(runtime)
    discovery = DiscoveryService(runtime, jobs)
    manifests = ManifestsService(runtime)
    projects = ProjectsService(runtime, discovery, manifests)
    runs = ForgeRunsService(runtime, projects)
    # Discovery starts last so every project listener is in place before it publishes.
    runtime.register([jobs, manifests, projects, *([runs] if runs_before_start else []), discovery])
    await runtime.open()
    await runtime.db.kv_put("config", "discovery", {"roots": [str(tmp_path)], "max_depth": 4})
    await runtime.start()
    await wait_for(lambda: projects.state.loaded)
    if not runs_before_start:
        runtime.register([runs])  # after start, so no background loop races the test's own refreshes
    return Stack(runs, projects, runtime)


@pytest.fixture
async def stack(data_paths: DataPaths, tmp_path: Path, forge_repo: Path) -> AsyncIterator[Stack]:
    opened = await open_stack(data_paths, tmp_path, runs_before_start=False)
    try:
        yield opened
    finally:
        await opened.runtime.stop()


@pytest.fixture
def runs_root(forge_repo: Path) -> Path:
    """forge.toml's `workspace_root = "runs"`, relative to the forge repo."""
    return forge_repo / "runs"


@pytest.fixture
def sibling_root(forge_repo: Path) -> Path:
    """A directory beside the forge repo with the same layout, like `Tests/forge-runs`."""
    return forge_repo.parent / "forge-runs"


@pytest.fixture
def populated(runs_root: Path, sibling_root: Path) -> dict[str, str]:
    """alpha under forge.toml's root, hair-brawler beside the forge repo; returns the stems by name."""
    stems = {
        "initial": write_step(runs_root, "alpha", 0, "initial", minutes=0, duration_s=600, stderr=b"warn\n"),
        "follow-up": write_step(
            runs_root, "alpha", 1, "follow-up-1", minutes=20, duration_s=3600, exit_code=None, timed_out=True
        ),
        "polish": write_step(
            runs_root, "alpha", 2, "polish", minutes=90, duration_s=30, exit_code=1, workspace_exists=False
        ),
        "hb-initial": write_step(sibling_root, "hair-brawler", 0, "initial", minutes=300, duration_s=3000),
        "hb-fix2": write_step(
            sibling_root, "hair-brawler", 0, "initial-fix2", minutes=340, duration_s=1000, exit_code=1
        ),
        "hb-fix1": write_step(
            sibling_root, "hair-brawler", 0, "initial-fix1", minutes=310, duration_s=1900, exit_code=1
        ),
    }
    (sibling_root / "fixes").mkdir()
    (sibling_root / "stray-file.txt").write_text("not a run", encoding="utf-8")
    return stems


# --- reading forge.toml ---


def test_the_workspace_root_is_read_from_forge_toml_relative_to_the_forge_repo(tmp_path: Path) -> None:
    (tmp_path / "forge.toml").write_text('[runner]\nworkspace_root = "../forge-runs"\n', encoding="utf-8")
    assert workspace_root(tmp_path) == (tmp_path.parent / "forge-runs").resolve()


def test_an_absolute_workspace_root_is_used_as_written(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    (tmp_path / "forge.toml").write_text(f"[runner]\nworkspace_root = {json.dumps(str(elsewhere))}\n", encoding="utf-8")
    assert workspace_root(tmp_path) == elsewhere.resolve()


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        (None, "not found"),
        ("[runner\nworkspace_root = 1", "not valid TOML"),
        ('default_harness = "claude"\n', "no [runner] table"),
        ('[runner]\ndefault_harness = "claude"\n', "workspace_root must be a non-empty string"),
        ("[runner]\nworkspace_root = 5\n", "workspace_root must be a non-empty string"),
        ('[runner]\nworkspace_root = ""\n', "workspace_root must be a non-empty string"),
    ],
)
def test_a_forge_toml_that_cannot_place_runs_says_why(tmp_path: Path, text: str | None, complaint: str) -> None:
    if text is not None:
        (tmp_path / "forge.toml").write_text(text, encoding="utf-8")
    with pytest.raises(ForgeTomlError, match=re.escape(complaint)):
        workspace_root(tmp_path)


# --- finding runs ---


async def test_runs_are_found_under_forge_tomls_root_and_in_sibling_directories(
    stack: Stack, populated: dict[str, str], runs_root: Path, sibling_root: Path
) -> None:
    state = await stack.runs.refresh(NoInput())
    assert state is stack.runs.state
    assert state.forge_repo == str(stack.projects.forge_repo) and state.notice is None
    assert [(root.path, root.origin, root.exists, root.projects) for root in state.roots] == [
        (str(runs_root.resolve()), "forge-toml", True, 1),
        (str(sibling_root.resolve()), "sibling", True, 1),
    ]
    assert [(run.name, run.root) for run in state.runs] == [
        ("hair-brawler", str(sibling_root.resolve())),
        ("alpha", str(runs_root.resolve())),
    ]
    assert state.refreshed_at is not None


async def test_steps_come_in_run_order_even_when_they_share_an_index(stack: Stack, populated: dict[str, str]) -> None:
    state = await stack.runs.refresh(NoInput())
    runs = {run.name: run for run in state.runs}
    assert [step.stem for step in runs["alpha"].steps] == [
        populated["initial"],
        populated["follow-up"],
        populated["polish"],
    ]
    assert [step.stem for step in runs["hair-brawler"].steps] == [
        populated["hb-initial"],
        populated["hb-fix1"],
        populated["hb-fix2"],
    ]
    assert [step.name for step in runs["hair-brawler"].steps] == ["initial", "initial-fix1", "initial-fix2"]


async def test_each_step_reports_its_outcome_timing_and_files(stack: Stack, populated: dict[str, str]) -> None:
    state = await stack.runs.refresh(NoInput())
    alpha = next(run for run in state.runs if run.name == "alpha")
    initial, timed_out, failed = alpha.steps
    assert (initial.ok, initial.exit_code, initial.timed_out) == (True, 0, False)
    assert (timed_out.ok, timed_out.exit_code, timed_out.timed_out) == (False, None, True)
    assert (failed.ok, failed.exit_code, failed.timed_out) == (False, 1, False)
    assert (initial.started_at, initial.duration_s, initial.harness) == (epoch(0), 600, "astra")
    assert (initial.stdout_bytes, initial.stderr_bytes) == (len(b"built it\n"), len(b"warn\n"))
    assert initial.workspace_exists and not failed.workspace_exists
    assert (alpha.total_duration_s, alpha.last_activity) == (600 + 3600 + 30, epoch(90) + 30)


async def test_a_step_without_captured_files_reports_them_as_absent_not_empty(stack: Stack, runs_root: Path) -> None:
    write_step(runs_root, "beta", 0, "initial", stdout=None, stderr=b"")
    state = await stack.runs.refresh(NoInput())
    [step] = state.runs[0].steps
    assert (step.stdout_bytes, step.stderr_bytes) == (None, 0)


async def test_state_never_carries_prompt_text(stack: Stack, populated: dict[str, str]) -> None:
    state = await stack.runs.refresh(NoInput())
    step = state.runs[0].steps[0]
    assert step.command.startswith("claude --model gpt-6-astra(high) -p <") and "chars>" in step.command
    assert "Anime hair brawler" not in state.model_dump_json()


async def test_manifests_that_are_not_step_manifests_are_listed_as_unreadable(stack: Stack, runs_root: Path) -> None:
    good = write_step(runs_root, "alpha", 0, "initial")
    meta = runs_root / "alpha" / "meta"
    (meta / "step-01-truncated.json").write_text('{"project": "alpha", "step_in', encoding="utf-8")
    (meta / "step-02-incomplete.json").write_text(json.dumps({"project": "alpha", "step_index": 2}), encoding="utf-8")
    (meta / "notes.json").write_text("{}", encoding="utf-8")  # not a step file at all
    state = await stack.runs.refresh(NoInput())
    [run] = state.runs
    assert [step.stem for step in run.steps] == [good]
    assert [problem.stem for problem in run.unreadable] == ["step-01-truncated", "step-02-incomplete"]
    assert "Invalid JSON" in run.unreadable[0].problem and run.unreadable[1].problem.startswith("step_name:")


async def test_extra_roots_are_read_and_a_directory_found_twice_keeps_its_first_origin(
    stack: Stack, populated: dict[str, str], tmp_path: Path, sibling_root: Path
) -> None:
    archive = tmp_path / "archive"
    write_step(archive, "old-experiment", 0, "initial", minutes=-5000)
    missing = tmp_path / "not-created-yet"
    await stack.runs.update_config(ForgeRunsConfig(extra_roots=[str(archive), str(missing), str(sibling_root)]))
    state = stack.runs.state  # a config change refreshes on its own
    assert [(Path(root.path).name, root.origin, root.exists, root.projects) for root in state.roots] == [
        ("runs", "forge-toml", True, 1),
        ("archive", "configured", True, 1),
        ("forge-runs", "configured", True, 1),
        ("not-created-yet", "configured", False, 0),
    ]
    assert [run.name for run in state.runs] == ["hair-brawler", "alpha", "old-experiment"]


def test_extra_roots_must_be_absolute_paths() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        ForgeRunsConfig(extra_roots=["relative/runs"])


def test_without_a_forge_repo_the_state_says_so_and_extra_roots_still_work(tmp_path: Path) -> None:
    write_step(tmp_path / "archive", "old", 0, "initial")
    state = scan_runs(None, [str(tmp_path / "archive")])
    assert state.forge_repo is None and state.notice is not None and "forge repo was not found" in state.notice
    assert [(root.origin, root.projects) for root in state.roots] == [("configured", 1)]
    assert [run.name for run in state.runs] == ["old"]


def test_a_broken_forge_toml_is_reported_while_sibling_runs_are_still_shown(
    forge_repo: Path, sibling_root: Path
) -> None:
    write_step(sibling_root, "hair-brawler", 0, "initial")
    (forge_repo / "forge.toml").write_text("[runner\n", encoding="utf-8")
    location = locate_run_roots(forge_repo, [])
    assert location.notice is not None and "not valid TOML" in location.notice
    assert list(location.roots.values()) == ["sibling"]


def test_the_forge_repo_itself_is_never_a_sibling_run_root(forge_repo: Path) -> None:
    write_step(forge_repo, "inside", 0, "initial")  # forge/<project>/meta looks like a run root layout
    assert "sibling" not in locate_run_roots(forge_repo, []).roots.values()


async def test_run_folders_added_later_appear_on_the_next_refresh(stack: Stack, runs_root: Path) -> None:
    assert (await stack.runs.refresh(NoInput())).runs == []
    write_step(runs_root, "gamma", 0, "initial")
    assert [run.name for run in (await stack.runs.refresh(NoInput())).runs] == ["gamma"]


def test_a_folder_that_cannot_be_listed_holds_no_runs_and_does_not_hide_the_others(
    sibling_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_step(sibling_root, "readable", 0, "initial")
    write_step(sibling_root, "locked", 0, "initial")
    original = Path.glob

    def deny_locked(self: Path, pattern: str, *, case_sensitive: bool | None = None) -> Iterator[Path]:
        if self.parent.name == "locked":
            raise PermissionError(13, "Access is denied")
        return original(self, pattern, case_sensitive=case_sensitive)

    monkeypatch.setattr(Path, "glob", deny_locked)
    assert [directory.name for directory in run_project_dirs(sibling_root)] == ["readable"]


# --- the command digest ---


def test_a_command_keeps_its_flags_and_loses_its_prompt() -> None:
    argv = ["claude", "--model", "opus", "-p", "x" * 49, "--append-system-prompt", "line one\nline two", "short"]
    assert summarize_command(argv) == "claude --model opus -p <49 chars> --append-system-prompt <17 chars> short"
    assert summarize_command(["claude", "y" * 48]) == f"claude {'y' * 48}"
    assert summarize_command([]) == ""


def test_a_command_digest_is_one_bounded_line() -> None:
    digest = summarize_command(["tool", *[f"--flag-number-{n}" for n in range(60)]])
    assert len(digest) == LONGEST_COMMAND_CHARS and digest.endswith("…")


# --- step actions ---


async def test_step_manifest_returns_the_whole_manifest_with_the_prompt(
    stack: Stack, populated: dict[str, str], runs_root: Path
) -> None:
    await stack.runs.refresh(NoInput())
    manifest = await stack.runs.step_manifest(
        StepRef(root=str(runs_root.resolve()), project="alpha", step=populated["follow-up"])
    )
    assert isinstance(manifest, StepManifest)
    assert (manifest.project, manifest.step_index, manifest.step_name) == ("alpha", 1, "follow-up-1")
    assert (manifest.exit_code, manifest.timed_out, manifest.ok) == (None, True, False)
    assert manifest.command[4] == PROMPT and manifest.started_at == ORIGIN + timedelta(minutes=20)


async def test_step_manifest_of_an_unreadable_file_is_a_clear_error(stack: Stack, runs_root: Path) -> None:
    write_step(runs_root, "alpha", 0, "initial")
    (runs_root / "alpha" / "meta" / "step-01-bad.json").write_text("{}", encoding="utf-8")
    await stack.runs.refresh(NoInput())
    with pytest.raises(ActionError, match="not a step manifest") as raised:
        await stack.runs.step_manifest(StepRef(root=str(runs_root.resolve()), project="alpha", step="step-01-bad"))
    assert raised.value.status == 422


async def test_step_output_returns_a_window_of_lines_and_the_total(stack: Stack, runs_root: Path) -> None:
    body = "".join(f"line {n}\n" for n in range(10)).encode()
    stem = write_step(runs_root, "alpha", 0, "initial", stdout=body)
    await stack.runs.refresh(NoInput())
    root = str(runs_root.resolve())
    window = await stack.runs.step_output(StepOutputRequest(root=root, project="alpha", step=stem, offset=3, limit=4))
    assert (window.which, window.lines, window.offset, window.total_lines, window.size_bytes) == (
        "stdout",
        ["line 3", "line 4", "line 5", "line 6"],
        3,
        10,
        len(body),
    )
    tail = await stack.runs.step_output(StepOutputRequest(root=root, project="alpha", step=stem, offset=8, limit=500))
    assert tail.lines == ["line 8", "line 9"] and tail.total_lines == 10
    beyond = await stack.runs.step_output(StepOutputRequest(root=root, project="alpha", step=stem, offset=50))
    assert beyond.lines == [] and beyond.total_lines == 10


async def test_step_output_reads_stderr_when_asked(stack: Stack, runs_root: Path) -> None:
    stem = write_step(runs_root, "alpha", 0, "initial", stdout=b"out\n", stderr=b"boom\nbang")
    await stack.runs.refresh(NoInput())
    errors = await stack.runs.step_output(
        StepOutputRequest(root=str(runs_root.resolve()), project="alpha", step=stem, which="stderr")
    )
    assert (errors.which, errors.lines, errors.total_lines) == ("stderr", ["boom", "bang"], 2)


def test_output_lines_tolerate_crlf_invalid_bytes_and_empty_files(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"
    path.write_bytes(b"windows\r\nbroken \xff byte\r\n\r\nlast")
    result = read_output_lines(path, "stdout", 0, 10)
    assert result.lines == ["windows", "broken \ufffd byte", "", "last"] and result.total_lines == 4
    path.write_bytes(b"")
    assert (
        read_output_lines(path, "stderr", 0, 10).lines == []
        and read_output_lines(path, "stderr", 0, 10).total_lines == 0
    )


async def test_step_output_without_a_captured_file_is_not_found(stack: Stack, runs_root: Path) -> None:
    stem = write_step(runs_root, "alpha", 0, "initial", stdout=None)
    await stack.runs.refresh(NoInput())
    with pytest.raises(ActionError, match="no such file") as raised:
        await stack.runs.step_output(StepOutputRequest(root=str(runs_root.resolve()), project="alpha", step=stem))
    assert raised.value.status == 404


# --- path safety ---


@pytest.fixture
async def secured(stack: Stack, runs_root: Path, tmp_path: Path) -> tuple[Stack, str, str]:
    stem = write_step(runs_root, "alpha", 0, "initial")
    (tmp_path / "secret.stdout.txt").write_text("outside the run root\n", encoding="utf-8")
    (tmp_path / "secret.json").write_text("{}", encoding="utf-8")
    await stack.runs.refresh(NoInput())
    return stack, str(runs_root.resolve()), stem


async def test_roots_that_are_not_known_run_roots_are_refused(
    secured: tuple[Stack, str, str], tmp_path: Path, forge_repo: Path
) -> None:
    stack, _root, stem = secured
    for foreign in (tmp_path, forge_repo, tmp_path / "nowhere", tmp_path.parent):
        with pytest.raises(ActionError, match="not a known run root") as raised:
            await stack.runs.step_output(StepOutputRequest(root=str(foreign), project="alpha", step=stem))
        assert raised.value.status == 404


@pytest.mark.parametrize(
    "project", ["..", ".", "", "alpha/..", "../alpha", "alpha/meta", "C:", "C:\\Windows", "a\x00b"]
)
async def test_project_names_that_could_leave_the_root_are_refused(
    secured: tuple[Stack, str, str], project: str
) -> None:
    stack, root, stem = secured
    with pytest.raises(ActionError, match="invalid project") as raised:
        await stack.runs.step_output(StepOutputRequest(root=root, project=project, step=stem))
    assert raised.value.status == 400


@pytest.mark.parametrize(
    "step", ["..", "", "../../secret", "../secret", "step-00-initial/..", "secret:stream", "/etc/passwd"]
)
async def test_step_names_that_could_leave_the_meta_folder_are_refused(
    secured: tuple[Stack, str, str], step: str
) -> None:
    stack, root, _stem = secured
    with pytest.raises(ActionError, match="invalid step") as output_error:
        await stack.runs.step_output(StepOutputRequest(root=root, project="alpha", step=step))
    with pytest.raises(ActionError, match="invalid step") as manifest_error:
        await stack.runs.step_manifest(StepRef(root=root, project="alpha", step=step))
    assert (output_error.value.status, manifest_error.value.status) == (400, 400)


async def test_a_link_inside_meta_that_leads_outside_the_root_is_refused(
    secured: tuple[Stack, str, str], tmp_path: Path
) -> None:
    stack, root, _stem = secured
    link = Path(root) / "alpha" / "meta" / "step-09-linked.stdout.txt"
    try:
        link.symlink_to(tmp_path / "secret.stdout.txt")
    except OSError:
        pytest.skip("this account may not create symbolic links")
    with pytest.raises(ActionError, match="leaves the run root") as raised:
        await stack.runs.step_output(StepOutputRequest(root=root, project="alpha", step="step-09-linked"))
    assert raised.value.status == 400


async def test_a_root_spelled_with_parent_references_still_names_the_known_root(
    secured: tuple[Stack, str, str],
) -> None:
    stack, root, stem = secured
    spelled = str(Path(root) / "alpha" / "..")
    result = await stack.runs.step_output(StepOutputRequest(root=spelled, project="alpha", step=stem))
    assert result.lines == ["built it"]


# --- lifecycle ---


async def test_the_forge_repo_found_after_start_triggers_a_refresh(
    data_paths: DataPaths, tmp_path: Path, forge_repo: Path, runs_root: Path, make_project: Callable[..., Path]
) -> None:
    write_step(runs_root, "alpha", 0, "initial")
    make_project("Unrelated")  # announced to the service too, and ignored
    opened = await open_stack(data_paths, tmp_path, runs_before_start=True)
    try:
        # The 60 s interval cannot explain this: only the registry announcing the forge repo can.
        await wait_for(lambda: [run.name for run in opened.runs.state.runs] == ["alpha"])
        assert opened.runs.state.forge_repo == str(opened.projects.forge_repo)
    finally:
        await opened.runtime.stop()


async def test_a_failing_refresh_is_logged_and_the_loop_survives(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, runs_root: Path
) -> None:
    calls: list[int] = []
    real_scan = forge_runs_module.scan_runs

    def flaky(forge_repo: Path | None, extra_roots: Sequence[str]) -> ForgeRunsState:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk on fire")
        return real_scan(forge_repo, extra_roots)

    monkeypatch.setattr(forge_runs_module, "scan_runs", flaky)
    stack.runs.config = ForgeRunsConfig(interval_s=1)
    write_step(runs_root, "alpha", 0, "initial")
    with caplog.at_level(logging.ERROR, logger="forge_dashboard"):
        await stack.runs.start()
        try:
            await wait_for(lambda: [run.name for run in stack.runs.state.runs] == ["alpha"])
        finally:
            await stack.runs.stop()
    assert "forge runs refresh failed" in caplog.text and len(calls) >= 2


def test_state_and_config_schemas_carry_the_display_hints_the_ui_renders_from() -> None:
    steps = forge_runs_module.StepSummary.model_json_schema()["properties"]
    assert steps["ok"]["x-ui"]["format"] == "badge" and steps["ok"]["x-ui"]["tones"] == {"true": "good", "false": "bad"}
    assert steps["stdout_bytes"]["x-ui"]["format"] == "bytes"
    state = ForgeRunsState.model_json_schema()["properties"]
    assert state["runs"]["x-ui"]["columns"] and state["roots"]["x-ui"]["columns"]
    config = ForgeRunsConfig.model_json_schema()["properties"]
    assert config["interval_s"]["default"] == 60 and config["extra_roots"]["x-ui"]["widget"] == "path-dir"


def test_actions_are_a_refresh_and_two_read_only_queries() -> None:
    kinds = {action_id: meta.query for action_id, meta, _input, _output in ForgeRunsService.action_specs()}
    assert kinds == {"refresh": False, "step-manifest": True, "step-output": True}
