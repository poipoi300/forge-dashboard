"""The manifest contract: what validates, what fails, and where errors point."""

import re
from pathlib import Path

import pytest

from forge_dashboard.manifest import MetricReading, load_manifest, parse_manifest_text
from forge_dashboard.manifest.models import HttpReady, NoReady, PortReady
from forge_dashboard.manifest.placeholders import render

REPO_ROOT = Path(__file__).resolve().parent.parent

MINIMAL = """\
schema = 1

[project]
name = "Demo"
id = "demo"
summary = "A demo project"
kind = "app"
languages = ["python"]

[v1]
source = "milestones"

[[v1.milestones]]
id = "first"
title = "First milestone"
"""


def _issues(text: str) -> list[tuple[int | None, str, str]]:
    result = parse_manifest_text(text)
    assert result.manifest is None, "expected the manifest to be invalid"
    return [(issue.line, issue.location, issue.message) for issue in result.issues]


def _line_of(text: str, needle: str, occurrence: int = 1) -> int:
    """1-based number of the `occurrence`-th line containing `needle`."""
    hits = [number for number, line in enumerate(text.splitlines(), start=1) if needle in line]
    return hits[occurrence - 1]


def _doc_example() -> str:
    doc = (REPO_ROOT / "docs" / "project-manifest.md").read_text(encoding="utf-8")
    match = re.search(r"## Example\n\n```toml\n(?P<body>.*?)```", doc, flags=re.DOTALL)
    assert match is not None
    return match["body"]


def test_documented_example_validates() -> None:
    result = parse_manifest_text(_doc_example())
    assert result.issues == []
    assert result.manifest is not None
    assert result.manifest.project.id == "reelcut"


def test_dashboard_dogfoods_a_valid_manifest() -> None:
    result = load_manifest(REPO_ROOT)
    assert result.issues == []


def test_minimal_manifest_applies_documented_defaults() -> None:
    manifest = parse_manifest_text(MINIMAL).manifest
    assert manifest is not None
    assert manifest.project.default_branch == "main"
    assert manifest.project.tags == []
    assert manifest.v1.milestones[0].weight == 1
    assert manifest.v1.milestones[0].done is False
    assert manifest.checks == [] and manifest.services == [] and manifest.launch is None


def test_unknown_key_fails_with_its_line() -> None:
    text = MINIMAL.replace('kind = "app"', 'kind = "app"\nnmae = "typo"')
    assert _issues(text) == [(_line_of(text, "nmae"), "project.nmae", "Extra inputs are not permitted")]


def test_missing_required_key_points_at_its_table() -> None:
    text = MINIMAL.replace('summary = "A demo project"\n', "")
    [(line, location, message)] = _issues(text)
    assert (line, location) == (3, "project.summary")
    assert message == "Field required"


def test_schema_version_must_be_one() -> None:
    [(line, location, _message)] = _issues(MINIMAL.replace("schema = 1", "schema = 2"))
    assert (line, location) == (1, "schema")


@pytest.mark.parametrize("bad_id", ["Demo", "-demo", "demo_x", "dé"])
def test_ids_must_be_slugs(bad_id: str) -> None:
    [(line, location, _message)] = _issues(MINIMAL.replace('id = "demo"', f'id = "{bad_id}"'))
    assert (line, location) == (5, "project.id")


def test_summary_is_limited_to_140_chars() -> None:
    [(_line, location, _message)] = _issues(MINIMAL.replace("A demo project", "x" * 141))
    assert location == "project.summary"


def test_languages_must_be_lowercase() -> None:
    [(_line, location, message)] = _issues(MINIMAL.replace('["python"]', '["Python"]'))
    assert location == "project.languages[0]"
    assert "lowercase" in message


@pytest.mark.parametrize(
    ("cwd", "fragment"),
    [("/abs", "absolute"), ("C:/abs", "absolute"), ("a/../b", "'..'"), ("a\\\\b", "forward slashes"), ("", "empty")],
)
def test_repo_paths_reject_escapes(cwd: str, fragment: str) -> None:
    text = MINIMAL + f'\n[[checks]]\nid = "t"\nlabel = "T"\nargv = ["x"]\ncwd = "{cwd}"\n'
    [(line, location, message)] = _issues(text)
    assert (line, location) == (_line_of(text, "cwd ="), "checks[0].cwd")
    assert fragment in message


def test_junit_checks_require_report_path() -> None:
    text = MINIMAL + '\n[[checks]]\nid = "t"\nlabel = "T"\nargv = ["pytest"]\nreport = "junit"\n'
    [(line, location, message)] = _issues(text)
    assert (line, location) == (_line_of(text, "argv ="), "checks[0].argv")
    assert "{report_path}" in message


