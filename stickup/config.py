"""Per-stick configuration and run state, stored under the user's XDG directories."""

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

UUID_PATTERN = re.compile(r"[A-Za-z0-9-]{1,64}")

DEFAULT_EXCLUDES = [
    "~/.cache",
    "~/.local/share/Trash",
    "~/.local/share/containers",
]


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "stickup"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "stickup"


def runtime_dir() -> Path:
    return Path(os.environ["XDG_RUNTIME_DIR"]) / "stickup"


@dataclass
class Stick:
    uuid: str
    name: str
    sources: list[str] = field(default_factory=lambda: ["~"])
    excludes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    repository: str = "restic"
    keep_last: int = 10
    exclude_caches: bool = True
    package_lists: bool = True
    auto: bool = True
    skip_when_locked: bool = True

    def __post_init__(self):
        if not isinstance(self.uuid, str) or UUID_PATTERN.fullmatch(self.uuid) is None:
            raise ValueError(f"not a filesystem UUID: {self.uuid!r}")
        repository = Path(self.repository)
        if repository.is_absolute() or ".." in repository.parts or not repository.parts:
            raise ValueError(f"repository must be a folder name on the stick: {self.repository!r}")

    def expanded_sources(self) -> list[str]:
        return [os.path.expanduser(path) for path in self.sources]

    def expanded_excludes(self) -> list[str]:
        return [os.path.expanduser(path) for path in self.excludes]


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "config.yaml"
        self.sticks: dict[str, Stick] = {}
        self.load()

    def _stamp(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def changed_on_disk(self) -> bool:
        return self._stamp() != self.loaded_stamp

    def load(self) -> None:
        self.sticks = {}
        self.loaded_stamp = self._stamp()
        if not self.path.exists():
            return
        data = yaml.safe_load(self.path.read_text()) or {}
        for uuid, fields in (data.get("sticks") or {}).items():
            self.sticks[uuid] = Stick(uuid=uuid, **fields)

    def save(self) -> None:
        sticks = {}
        for uuid, stick in self.sticks.items():
            fields = asdict(stick)
            del fields["uuid"]
            sticks[uuid] = fields
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".yaml.tmp")
        temporary.write_text(yaml.safe_dump({"sticks": sticks}, sort_keys=False))
        temporary.replace(self.path)
        self.loaded_stamp = self._stamp()

    def get(self, uuid: str) -> Stick:
        try:
            return self.sticks[uuid]
        except KeyError:
            raise KeyError(f"stick {uuid} is not enrolled") from None


def read_last_run(uuid: str) -> dict:
    path = state_dir() / f"{uuid}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def write_last_run(uuid: str, result: dict) -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"{uuid}.json.tmp"
    temporary.write_text(json.dumps(result, indent=2))
    temporary.replace(directory / f"{uuid}.json")


def progress_path(uuid: str) -> Path:
    return runtime_dir() / f"{uuid}.progress.json"


def read_progress(uuid: str) -> dict:
    path = progress_path(uuid)
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_progress(uuid: str, progress: dict) -> None:
    path = progress_path(uuid)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(progress))
    temporary.replace(path)


def clear_progress(uuid: str) -> None:
    progress_path(uuid).unlink(missing_ok=True)
