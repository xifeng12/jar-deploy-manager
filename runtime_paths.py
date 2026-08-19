import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    home: Path

    @property
    def database(self) -> Path:
        return self.home / "data" / "deploy.db"

    @property
    def jars(self) -> Path:
        return self.home / "jars"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def scheduled_tasks(self) -> Path:
        return self.home / "scheduled_tasks.json"

    @property
    def known_hosts(self) -> Path:
        return self.home / "known_hosts"


def resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


def get_runtime_paths() -> RuntimePaths:
    configured_home = os.environ.get("DEPLOY_HOME", "").strip()
    if configured_home:
        home = Path(configured_home).expanduser()
    elif getattr(sys, "frozen", False) and os.name == "nt":
        home = Path(sys.executable).resolve().parent
    else:
        home = resource_dir()
    return RuntimePaths(home=home)


def ensure_runtime_directories() -> RuntimePaths:
    paths = get_runtime_paths()
    for path in (paths.database.parent, paths.jars, paths.logs):
        path.mkdir(parents=True, exist_ok=True)
    return paths
