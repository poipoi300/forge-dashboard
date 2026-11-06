"""Harness argv and stdin rendering: placeholders are filled once, and only from the template."""

import pytest

from forge_dashboard.domain import harness
from forge_dashboard.domain.harness import HarnessConfig, RunKinds, RunKindSettings

LOOKS_LIKE_PLACEHOLDERS = "Rename {model} to {prompt_path}, keep {unknown} and {{braces}} as they are."


def test_placeholders_inside_the_prompt_text_are_not_substituted() -> None:
    config = HarnessConfig(
        command=["agent", "{prompt}", "--file", "{prompt_path}"],
        stdin="{prompt}",
        model_args=["--model={model}", "--note={prompt}"],
    )

    argv = harness.render_argv(config, RunKindSettings(model="haiku"), LOOKS_LIKE_PLACEHOLDERS, "/runs/p.md")

    assert argv[:4] == ["agent", LOOKS_LIKE_PLACEHOLDERS, "--file", "/runs/p.md"]
    assert argv[4:6] == ["--model=haiku", f"--note={LOOKS_LIKE_PLACEHOLDERS}"]
    assert harness.render_stdin(config, LOOKS_LIKE_PLACEHOLDERS, "/runs/p.md") == LOOKS_LIKE_PLACEHOLDERS.encode()


def test_a_value_that_names_another_value_is_still_inserted_as_written() -> None:
    config = HarnessConfig(command=["agent", "{prompt}", "{prompt_path}"], stdin="{prompt_path}|{prompt}")

    argv = harness.render_argv(config, RunKindSettings(), "{prompt_path}", "{prompt}")

    assert argv[:3] == ["agent", "{prompt_path}", "{prompt}"]
    assert harness.render_stdin(config, "{prompt_path}", "{prompt}") == b"{prompt}|{prompt_path}"


def test_braces_that_are_not_known_names_stay_in_a_template() -> None:
    config = HarnessConfig(stdin='{"task": "{prompt}", "extra": {unknown}, "empty": {}}')

    assert harness.render_stdin(config, "fix it", "/p") == b'{"task": "fix it", "extra": {unknown}, "empty": {}}'


def test_a_stored_config_that_still_names_an_output_mode_loads() -> None:
    config = HarnessConfig.model_validate({"output": "text", "command": ["agent"]})

    assert config.command == ["agent"]
    assert "output" not in HarnessConfig.model_json_schema()["properties"]


@pytest.mark.parametrize("stdin", [None, ""])
def test_no_stdin_template_sends_nothing(stdin: str | None) -> None:
    assert harness.render_stdin(HarnessConfig(stdin=stdin), "prompt", "/p") is None


def test_issue_driven_runs_may_run_project_tools_but_not_arbitrary_commands() -> None:
    kinds = RunKinds()

    for settings in (kinds.delegate, kinds.update_deps):
        assert "Bash" not in settings.allowed_tools
        assert "Bash(uv run:*)" in settings.allowed_tools and "Bash(bun test:*)" in settings.allowed_tools
        assert not any(tool.startswith("Bash(git") for tool in settings.allowed_tools)
        assert {"WebFetch", "WebSearch"} <= set(settings.disallowed_tools)
    assert not any(tool.startswith("Bash") for tool in kinds.fork_sync.allowed_tools)
