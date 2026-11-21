"""Exercise the launcher with an isolated PATH and stub tool installers."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

RUN_SH = Path(__file__).resolve().parents[1] / "run.sh"
pytestmark = pytest.mark.skipif(os.name == "nt", reason="run.sh is the macOS/Linux launcher")


@pytest.fixture
def launcher_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("bash", "sh", "dirname", "mkdir", "ln"):
        path = shutil.which(name)
        assert path is not None
        (bin_dir / name).symlink_to(path)

    curl = bin_dir / "curl"
    curl.write_text(
        r"""#!/usr/bin/env bash
url="${!#}"
printf '%s\n' "$url" >> "$INSTALL_LOG"
case "$url" in
  https://astral.sh/uv/install.sh)
    printf '%s\n' 'mkdir -p "$HOME/.local/bin"' 'ln -s "$STUB_UV" "$HOME/.local/bin/uv"' ;;
  https://bun.sh/install)
    printf '%s\n' 'mkdir -p "$HOME/.bun/bin"' 'ln -s "$STUB_BUN" "$HOME/.bun/bin/bun"' ;;
  *) exit 99 ;;
esac
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)

    uv = tmp_path / "uv"
    uv.write_text(
        '#!/usr/bin/env bash\ncommand -v bun >/dev/null || exit 98\nprintf "%s\\n" "$@"\nexit 42\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    bun = tmp_path / "bun"
    bun.symlink_to(bin_dir / "sh")

    return {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "INSTALL_LOG": str(tmp_path / "install.log"),
        "STUB_UV": str(uv),
        "STUB_BUN": str(bun),
    }


@pytest.mark.parametrize(("missing", "present"), [("uv", "bun"), ("bun", "uv")])
def test_missing_tool_fails_before_installing_or_running(
    launcher_env: dict[str, str], missing: str, present: str
) -> None:
    bin_dir = Path(launcher_env["PATH"])
    (bin_dir / present).symlink_to(launcher_env[f"STUB_{present.upper()}"])

    result = subprocess.run(
        [str(RUN_SH), "--check"],
        env=launcher_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert not Path(launcher_env["INSTALL_LOG"]).exists(), result.stdout
    assert result.returncode == 1
    assert f"{missing} is required" in result.stderr
    assert f"add the {missing} package to your arcus module" in result.stderr
    assert "--install-tools" in result.stderr
    assert not result.stdout


def test_install_tools_bootstraps_and_forwards_runner_arguments(launcher_env: dict[str, str]) -> None:
    result = subprocess.run(
        [str(RUN_SH), "--install-tools", "--only", "service name"],
        env=launcher_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert Path(launcher_env["INSTALL_LOG"]).read_text(encoding="utf-8").splitlines() == [
        "https://astral.sh/uv/install.sh",
        "https://bun.sh/install",
    ]
    assert result.returncode == 42, result.stderr
    assert result.stdout.splitlines()[-5:] == ["run", "-m", "forge_dashboard.runner", "--only", "service name"]