def test_report_path_is_rejected_outside_junit_checks() -> None:
    text = MINIMAL + '\n[[checks]]\nid = "t"\nlabel = "T"\nargv = ["pytest", "{report_path}"]\n'
    [(_line, location, message)] = _issues(text)
    assert location == "checks[0].argv"
    assert "{report_path}" in message and "allowed here: {project_root}" in message


def test_literal_braces_and_project_root_are_accepted() -> None:
    text = MINIMAL + '\n[[checks]]\nid = "t"\nlabel = "T"\nargv = ["echo", "{{json}}", "{project_root}/x"]\n'
    manifest = parse_manifest_text(text).manifest
    assert manifest is not None
    rendered = [render(part, {"project_root": "/repo"}) for part in manifest.checks[0].argv]
    assert rendered == ["echo", "{json}", "/repo/x"]


def test_malformed_placeholder_is_rejected() -> None:
    text = MINIMAL + '\n[[checks]]\nid = "t"\nlabel = "T"\nargv = ["echo", "{oops"]\n'
    [(_line, location, _message)] = _issues(text)
    assert location == "checks[0].argv"


def test_env_placeholders_are_checked_per_key() -> None:
    text = MINIMAL + '\n[[checks]]\nid = "t"\nlabel = "T"\nargv = ["x"]\nenv = { PORT = "{port}" }\n'
    [(line, location, _message)] = _issues(text)
    assert (line, location) == (_line_of(text, "env ="), "checks[0].env.PORT")


SERVICE = '\n[[services]]\nid = "app"\nlabel = "App"\nargv = ["serve", "--port", "{port}", "--host", "{host}"]\n'


def test_service_defaults_and_port_readiness() -> None:
    text = MINIMAL + SERVICE + 'port = { policy = "dynamic", preferred = 8740 }\n'
    manifest = parse_manifest_text(text).manifest
    assert manifest is not None
    service = manifest.service("app")
    assert service.host == "127.0.0.1"
    assert isinstance(service.effective_ready, PortReady)


def test_portless_service_defaults_to_no_readiness_and_rejects_port_placeholder() -> None:
    [(line, location, message)] = _issues(MINIMAL + SERVICE)
    assert (line, location) == (_line_of(MINIMAL + SERVICE, "argv ="), "services[0].argv")
    assert "{port}" in message
    manifest = parse_manifest_text(MINIMAL + '\n[[services]]\nid = "w"\nlabel = "W"\nargv = ["work"]\n').manifest
    assert manifest is not None
    assert isinstance(manifest.services[0].effective_ready, NoReady)


def test_pinned_port_requires_evidence() -> None:
    text = MINIMAL + SERVICE + 'port = { policy = "pinned", port = 5173 }\n'
    [(line, location, message)] = _issues(text)
    assert (line, location) == (_line_of(text, "port ="), "services[0].port.evidence")
    assert message == "Field required"


def test_unknown_port_policy_is_reported_at_the_port_key() -> None:
    text = MINIMAL + SERVICE + 'port = { policy = "random" }\n'
    [(line, location, _message)] = _issues(text)
    assert (line, location) == (_line_of(text, "port ="), "services[0].port")


def test_http_readiness_parses_with_default_status() -> None:
    text = MINIMAL + SERVICE + 'port = { policy = "dynamic" }\nready = { kind = "http", path = "/api/health" }\n'
    manifest = parse_manifest_text(text).manifest
    assert manifest is not None
    ready = manifest.services[0].effective_ready
    assert isinstance(ready, HttpReady) and ready.expect_status == 200


def test_log_readiness_requires_a_valid_regex() -> None:
    text = MINIMAL + '\n[[services]]\nid = "w"\nlabel = "W"\nargv = ["w"]\nready = { kind = "log", pattern = "(" }\n'
    [(line, location, _message)] = _issues(text)
    assert (line, location) == (_line_of(text, "ready ="), "services[0].ready.pattern")


def test_open_and_port_readiness_need_a_port() -> None:
    text = MINIMAL + '\n[[services]]\nid = "w"\nlabel = "W"\nargv = ["w"]\nready = { kind = "port" }\nopen = "/"\n'
    locations = [location for _line, location, _message in _issues(text)]
    assert locations == ["services[0].ready", "services[0].open"]


def test_dependency_references_and_cycles_are_checked() -> None:
    services = (
        '\n[[services]]\nid = "a"\nlabel = "A"\nargv = ["a"]\ndepends_on = ["b"]\n'
        '\n[[services]]\nid = "b"\nlabel = "B"\nargv = ["b"]\ndepends_on = ["a", "ghost"]\n'
    )
    issues = _issues(MINIMAL + services)
    expected = (_line_of(MINIMAL + services, "ghost"), "services[1].depends_on[1]")
    assert expected in [(line, location) for line, location, _ in issues]
    assert any("cycle" in message for _line, _location, message in issues)


