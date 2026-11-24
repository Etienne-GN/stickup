import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from stickup import backup, config, devices
from stickup.notify import ProgressSteps

UUID = "0000-TEST"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ProgressStepsTest(unittest.TestCase):
    def steps_for(self, percents, quiet_until_index=0):
        clock = FakeClock()
        steps = ProgressSteps(quiet_seconds=300, clock=clock)
        shown = []
        for index, percent in enumerate(percents):
            clock.now = 300 if index >= quiet_until_index else 10
            due = steps.due(percent)
            if due is not None:
                shown.append(due)
        return shown

    def test_nine_percent_at_five_minutes_then_every_ten(self):
        self.assertEqual(self.steps_for([9, 9, 10, 14, 20, 21, 30]), [9, 10, 20, 30])

    def test_eleven_percent_at_five_minutes_skips_to_twenty(self):
        self.assertEqual(self.steps_for([11, 12, 19, 20, 30]), [11, 20, 30])

    def test_jump_reports_the_reached_ten_once(self):
        self.assertEqual(self.steps_for([5, 47, 48, 50]), [5, 40, 50])

    def test_nothing_after_ninety(self):
        self.assertEqual(self.steps_for([85, 90, 95, 99, 100]), [85, 90])

    def test_silent_while_the_run_is_short(self):
        self.assertEqual(self.steps_for([10, 50, 100], quiet_until_index=99), [])


