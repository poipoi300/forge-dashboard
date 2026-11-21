"""Read the shared report over the Forge doctor's CLI contract."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from forge_dashboard.domain import requirements


def _checkout(tmp_path: Path, script: str, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "forge"
    package = checkout / "src" / "forge"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    (package / "doctor.py").touch()
    (package / "cli.py").write_text(script, encoding="utf-8")
    monkeypatch.setenv(requirements.FORGE_REPO_ENV, str(checkout))


def test_reads_a_report_even_when_forge_exits_one_for_missing_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "work tree" / "forge.project.toml"
    manifest.parent.mkdir()
    manifest.touch()
    report = {
        "ok": False,
        "tools": [
            {
                "name": "bun",
                "status": "missing",
                "required": True,
                "path": None,
                "version": None,
                "package": None,
                "ownership_unknown": False,
            }
        ],
        "harnesses": {"pi": {"cli": "pi", "status": "unavailable", "missing": ["pi"]}},
    }
    argv = ["doctor", "--json", "--manifest", str(manifest)]
    _checkout(
        tmp_path,
        f"import json, sys\nassert sys.argv[1:] == {argv!r}\nprint({json.dumps(report)!r})\nsys.exit(1)\n",
        monkeypatch,
    )
    result = requirements.read_report(manifest)
    assert result.missing_required == ["bun"]
    assert result.harnesses["pi"].status == "unavailable"


def test_installed_forge_gets_version_opt_in_only_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(requirements.FORGE_REPO_ENV, raising=False)
    first = tmp_path / "empty"
    second = tmp_path / "bin"
    executable = str(second / "forge")
    manifest = tmp_path / "forge.project.toml"
    looked_up: list[str] = []

    def which(name: str) -> str | None:
        looked_up.append(name)
        return executable if name == executable else None

    monkeypatch.setenv("PATH", os.pathsep.join(("project-bin", str(first), str(second))))
    monkeypatch.setattr(shutil, "which", which)

    def fake_run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert argv == (executable, "doctor", "--json", "--manifest", str(manifest), "--trust-manifest")
        timeout = kwargs["timeout"]
        assert isinstance(timeout, int) and timeout >= 150
        return subprocess.CompletedProcess(argv, 0, b'{"ok":true,"tools":[],"harnesses":{}}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert requirements.read_report(manifest, versions=True).ok
    assert looked_up == [str(first / "forge"), executable]


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("raise SystemExit(2)", "could not check the manifest"),
        ("print('malformed')", "returned an invalid report"),
        ('print(\'{"ok":false,"tools":[],"harnesses":{}}\')', "returned an inconsistent report"),
        ('print(\'{"ok":false,"tools":[],"harnesses":{}}\'); raise SystemExit(1)', "returned an inconsistent report"),
    ],
)
def test_non_reports_are_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str, message: str) -> None:
    _checkout(tmp_path, script, monkeypatch)
    with pytest.raises(requirements.DoctorError, match=message):
        requirements.read_report(tmp_path / "forge.project.toml")


def test_checkout_must_explicitly_provide_the_doctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(requirements.FORGE_REPO_ENV, str(tmp_path))
    with pytest.raises(requirements.DoctorError, match="must point to a checkout with Forge doctor"):
        requirements.read_report(tmp_path / "forge.project.toml")


def test_missing_or_unstartable_doctor_has_an_actionable_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "forge.project.toml"
    monkeypatch.delenv(requirements.FORGE_REPO_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(requirements.DoctorError, match=requirements.FORGE_REPO_ENV):
        requirements.read_report(manifest)

    _checkout(tmp_path, "", monkeypatch)

    def timed_out(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(requirements.DoctorError, match="could not run"):
        requirements.read_report(manifest)
