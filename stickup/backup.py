"""One backup run, and the other operations that need the stick mounted."""

import fcntl
import logging
import os
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config, devices
from .notify import Notifier, ProgressSteps
from .restic import Repository, ResticError

log = logging.getLogger(__name__)


class Busy(RuntimeError):
    pass


@contextmanager
def stick_lock(uuid: str):
    directory = config.runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f"{uuid}.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("another operation is using this stick") from None
        yield


def verify_repository_tree(directory: Path) -> None:
    """Refuse a backup folder that someone else could have tampered with.

    restic never creates symlinks in a repository, so any found there was planted;
    and a folder another user can write to could gain one between this check and
    restic's own access.
    """
    if not directory.is_dir():
        return
    me, my_group = os.getuid(), os.getgid()
    pending = [directory]
    while pending:
        current = pending.pop()
        status = os.lstat(current)
        open_to_group = status.st_mode & 0o020 and status.st_gid != my_group
        if status.st_uid != me or status.st_mode & 0o002 or open_to_group:
            raise RuntimeError(f"{current} is not yours alone to write, refusing to use it")
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise RuntimeError(f"{entry.path} is a symlink inside the backup folder, refusing to use it")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))


@contextmanager
def mounted_repository(stick: config.Stick, keep_mounted: bool = False):
    mountpoint, mounted_here = devices.mount(stick.uuid)
    try:
        root = Path(mountpoint).resolve()
        path = root / stick.repository
        # the stick's contents are not trusted: a planted symlink must not redirect the repository
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise RuntimeError(f"{path} leads outside the stick, refusing to use it")
        verify_repository_tree(path)
        yield Repository(path, stick.uuid), root
    finally:
        if mounted_here and not keep_mounted:
            devices.unmount(stick.uuid)


