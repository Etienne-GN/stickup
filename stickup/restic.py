"""Thin wrapper around the restic command line."""

import json
import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

from . import devices

UNREADABLE_FILES_EXIT = 3
PATTERN_CHARACTERS = "\\*?["


class ResticError(RuntimeError):
    pass


class Repository:
    def __init__(self, path: Path, uuid: str, password_command: str | None = None):
        self.path = path
        self.uuid = uuid
        self.password_command = password_command or devices.password_command(uuid)

    def environment(self, **extra: str) -> dict[str, str]:
        environment = dict(os.environ)
        for name in ("RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE"):
            environment.pop(name, None)
        environment["RESTIC_REPOSITORY"] = str(self.path)
        environment["RESTIC_PASSWORD_COMMAND"] = self.password_command
        environment.update(extra)
        return environment

    def run(self, *arguments: str, accept: tuple[int, ...] = (0,)) -> str:
        result = subprocess.run(
            ["restic", *arguments],
            env=self.environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode not in accept:
            raise ResticError(result.stderr.strip() or f"restic exited {result.returncode}")
        return result.stdout

    def exists(self) -> bool:
        return (self.path / "config").exists()

    def init(self) -> None:
        self.run("init")

    def opens(self) -> bool:
        try:
            self.run("cat", "config")
        except ResticError:
            return False
        return True

    def unlock(self) -> None:
        self.run("unlock")

    def snapshots(self) -> list[dict]:
        return json.loads(self.run("snapshots", "--json"))

    def forget(self, keep_last: int) -> None:
        self.run("forget", "--keep-last", str(keep_last), "--prune")

    def check(self) -> None:
        self.run("check", "--with-cache")

    def ls(self, snapshot: str, directory: str) -> list[dict]:
        """List what a folder of a snapshot holds, folders first."""
        entries = []
        for line in self.run("ls", "--json", "--", snapshot, directory).splitlines():
            entry = json.loads(line)
            if entry.get("struct_type") == "node" and entry.get("path") != directory:
                entries.append(entry)
        entries.sort(key=lambda entry: (entry.get("type") != "dir", str(entry.get("name", "")).casefold()))
        return entries

    def restore(self, snapshot: str, include: str, target: str) -> None:
        self.run("restore", "--target", target, "--include", literal_pattern(include), "--", snapshot)

    def backup(
        self,
        sources: list[str],
        excludes: list[str],
        exclude_caches: bool,
        on_event: Callable[[dict], None],
    ) -> tuple[dict, list[str]]:
        """Run a backup; return restic's summary and the list of unreadable paths."""
        command = ["restic", "backup", "--json", "--one-file-system"]
        if exclude_caches:
            command.append("--exclude-caches")
        for pattern in excludes:
            command += ["--exclude", pattern]
        command += ["--", *sources]

        summary: dict = {}
        unreadable: list[str] = []
        plain: list[str] = []
        with subprocess.Popen(
            command,
            env=self.environment(RESTIC_PROGRESS_FPS="0.2"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as process:
            for event in _events(process.stdout, plain):
                kind = event.get("message_type")
                if kind == "summary":
                    summary = event
                elif kind == "error":
                    unreadable.append(event.get("item") or str(event.get("error")))
                on_event(event)
        code = process.returncode
        if code not in (0, UNREADABLE_FILES_EXIT):
            raise ResticError("\n".join(plain[-5:]) or f"restic backup exited {code}")
        return summary, unreadable


def literal_pattern(path: str) -> str:
    """Escape a path so restic matches it as written rather than as a pattern."""
    return "".join("\\" + character if character in PATTERN_CHARACTERS else character for character in path)


def _events(stream, plain: list[str]) -> Iterator[dict]:
    """Yield restic's JSON messages; anything else is kept as plain error text."""
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            plain.append(line)
            continue
        if isinstance(event, dict):
            yield event
