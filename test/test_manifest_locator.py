"""The TOML line locator handles every structure a manifest can use."""

import tomllib

from forge_dashboard.manifest.locator import concrete_path, key_lines, line_for

DOCUMENT = '''\
schema = 1  # trailing comment
"quoted key" = 'literal'

[project]
name = """multi
[not.a.header]
fake = "not a key"
"""
tags = [
  "a",  # comment inside an array
  "b",
]
dotted.inner = { x = 1, y = { z = "}" } }

[[checks]]
id = "first"

[[checks]]
id = "second"
argv = ['C:\\path', "{{literal}}"]

[[v1.milestones]]
id = "m1"

[[v1.milestones]]
id = "m2"
when = 1979-05-27 07:32:00Z
'''


def test_document_is_valid_toml() -> None:
    tomllib.loads(DOCUMENT)


def test_keys_headers_and_array_elements_have_lines() -> None:
    lines = key_lines(DOCUMENT)
    assert lines[("schema",)] == 1
    assert lines[("quoted key",)] == 2
    assert lines[("project",)] == 4
    assert lines[("project", "name")] == 5
    assert lines[("project", "tags")] == 9
    assert lines[("project", "tags", 1)] == 11
    assert lines[("project", "dotted", "inner", "y", "z")] == 13
    assert lines[("checks",)] == 15
    assert lines[("checks", 1)] == 18
    assert lines[("checks", 1, "argv", 1)] == 20
    assert lines[("v1", "milestones", 1, "when")] == 27


def test_multiline_string_content_is_not_mistaken_for_structure() -> None:
    lines = key_lines(DOCUMENT)
    assert ("not", "a", "header") not in lines
    assert ("project", "fake") not in lines


def test_line_for_falls_back_to_nearest_ancestor() -> None:
    lines = key_lines(DOCUMENT)
    assert line_for(lines, ("checks", 0, "label")) == 15
    assert line_for(lines, ("nowhere",)) is None


def test_concrete_path_drops_union_tags_and_keeps_missing_keys() -> None:
    data = {"services": [{"port": {"policy": "pinned", "port": 1}}]}
    assert concrete_path(data, ("services", 0, "port", "pinned", "evidence")) == ("services", 0, "port", "evidence")
    assert concrete_path(data, ("services", 3)) == ("services", 3)
