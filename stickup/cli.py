"""Command line entry point; the window and the systemd units both go through it."""

import argparse
import getpass
import json
import logging
import sys

from . import __version__, backup, config, devices, units


def stick_summary(stick: config.Stick) -> dict:
    filesystem = devices.find(stick.uuid)
    return {
        "uuid": stick.uuid,
        "name": stick.name,
        "plugged": filesystem is not None,
        "auto": units.is_enabled(stick.uuid),
        "running": units.is_running(stick.uuid),
        "progress": config.read_progress(stick.uuid),
        "last_run": config.read_last_run(stick.uuid),
    }


def command_devices(arguments, configuration) -> int:
    for filesystem in devices.usb_filesystems():
        enrolled = "enrolled" if filesystem.uuid in configuration.sticks else "new"
        print(
            f"{filesystem.uuid}\t{filesystem.label or '-'}\t{filesystem.fstype}\t"
            f"{filesystem.size}\t{filesystem.drive}\t{enrolled}"
        )
    return 0


def command_status(arguments, configuration) -> int:
    summaries = [stick_summary(stick) for stick in configuration.sticks.values()]
    if arguments.json:
        print(json.dumps(summaries, indent=2))
        return 0
    if not summaries:
        print("no stick enrolled")
    for summary in summaries:
        last = summary["last_run"]
        state = "plugged in" if summary["plugged"] else "not plugged in"
        print(f"{summary['name']} ({summary['uuid']}): {state}")
        if summary["running"]:
            progress = summary["progress"]
            print(f"  running: {progress.get('phase', 'starting')} {progress.get('percent', 0)}%")
        if last:
            print(f"  last backup: {last.get('finished', last.get('started'))} {last['status']}")
        else:
            print("  last backup: never")
    return 0


def command_add(arguments, configuration) -> int:
    filesystem = devices.find(arguments.uuid)
    if filesystem is None:
        print("that stick is not plugged in", file=sys.stderr)
        return 1
    stick = configuration.sticks.get(arguments.uuid) or config.Stick(
        uuid=arguments.uuid, name=arguments.name or filesystem.label or filesystem.drive or "USB stick"
    )
    passphrase = None
    if not devices.has_passphrase(stick.uuid):
        if sys.stdin.isatty():
            passphrase = getpass.getpass("Backup passphrase: ")
            if passphrase != getpass.getpass("Repeat it: "):
                print("the two entries differ", file=sys.stderr)
                return 1
        else:
            passphrase = sys.stdin.readline().rstrip("\n")
    existing = backup.enroll(stick, passphrase)
    configuration.sticks[stick.uuid] = stick
    configuration.save()
    if stick.auto:
        units.enable(stick.uuid)
    print(f"enrolled {stick.name}" + (", continuing the backup already on it" if existing else ""))
    return 0


def command_remove(arguments, configuration) -> int:
    configuration.get(arguments.uuid)
    units.disable(arguments.uuid)
    del configuration.sticks[arguments.uuid]
    configuration.save()
    print("removed from stickup; the data on the stick was not touched")
    return 0


def command_backup(arguments, configuration) -> int:
    result = backup.run_backup(configuration.get(arguments.uuid), notifications=not arguments.quiet)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in ("ok", "incomplete", "skipped") else 1


def command_snapshots(arguments, configuration) -> int:
    for snapshot in backup.list_snapshots(configuration.get(arguments.uuid)):
        print(f"{snapshot['short_id']}\t{snapshot['time'][:19]}\t{' '.join(snapshot['paths'])}")
    return 0


def command_restore(arguments, configuration) -> int:
    backup.restore(configuration.get(arguments.uuid), arguments.path, arguments.target, arguments.snapshot)
    print(f"restored under {arguments.target}{arguments.path}")
    return 0


def command_check_passphrase(arguments, configuration) -> int:
    typed = getpass.getpass("Backup passphrase: ")
    if backup.passphrase_opens(configuration.get(arguments.uuid), typed):
        print("passphrase OK")
        return 0
    print("passphrase REJECTED", file=sys.stderr)
    return 1


def command_gui(arguments, configuration) -> int:
    from . import gui

    return gui.main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stickup", description="Back up to USB sticks when they are plugged in.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("gui", help="open the window (default)").set_defaults(run=command_gui)
    commands.add_parser("devices", help="list plugged-in USB filesystems").set_defaults(run=command_devices)

    status = commands.add_parser("status", help="show enrolled sticks and their last backup")
    status.add_argument("--json", action="store_true")
    status.set_defaults(run=command_status)

    add = commands.add_parser("add", help="enroll a plugged-in stick")
    add.add_argument("uuid")
    add.add_argument("--name")
    add.set_defaults(run=command_add)

    remove = commands.add_parser("remove", help="forget a stick, leaving its data alone")
    remove.add_argument("uuid")
    remove.set_defaults(run=command_remove)

    run = commands.add_parser("backup", help="back up to a stick now")
    run.add_argument("uuid")
    run.add_argument("--quiet", action="store_true", help="no desktop notifications")
    run.set_defaults(run=command_backup)

    snapshots = commands.add_parser("snapshots", help="list the snapshots on a stick")
    snapshots.add_argument("uuid")
    snapshots.set_defaults(run=command_snapshots)

    restore = commands.add_parser("restore", help="restore a path into a target directory")
    restore.add_argument("uuid")
    restore.add_argument("path")
    restore.add_argument("target")
    restore.add_argument("--snapshot", default="latest")
    restore.set_defaults(run=command_restore)

    check = commands.add_parser("check-passphrase", help="check that you remember the passphrase")
    check.add_argument("uuid")
    check.set_defaults(run=command_check_passphrase)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    arguments = build_parser().parse_args(argv)
    if arguments.command is None:
        arguments.run = command_gui
    configuration = config.Config()
    try:
        return arguments.run(arguments, configuration)
    except (KeyError, RuntimeError, ValueError) as error:
        print(str(error).strip("'\""), file=sys.stderr)
        return 1