def human_duration(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {seconds} s"
    return f"{seconds} s"


def write_package_lists() -> None:
    directory = config.state_dir() / "packages"
    directory.mkdir(parents=True, exist_ok=True)
    listings = {
        "apt-manual.txt": ["apt-mark", "showmanual"],
        "dpkg-versions.txt": ["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"],
        "pacman-explicit.txt": ["pacman", "-Qe"],
    }
    for filename, command in listings.items():
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            continue
        if result.returncode == 0:
            (directory / filename).write_text(result.stdout)


def run_backup(stick: config.Stick, notifications: bool = True, quiet_seconds: int | None = None) -> dict:
    """Back up to a plugged-in stick. Returns the recorded result."""
    notifier = Notifier(stick.name, enabled=notifications)
    started = time.monotonic()
    result = {"started": datetime.now().isoformat(timespec="seconds"), "status": "failed"}

    if stick.skip_when_locked and devices.session_locked():
        log.info("session is locked or its lock state is unknown, not mounting the stick")
        return {"status": "skipped", "reason": "session locked or lock state unknown"}

    try:
        with stick_lock(stick.uuid):
            try:
                result.update(_backup_locked(stick, notifier, quiet_seconds))
            except (ResticError, RuntimeError, subprocess.CalledProcessError) as error:
                message = _describe(error)
                log.error("backup failed: %s", message)
                result["error"] = message
                notifier.send("backup FAILED", message, critical=True)
            finally:
                config.clear_progress(stick.uuid)
    except Busy as error:
        log.info("%s, skipping this run", error)
        return {"status": "skipped", "reason": str(error)}

    result["duration_seconds"] = round(time.monotonic() - started)
    config.write_last_run(stick.uuid, result)
    if result["status"] == "ok":
        notifier.send("backup done", f"Took {human_duration(result['duration_seconds'])}. Safe to unplug.")
    elif result["status"] == "incomplete":
        count = len(result["unreadable"])
        notifier.send(
            "backup done, some files unreadable",
            f"{count} could not be read. Took {human_duration(result['duration_seconds'])}. Safe to unplug.",
            critical=True,
        )
    return result


def _describe(error: Exception) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        return (error.stderr or str(error)).strip()
    return str(error)


def _backup_locked(stick: config.Stick, notifier: Notifier, quiet_seconds: int | None) -> dict:
    with mounted_repository(stick) as (repository, mountpoint):
        if not repository.opens():
            raise ResticError("cannot open the repository: missing, or passphrase not in the keyring")
        repository.unlock()
        if stick.package_lists:
            write_package_lists()

        notifier.send("backup started", "Do not unplug the stick.")
        config.write_progress(stick.uuid, {"phase": "starting", "percent": 0})
        steps = ProgressSteps() if quiet_seconds is None else ProgressSteps(quiet_seconds)

        def on_event(event: dict) -> None:
            if event.get("message_type") != "status":
                return
            percent = int(event.get("percent_done", 0) * 100)
            # restic's seconds_remaining is deliberately not shown: it was off by a factor of two in practice
            config.write_progress(
                stick.uuid,
                {
                    "phase": "copying",
                    "percent": percent,
                    "bytes_done": event.get("bytes_done"),
                    "total_bytes": event.get("total_bytes"),
                },
            )
            shown = steps.due(percent)
            if shown is not None:
                notifier.send(f"backup {shown}% done", "Do not unplug the stick.")

        summary, unreadable = repository.backup(
            stick.expanded_sources(), stick.expanded_excludes(), stick.exclude_caches, on_event
        )

        config.write_progress(stick.uuid, {"phase": "tidying up", "percent": 100})
        repository.forget(stick.keep_last)
        repository.check()
        verify_repository_tree(repository.path)
        snapshots = repository.snapshots()
        usage = _disk_usage(mountpoint)

    return {
        "status": "incomplete" if unreadable else "ok",
        "finished": datetime.now().isoformat(timespec="seconds"),
        "snapshot": summary.get("snapshot_id", "")[:8],
        "files": summary.get("total_files_processed"),
        "bytes": summary.get("total_bytes_processed"),
        "bytes_added": summary.get("data_added_packed", summary.get("data_added")),
        "unreadable": unreadable,
        "snapshot_count": len(snapshots),
        **usage,
    }


def _disk_usage(mountpoint: Path) -> dict:
    usage = shutil.disk_usage(mountpoint)
    return {"stick_total": usage.total, "stick_used": usage.used, "stick_free": usage.free}


def enroll(stick: config.Stick, passphrase: str | None) -> None:
    """Create the repository if the stick has none, and remember the passphrase."""
    if passphrase:
        devices.store_passphrase(stick.uuid, stick.name, passphrase)
    if not devices.has_passphrase(stick.uuid):
        raise RuntimeError("no passphrase stored for this stick")
    with stick_lock(stick.uuid), mounted_repository(stick) as (repository, mountpoint):
        probe = mountpoint / f".stickup-write-test-{secrets.token_hex(8)}"
        try:
            os.close(os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
            probe.unlink()
        except OSError:
            raise RuntimeError(
                f"{mountpoint} is not writable by you; fix its ownership or reformat the stick"
            ) from None
        if repository.exists():
            if not repository.opens():
                raise ResticError("this stick already holds a backup and the passphrase does not open it")
        elif repository.path.exists() and any(repository.path.iterdir()):
            raise RuntimeError(
                f"{repository.path} already holds files that are not a backup; empty or remove that folder first"
            )
        else:
            repository.init()


def list_snapshots(stick: config.Stick) -> list[dict]:
    with stick_lock(stick.uuid), mounted_repository(stick) as (repository, _):
        return repository.snapshots()


def restore(stick: config.Stick, include: str, target: str, snapshot: str = "latest") -> None:
    with stick_lock(stick.uuid), mounted_repository(stick) as (repository, _):
        repository.restore(snapshot, include, target)


def passphrase_opens(stick: config.Stick, passphrase: str) -> bool:
    """Check a typed passphrase against the stick, ignoring the keyring."""
    with stick_lock(stick.uuid), mounted_repository(stick) as (repository, _):
        with tempfile.NamedTemporaryFile("w", dir=config.runtime_dir(), suffix=".pass") as handle:
            handle.write(passphrase)
            handle.flush()
            typed = Repository(repository.path, stick.uuid, f"cat {shlex.quote(handle.name)}")
            return typed.opens()
