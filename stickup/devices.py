"""USB filesystems: discovery, mounting through udisks, keyring and session state."""

import json
import os
import shlex
import subprocess
from dataclasses import dataclass

from .config import UUID_PATTERN

# NAME must stay in the list: without it lsblk prints a flat list instead of a tree
LSBLK_COLUMNS = "NAME,PATH,TRAN,VENDOR,MODEL,SERIAL,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT"


@dataclass
class Filesystem:
    uuid: str
    device: str
    label: str
    fstype: str
    size: str
    drive: str
    serial: str
    mountpoint: str | None


def valid_uuid(uuid: str) -> bool:
    """Filesystem UUIDs come from the stick itself, so they are checked before use."""
    return isinstance(uuid, str) and UUID_PATTERN.fullmatch(uuid) is not None


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)


def usb_filesystems() -> list[Filesystem]:
    output = run(["lsblk", "--json", "--tree", "--output", LSBLK_COLUMNS]).stdout
    return parse_lsblk(json.loads(output))


def parse_lsblk(listing: dict) -> list[Filesystem]:
    found = []
    for disk in listing["blockdevices"]:
        if disk.get("tran") != "usb":
            continue
        drive = " ".join(part.strip() for part in (disk.get("vendor"), disk.get("model")) if part).strip()
        for node in [disk, *disk.get("children", [])]:
            if not node.get("fstype") or not valid_uuid(node.get("uuid")):
                continue
            found.append(
                Filesystem(
                    uuid=node["uuid"],
                    device=node["path"],
                    label=node.get("label") or "",
                    fstype=node["fstype"],
                    size=node["size"],
                    drive=drive,
                    serial=disk.get("serial") or "",
                    mountpoint=node.get("mountpoint"),
                )
            )
    return found


def find(uuid: str) -> Filesystem | None:
    for filesystem in usb_filesystems():
        if filesystem.uuid == uuid:
            return filesystem
    return None


def mount(uuid: str) -> tuple[str, bool]:
    """Return the mount point and whether this call mounted it."""
    filesystem = find(uuid)
    if filesystem is None:
        raise RuntimeError("the stick is not plugged in")
    if filesystem.mountpoint:
        return filesystem.mountpoint, False
    run(["udisksctl", "mount", "--block-device", filesystem.device, "--no-user-interaction"])
    mounted = find(uuid)
    if mounted is None or not mounted.mountpoint:
        raise RuntimeError("the stick did not mount")
    return mounted.mountpoint, True


def unmount(uuid: str) -> None:
    filesystem = find(uuid)
    if filesystem is None or not filesystem.mountpoint:
        return
    run(["sync"])
    run(["udisksctl", "unmount", "--block-device", filesystem.device, "--no-user-interaction"])


def session_locked() -> bool:
    """True when the graphical session is locked, or when that cannot be established."""
    try:
        user = os.environ.get("USER") or run(["id", "-un"]).stdout.strip()
        session = run(["loginctl", "show-user", user, "--value", "--property", "Display"]).stdout.strip()
        if not session:
            return True
        hint = run(["loginctl", "show-session", session, "--value", "--property", "LockedHint"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return True
    return hint.stdout.strip() != "no"


def keyring_attributes(uuid: str) -> list[str]:
    if not valid_uuid(uuid):
        raise ValueError(f"not a filesystem UUID: {uuid!r}")
    return ["service", "stickup", "stick", uuid]


def password_command(uuid: str) -> str:
    return shlex.join(["secret-tool", "lookup", *keyring_attributes(uuid)])


def has_passphrase(uuid: str) -> bool:
    result = subprocess.run(
        ["secret-tool", "lookup", *keyring_attributes(uuid)], capture_output=True, check=False
    )
    return result.returncode == 0 and bool(result.stdout)


def store_passphrase(uuid: str, name: str, passphrase: str) -> None:
    run(
        ["secret-tool", "store", f"--label=stickup backup: {name}", *keyring_attributes(uuid)],
        input=passphrase,
    )
