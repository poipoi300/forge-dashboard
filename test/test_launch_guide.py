"""Launch instructions: manifest text or file first, then the README's setup or run section."""

from pathlib import Path

from conftest import MANIFEST, write_files

from forge_dashboard.domain.launch import launch_guide, readme_run_section
from forge_dashboard.manifest import ProjectManifest, parse_manifest_text

SERVICE = """
[[services]]
id = "web"
label = "Web"
argv = ["bun", "run", "dev"]
"""


def manifest(extra: str = "") -> ProjectManifest:
    loaded = parse_manifest_text(MANIFEST.replace("{name}", "Demo").replace("{project_id}", "demo") + extra)
    assert loaded.manifest is not None, loaded.issues
    return loaded.manifest


def test_readme_section_stops_at_the_next_heading_of_the_same_level() -> None:
    readme = "# Demo\n\nIntro.\n\n## Setup\n\nRun it.\n\n### Detail\n\nMore.\n\n## License\n\nMIT\n"

    assert readme_run_section(readme) == "## Setup\n\nRun it.\n\n### Detail\n\nMore.\n"


def test_readme_headings_inside_code_fences_are_not_sections() -> None:
    readme = "# Demo\n\n```md\n## Usage\n```\n\n## Running\n\nuv run demo\n"

    assert readme_run_section(readme) == "## Running\n\nuv run demo\n"


def test_readme_without_a_run_section_has_none() -> None:
    assert readme_run_section("# Demo\n\n## License\n\nMIT\n") is None


def test_inline_manifest_instructions_win_over_the_readme(tmp_path: Path) -> None:
    write_files(tmp_path, {"README.md": "# Demo\n\n## Setup\n\nfrom readme\n"})

    guide = launch_guide(tmp_path, manifest(SERVICE + '\n[launch]\ninstructions = "Open the **web** service."\n'))

    assert (guide.source, guide.markdown, guide.primary_service) == ("manifest", "Open the **web** service.", "web")


def test_manifest_instructions_can_name_a_markdown_file(tmp_path: Path) -> None:
    write_files(tmp_path, {"docs/launch.md": "# Launch\n\nStep one.\n"})

    guide = launch_guide(tmp_path, manifest('\n[launch]\ninstructions = "docs/launch.md"\n'))

    assert guide.source == "manifest-file"
    assert guide.markdown == "# Launch\n\nStep one.\n"
    assert guide.problem is None


def test_a_missing_instructions_file_is_reported(tmp_path: Path) -> None:
    guide = launch_guide(tmp_path, manifest('\n[launch]\ninstructions = "docs/missing.md"\n'))

    assert guide.source == "manifest-file"
    assert guide.problem is not None and "docs/missing.md" in guide.problem


def test_an_instructions_file_outside_the_project_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "demo"
    write_files(tmp_path, {"secret.md": "outside\n", "demo/README.md": "# Demo\n"})

    guide = launch_guide(project, manifest('\n[launch]\ninstructions = "../secret.md"\n'))

    assert guide.markdown == ""
    assert guide.problem is not None and "outside the project" in guide.problem


def test_without_manifest_instructions_the_readme_section_is_used(tmp_path: Path) -> None:
    write_files(tmp_path, {"README.md": "# Demo\n\n## Quick start\n\nuv run demo\n"})

    guide = launch_guide(tmp_path, manifest(SERVICE))

    assert (guide.source, guide.markdown, guide.primary_service) == ("readme", "## Quick start\n\nuv run demo\n", "web")


def test_no_readme_and_no_instructions_explains_why(tmp_path: Path) -> None:
    guide = launch_guide(tmp_path, None)

    assert (guide.source, guide.problem, guide.primary_service) == ("none", "no README.md", None)
