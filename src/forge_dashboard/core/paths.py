"""Where the dashboard keeps its state on disk."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

DATA_DIR_ENV = "FORGE_DASHBOARD_DATA_DIR"
APP_NAME = "forge-dashboard"


@dataclass(frozen=True)
class DataPaths:
    """Every location under the data dir, derived from one root.

    The default root is the platform's per-user data directory, which
    discovery prunes (AppData, Library), so dashboard-owned worktrees are never
    mistaken for projects. `FORGE_DASHBOARD_DATA_DIR` overrides it.
    """

    root: Path

    @classmethod
    def default(cls) -> DataPaths:
        override = os.environ.get(DATA_DIR_ENV)
        root = Path(override) if override else platformdirs.user_data_path(APP_NAME, appauthor=False)
        return cls(root=root)

    @property
    def database(self) -> Path:
        return self.root / "dashboard.sqlite3"

    @property
    def jobs(self) -> Path:
        return self.root / "jobs"

    @property
    def worktrees(self) -> Path:
        return self.root / "worktrees"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def transcripts(self) -> Path:
        return self.root / "transcripts"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def server_file(self) -> Path:
        """Written by a running API server so the CLI can find it."""
        return self.root / "server.json"

    def ensure(self) -> None:
        for directory in (self.root, self.jobs, self.worktrees, self.reports, self.transcripts, self.backups):
            directory.mkdir(parents=True, exist_ok=True)