class DeviceListingTest(unittest.TestCase):
    LISTING = {
        "blockdevices": [
            {
                "name": "sda", "path": "/dev/sda", "tran": "usb", "vendor": "Kingston ",
                "model": "DataTraveler 3.0", "serial": "ABC", "size": "57.7G", "fstype": None,
                "label": None, "uuid": None, "mountpoint": None,
                "children": [
                    {
                        "name": "sda1", "path": "/dev/sda1", "tran": None, "size": "57.7G",
                        "fstype": "ext4", "label": "BACKUP", "uuid": "1111-2222",
                        "mountpoint": "/run/media/me/BACKUP",
                    }
                ],
            },
            {
                "name": "sdb", "path": "/dev/sdb", "tran": "usb", "vendor": None, "model": "Flash",
                "serial": "DEF", "size": "8G", "fstype": "vfat", "label": None, "uuid": "AAAA-BBBB",
                "mountpoint": None,
            },
            {
                "name": "nvme0n1", "path": "/dev/nvme0n1", "tran": "nvme", "size": "476G",
                "fstype": None, "uuid": None,
                "children": [{"name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "fstype": "ext4", "uuid": "9999"}],
            },
        ]
    }

    def test_only_usb_filesystems_are_listed(self):
        found = devices.parse_lsblk(self.LISTING)
        self.assertEqual([filesystem.uuid for filesystem in found], ["1111-2222", "AAAA-BBBB"])
        self.assertEqual(found[0].drive, "Kingston DataTraveler 3.0")
        self.assertEqual(found[0].mountpoint, "/run/media/me/BACKUP")
        self.assertEqual(found[1].device, "/dev/sdb")


class UntrustedInputTest(unittest.TestCase):
    def test_uuid_with_shell_characters_is_rejected(self):
        for hostile in ("abc; rm -rf ~", "a b", "$(id)", "", "x" * 65, None):
            with self.subTest(uuid=hostile):
                self.assertFalse(devices.valid_uuid(hostile))
                with self.assertRaises(ValueError):
                    config.Stick(uuid=hostile, name="x")

    def test_hostile_uuid_is_dropped_from_the_device_listing(self):
        listing = {"blockdevices": [{
            "name": "sdz", "path": "/dev/sdz", "tran": "usb", "size": "1G",
            "fstype": "ext4", "uuid": "abc; touch /tmp/x", "mountpoint": None,
        }]}
        self.assertEqual(devices.parse_lsblk(listing), [])

    def test_password_command_only_accepts_a_checked_uuid(self):
        self.assertEqual(devices.password_command("1111-2222"), "secret-tool lookup service stickup stick 1111-2222")
        with self.assertRaises(ValueError):
            devices.password_command("1111 2222")

    def test_repository_must_stay_a_folder_on_the_stick(self):
        for hostile in ("/etc", "../outside", "a/../../b", ""):
            with self.subTest(repository=hostile):
                with self.assertRaises(ValueError):
                    config.Stick(uuid="1111-2222", name="x", repository=hostile)


class SessionLockTest(unittest.TestCase):
    def locked_with(self, display, hint=None, failing=False):
        def fake_run(command, **_kwargs):
            if failing:
                raise subprocess.CalledProcessError(1, command)
            value = display if "show-user" in command else hint
            return subprocess.CompletedProcess(command, 0, stdout=f"{value}\n", stderr="")

        with mock.patch.object(devices, "run", side_effect=fake_run):
            return devices.session_locked()

    def test_unlocked_session(self):
        self.assertFalse(self.locked_with("2", "no"))

    def test_locked_session(self):
        self.assertTrue(self.locked_with("2", "yes"))

    def test_no_graphical_session_counts_as_locked(self):
        self.assertTrue(self.locked_with(""))

    def test_unknown_hint_counts_as_locked(self):
        self.assertTrue(self.locked_with("2", ""))

    def test_loginctl_failure_counts_as_locked(self):
        self.assertTrue(self.locked_with("2", "no", failing=True))


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=os.environ["XDG_RUNTIME_DIR"])
        root = Path(self.directory.name)
        self.mountpoint = root / "stick"
        self.source = root / "source"
        self.mountpoint.mkdir()
        (self.source / "keep").mkdir(parents=True)
        (self.source / "skip").mkdir()
        (self.source / "keep" / "note.txt").write_text("hello")
        (self.source / "skip" / "junk.txt").write_text("junk")

        filesystem = devices.Filesystem(
            uuid=UUID, device="/dev/null", label="TEST", fstype="ext4", size="1G",
            drive="Test Drive", serial="S", mountpoint=str(self.mountpoint),
        )
        patches = [
            mock.patch.dict(os.environ, {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_RUNTIME_DIR": str(root / "run"),
                "XDG_CACHE_HOME": str(root / "cache"),
            }),
            mock.patch.object(devices, "find", return_value=filesystem),
            mock.patch.object(devices, "session_locked", return_value=False),
            mock.patch.object(devices, "password_command", return_value="echo right-passphrase"),
            mock.patch.object(devices, "has_passphrase", return_value=True),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.directory.cleanup)

        self.stick = config.Stick(
            uuid=UUID, name="TEST", sources=[str(self.source)],
            excludes=[str(self.source / "skip")], keep_last=2, package_lists=False,
        )
        backup.enroll(self.stick, None)

    def test_backup_then_restore_round_trip(self):
        result = backup.run_backup(self.stick, notifications=False)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["snapshot_count"], 1)
        self.assertEqual(config.read_last_run(UUID)["snapshot"], result["snapshot"])
        self.assertEqual(config.read_progress(UUID), {})

        target = Path(self.directory.name) / "restored"
        backup.restore(self.stick, str(self.source), str(target))
        restored = target / str(self.source).lstrip("/")
        self.assertEqual((restored / "keep" / "note.txt").read_text(), "hello")
        self.assertFalse((restored / "skip").exists())

    def test_retention_keeps_only_the_newest(self):
        for round_number in range(3):
            (self.source / "keep" / "note.txt").write_text(f"round {round_number}")
            backup.run_backup(self.stick, notifications=False)
        self.assertEqual(len(backup.list_snapshots(self.stick)), 2)

    def test_locked_session_skips_without_touching_the_stick(self):
        with mock.patch.object(devices, "session_locked", return_value=True):
            result = backup.run_backup(self.stick, notifications=False)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(config.read_last_run(UUID), {})

    def test_busy_stick_skips(self):
        with backup.stick_lock(UUID):
            result = backup.run_backup(self.stick, notifications=False)
        self.assertEqual(result["status"], "skipped")

    def test_wrong_passphrase_fails_and_is_recorded(self):
        with mock.patch.object(devices, "password_command", return_value="echo wrong"):
            result = backup.run_backup(self.stick, notifications=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("error", config.read_last_run(UUID))

    def test_typed_passphrase_check(self):
        self.assertTrue(backup.passphrase_opens(self.stick, "right-passphrase"))
        self.assertFalse(backup.passphrase_opens(self.stick, "nope"))

    def test_repository_symlink_planted_on_the_stick_is_refused(self):
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        hostile = config.Stick(uuid=UUID, name="TEST", repository="planted", package_lists=False)
        (self.mountpoint / "planted").symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "outside the stick"):
            backup.list_snapshots(hostile)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_planted_inside_the_repository_is_refused(self):
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        (self.mountpoint / "restic" / "data" / "planted").symlink_to(outside)
        result = backup.run_backup(self.stick, notifications=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("symlink inside the backup folder", result["error"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_repository_folder_writable_by_others_is_refused(self):
        (self.mountpoint / "restic" / "data").chmod(0o777)
        result = backup.run_backup(self.stick, notifications=False)
        self.assertEqual(result["status"], "failed")
        self.assertIn("not yours alone to write", result["error"])

    def test_enrolling_over_a_folder_that_is_not_a_backup_is_refused(self):
        other = config.Stick(uuid=UUID, name="TEST", repository="photos", package_lists=False)
        (self.mountpoint / "photos").mkdir()
        (self.mountpoint / "photos" / "holiday.jpg").write_text("pixels")
        with self.assertRaisesRegex(RuntimeError, "not a backup"):
            backup.enroll(other, None)
        self.assertEqual([entry.name for entry in (self.mountpoint / "photos").iterdir()], ["holiday.jpg"])

    def test_config_round_trip(self):
        configuration = config.Config()
        configuration.sticks[UUID] = self.stick
        configuration.save()
        self.assertEqual(config.Config().get(UUID), self.stick)


if __name__ == "__main__":
    unittest.main()
