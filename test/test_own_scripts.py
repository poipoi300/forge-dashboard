"""The dashboard's own manifest metrics and custom action (scripts/)."""

import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType

import pytest
from conftest import write_files

from forge_dashboard.manifest import MetricReading

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reading(capsys: pytest.CaptureFixture[str]) -> MetricReading:
    return MetricReading.model_validate_json(capsys.readouterr().out)


def test_source_lines_counts_non_blank_hand_written_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    metrics = load("metrics")
    write_files(
        tmp_path,
        {
            "src/pkg/a.py": "x = 1\n\ny = 2\n",
            "web/src/app.tsx": "export {};\n",
            "web/src/api/schema.d.ts": "generated\nlines\n",
        },
    )

    assert metrics.main(["source-lines"], root=tmp_path) == 0
    result = reading(capsys)
    assert (result.value, result.detail) == (3, "2 files")


def test_bundle_size_sums_shipped_files_and_warns_when_unbuilt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    metrics = load("metrics")

    assert metrics.main(["bundle-size"], root=tmp_path) == 0
    assert reading(capsys).status == "warn"

    write_files(
        tmp_path, {"web/dist/index.html": "12345", "web/dist/assets/a.js": "123", "web/dist/assets/a.js.map": "x" * 50}
    )
    assert metrics.main(["bundle-size"], root=tmp_path) == 0
    result = reading(capsys)
    assert (result.value, result.status) == (8, "ok")


def test_unknown_metrics_are_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert load("metrics").main(["nope"]) == 2
    assert "usage" in capsys.readouterr().err


def test_validate_manifest_action_reads_its_path_from_stdin(capsys: pytest.CaptureFixture[str]) -> None:
    action = load("validate_manifest")
    own_manifest = Path(__file__).resolve().parents[1] / "forge.project.toml"

    assert action.main(io.StringIO(json.dumps({"path": str(own_manifest)}))) == 0
    assert action.main(io.StringIO(json.dumps({"path": ""}))) == 2
    assert "non-empty" in capsys.readouterr().err