def test_duplicate_ids_point_at_the_second_entry() -> None:
    checks = '\n[[checks]]\nid = "t"\nlabel = "T"\nargv = ["x"]\n\n[[checks]]\nid = "t"\nlabel = "T2"\nargv = ["y"]\n'
    [(line, location, message)] = _issues(MINIMAL + checks)
    assert (line, location) == (_line_of(MINIMAL + checks, 'id = "t"', occurrence=2), "checks[1].id")
    assert "duplicate id 't'" in message


def test_v1_source_rules() -> None:
    no_milestones = MINIMAL.split("[[v1.milestones]]")[0]
    assert [loc for _l, loc, _m in _issues(no_milestones)] == ["v1.source"]
    github = no_milestones.replace('source = "milestones"', 'source = "github-milestone"\ngithub_milestone = "V1"')
    manifest = parse_manifest_text(github).manifest
    assert manifest is not None and manifest.v1.github_milestone == "V1"
    both = MINIMAL.replace('source = "milestones"', 'source = "github-milestone"\ngithub_milestone = "V1"')
    assert [loc for _l, loc, _m in _issues(both)] == ["v1.milestones"]


def test_metric_sources_are_discriminated_and_cross_checked() -> None:
    metric = '\n[[metrics]]\nid = "m"\nlabel = "M"\nsource = { kind = "http", service = "ghost", path = "/m" }\n'
    [(line, location, message)] = _issues(MINIMAL + metric)
    assert (line, location) == (_line_of(MINIMAL + metric, "source = {"), "metrics[0].source.service")
    assert "unknown service 'ghost'" in message


def test_metric_interval_has_a_floor() -> None:
    metric = '\n[[metrics]]\nid = "m"\nlabel = "M"\ninterval_s = 5\nsource = { kind = "file", path = "m.json" }\n'
    [(_line, location, _message)] = _issues(MINIMAL + metric)
    assert location == "metrics[0].interval_s"


def test_action_params_must_be_an_object_json_schema() -> None:
    action = '\n[[actions]]\nid = "a"\nlabel = "A"\nargv = ["a"]\n\n[actions.params]\ntype = "array"\n'
    [(line, location, message)] = _issues(MINIMAL + action)
    assert (line, location) == (_line_of(MINIMAL + action, "[actions.params]"), "actions[0].params")
    assert 'type = "object"' in message


def test_action_params_accept_nested_toml_tables() -> None:
    action = (
        '\n[[actions]]\nid = "a"\nlabel = "A"\nargv = ["a"]\nrequires_service = "app"\n'
        '\n[actions.params]\ntype = "object"\nproperties.full = { type = "boolean", default = false }\n'
    )
    text = MINIMAL + SERVICE + 'port = { policy = "dynamic" }\n' + action
    manifest = parse_manifest_text(text).manifest
    assert manifest is not None
    assert manifest.actions[0].params == {
        "type": "object",
        "properties": {"full": {"type": "boolean", "default": False}},
    }


def test_launch_primary_service_must_exist() -> None:
    text = MINIMAL + '\n[launch]\nprimary_service = "app"\n'
    [(line, location, _message)] = _issues(text)
    assert (line, location) == (_line_of(text, "primary_service"), "launch.primary_service")


def test_toml_syntax_errors_carry_their_line() -> None:
    text = MINIMAL + "\nbroken = = 1\n"
    [(line, location, message)] = _issues(text)
    assert (line, location) == (_line_of(text, "broken"), "")
    assert "at line" not in message


def test_missing_file_is_an_issue_not_an_exception(tmp_path: Path) -> None:
    result = load_manifest(tmp_path)
    assert not result.valid
    assert result.issues[0].message == "file not found"


def test_rendered_issue_uses_path_line_location() -> None:
    result = parse_manifest_text(MINIMAL.replace("schema = 1", "schema = 2"), "repo/forge.project.toml")
    assert result.issues[0].render(result.path).startswith("repo/forge.project.toml:1: schema: ")


@pytest.mark.parametrize(
    ("payload", "value"),
    [('{"value": 12}', 12), ('{"value": true}', True), ('{"value": "ok", "status": "warn"}', "ok")],
)
def test_metric_reading_accepts_numbers_strings_and_booleans(payload: str, value: object) -> None:
    reading = MetricReading.model_validate_json(payload)
    assert reading.value == value
    assert type(reading.value) is type(value)
