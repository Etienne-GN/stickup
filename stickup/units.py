"""systemd user units: one template service, linked to each stick's device unit."""

import os
import subprocess
import sys
from pathlib import Path

TEMPLATE = "stickup-backup@.service"


def unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"


def launcher() -> str:
    return str(Path(sys.argv[0]).resolve())


def service_name(uuid: str) -> str:
    return f"stickup-backup@{uuid}.service"


def device_unit(uuid: str) -> str:
    result = subprocess.run(
        ["systemd-escape", "--path", "--suffix=device", f"/dev/disk/by-uuid/{uuid}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def template_text() -> str:
    return f"""[Unit]
Description=stickup backup to USB stick %i

[Service]
Type=oneshot
ExecStart={launcher()} backup %i
Nice=19
IOSchedulingClass=idle
"""


def _reload() -> None:
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)


def install_template() -> None:
    directory = unit_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / TEMPLATE
    text = template_text()
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
        _reload()


def wants_link(uuid: str) -> Path:
    return unit_dir() / f"{device_unit(uuid)}.wants" / service_name(uuid)


def enable(uuid: str) -> None:
    """Start a backup whenever this stick's filesystem appears."""
    install_template()
    link = wants_link(uuid)
    if link.is_symlink():
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(unit_dir() / TEMPLATE)
    _reload()


def disable(uuid: str) -> None:
    link = wants_link(uuid)
    if not link.is_symlink():
        return
    link.unlink()
    if not any(link.parent.iterdir()):
        link.parent.rmdir()
    _reload()


def is_enabled(uuid: str) -> bool:
    return wants_link(uuid).is_symlink()


def is_running(uuid: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", service_name(uuid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() in ("activating", "active")


def start(uuid: str) -> None:
    install_template()
    subprocess.run(["systemctl", "--user", "start", "--no-block", service_name(uuid)], check=True)


def stop(uuid: str) -> None:
    subprocess.run(["systemctl", "--user", "stop", service_name(uuid)], check=True)
